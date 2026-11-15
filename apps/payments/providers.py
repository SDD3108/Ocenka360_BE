"""Halyk ePay transport. No live requests until enabled and configured by the operator."""

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.utils.crypto import constant_time_compare
from rest_framework.exceptions import APIException

from apps.accounts.security import digest


class ProviderUnavailable(APIException):
    status_code = 503
    default_detail = "Payment provider unavailable; retry status verification later."
    default_code = "payment_provider_unavailable"


class ProviderMismatch(APIException):
    status_code = 409
    default_detail = "The bank response requires reconciliation."
    default_code = "payment_requires_review"


def secure_url(value, hosts=None, origin_only=False):
    try:
        u = urlsplit(value)
        return bool(
            u.scheme == "https"
            and u.hostname
            and not u.username
            and not u.password
            and u.port in (None, 443)
            and not u.fragment
            and (not hosts or u.hostname in hosts)
            and (not origin_only or (u.path in ("", "/") and not u.query))
        )
    except ValueError:
        return False


def kaspi_configured():
    return secure_url(settings.KASPI_PAYMENT_URL, {"pay.kaspi.kz"})


def halyk_configured():
    return bool(
        settings.HALYK_ENABLED
        and settings.HALYK_ENVIRONMENT in {"test", "live"}
        and settings.HALYK_CLIENT_ID
        and settings.HALYK_CLIENT_SECRET
        and re.fullmatch(r"[A-Za-z0-9-]{1,80}", settings.HALYK_TERMINAL_ID)
        and secure_url(settings.PAYMENT_PUBLIC_ORIGIN, origin_only=True)
        and secure_url(settings.PAYMENT_RETURN_URL)
        and (settings.ALLOW_ORDER_SIMULATION or settings.HALYK_ENVIRONMENT == "live")
    )


def available_methods():
    return {
        "mock": bool(settings.ALLOW_ORDER_SIMULATION),
        "kaspi_manual": kaspi_configured(),
        "halyk": halyk_configured(),
    }


def callback_secret(intent):
    return digest("halyk-postlink", intent.pk)


def valid_callback(intent, value):
    return isinstance(value, str) and constant_time_compare(callback_secret(intent), value)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(method, url, fields=None, token=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if fields is not None:
        body = urlencode(fields).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(NoRedirect).open(req, timeout=settings.HALYK_HTTP_TIMEOUT) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ProviderUnavailable()
            if not raw:
                return {}
            result = json.loads(raw, parse_float=Decimal)
            if not isinstance(result, dict):
                raise ProviderUnavailable()
            return result
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        # Do not return/log bank bodies, credentials, URLs with tokens, or card metadata.
        raise ProviderUnavailable() from None


def decimal_amount(value):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result != result.quantize(Decimal(".01")):
            raise ValueError
        return result
    except (ValueError, InvalidOperation):
        raise ProviderMismatch() from None


class Halyk:
    def __init__(self, intent):
        if (
            not halyk_configured()
            or intent.provider != "halyk"
            or intent.environment != settings.HALYK_ENVIRONMENT
            or intent.terminal_id != settings.HALYK_TERMINAL_ID
        ):
            raise ProviderUnavailable()
        self.intent = intent
        test = intent.environment == "test"
        self.oauth = (
            "https://test-epay-oauth.epayment.kz/oauth2/token"
            if test
            else "https://epay-oauth.homebank.kz/oauth2/token"
        )
        self.api = "https://test-epay-api.epayment.kz" if test else "https://epay-api.homebank.kz"
        self.script = (
            "https://test-epay.epayment.kz/payform/payment-api.js"
            if test
            else "https://epay.homebank.kz/payform/payment-api.js"
        )

    def token(self, payment=False):
        fields = {
            "grant_type": "client_credentials",
            "scope": "webapi usermanagement email_send verification statement statistics payment",
            "client_id": settings.HALYK_CLIENT_ID,
            "client_secret": settings.HALYK_CLIENT_SECRET,
            "terminal": self.intent.terminal_id,
        }
        if payment:
            fields.update(
                invoiceID=self.intent.invoice_id,
                amount=str(self.intent.amount),
                currency="KZT",
                secret_hash=callback_secret(self.intent),
                postLink=self.post_link(),
            )
        data = request_json("POST", self.oauth, fields=fields)
        if (
            not isinstance(data.get("access_token"), str)
            or not data["access_token"]
            or data.get("token_type", "").lower() != "bearer"
        ):
            raise ProviderUnavailable()
        return {
            key: data[key] for key in ("access_token", "expires_in", "scope", "token_type") if key in data
        }

    def post_link(self):
        return settings.PAYMENT_PUBLIC_ORIGIN.rstrip("/") + "/api/v1/payments/halyk/postlink/"

    def checkout(self):
        return {
            "kind": "halyk_page",
            "script_url": self.script,
            "payment_object": {
                "invoiceId": self.intent.invoice_id,
                "amount": str(self.intent.amount),
                "currency": "KZT",
                "terminal": self.intent.terminal_id,
                "description": "Ocenka360 " + str(self.intent.pk),
                "language": {"ru": "rus", "kk": "kaz", "en": "eng"}[self.intent.order.language],
                "backLink": settings.PAYMENT_RETURN_URL,
                "failureBackLink": settings.PAYMENT_RETURN_URL,
                "postLink": self.post_link(),
                "auth": self.token(payment=True),
            },
            "is_test": self.intent.environment != "live",
        }

    def status(self):
        data = request_json(
            "GET",
            self.api + "/check-status/payment/transaction/" + self.intent.invoice_id,
            token=self.token()["access_token"],
        )
        code = str(data.get("resultCode"))
        if code in {"101", "102", "107"}:
            return {"state": "pending"}
        if code != "100" or not isinstance(data.get("transaction"), dict):
            raise ProviderUnavailable()
        tx = data["transaction"]
        if (
            str(tx.get("invoiceID")) != self.intent.invoice_id
            or tx.get("terminalID") != self.intent.terminal_id
            or tx.get("currency") != "KZT"
        ):
            raise ProviderMismatch()
        state = tx.get("statusName")
        if state not in {"CHARGE", "REFUND"}:
            return {"state": "pending"}
        # REFUND payload shape/amount semantics can differ for partial refunds: never infer them.
        amount = decimal_amount(tx.get("amount"))
        if amount != self.intent.amount:
            raise ProviderMismatch()
        txid = tx.get("id")
        if not isinstance(txid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", txid):
            raise ProviderMismatch()
        return {
            "state": "paid" if state == "CHARGE" else "refunded",
            "transaction_id": txid,
            "amount": amount,
        }

    def refund_full(self, refund):
        if (
            refund.amount != refund.receipt.amount
            or refund.receipt.refunds.exclude(pk=refund.pk).exists()
            or refund.amount < Decimal("10.00")
        ):
            raise ProviderMismatch("Use bank-cabinet verification for partial or small refunds.")
        return request_json(
            "POST",
            self.api
            + "/operation/"
            + refund.receipt.transaction_id
            + "/refund?"
            + urlencode({"externalID": str(refund.pk)}),
            token=self.token()["access_token"],
        )
