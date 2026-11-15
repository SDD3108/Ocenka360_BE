import io
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from apps.orders import services as orders
from apps.orders.models import Order
from apps.orders.tests.test_orders import ROOT
from apps.payments import providers, services
from apps.payments.models import (
    InvoiceSequence,
    PaymentAccount,
    PaymentAudit,
    PaymentIntent,
    Receipt,
    Refund,
    RefundEntry,
)
from apps.pricing.tests.test_pricing import PricingFixture
from apps.scheduling import services as scheduling

HALYK = dict(
    HALYK_ENABLED=True,
    HALYK_ENVIRONMENT="test",
    HALYK_CLIENT_ID="unit-test-client",
    HALYK_CLIENT_SECRET="unit-test-secret-not-real",
    HALYK_TERMINAL_ID="test-terminal",
    PAYMENT_PUBLIC_ORIGIN="https://api.example.invalid",
    PAYMENT_RETURN_URL="https://example.invalid/payment-return",
)
KASPI = dict(KASPI_PAYMENT_URL="https://pay.kaspi.kz/pay/TESTONLY")


class PaymentFixture(PricingFixture):
    def awaiting(self):
        self.api.force_authenticate(self.owner)
        data = self.draft()
        response = self.confirm(data)
        self.assertEqual(response.status_code, 200, response.json())
        return Order.objects.get(pk=data["id"])

    def payment(self, order, purpose="advance", provider="mock", **extra):
        self.api.force_authenticate(self.owner)
        return self.api.post(
            ROOT + f"orders/{order.pk}/payments/",
            {
                "expected_version": order.version,
                "client_request_id": str(uuid.uuid4()),
                "purpose": purpose,
                "provider": provider,
                **extra,
            },
        )

    def intent(self, order, **extra):
        response = self.payment(order, **extra)
        self.assertEqual(response.status_code, 201, response.json())
        order.refresh_from_db()
        return PaymentIntent.objects.get(pk=response.json()["payment"]["id"])

    def paid_advance(self):
        order = self.awaiting()
        intent = self.intent(order)
        services.simulate_payment(intent.pk)
        order.refresh_from_db()
        return order, intent

    def agreed_paid(self, price="20000.00", balance=True):
        order, advance = self.paid_advance()
        order = orders.review_action(self.reviewer, order.pk, order.version, "start_review")
        order = orders.review_action(self.reviewer, order.pk, order.version, "accept")
        quote = self.issued(order, lines=[{"kind": "appraisal", "title": "TEST", "amount": price}])
        response = self.accept(order, quote)
        self.assertEqual(response.status_code, 200, response.json())
        order.refresh_from_db()
        if balance and services.balance(order)["amount_due"] > 0:
            intent = self.intent(order, purpose="balance")
            services.simulate_payment(intent.pk)
            order.refresh_from_db()
        return order, advance

    def bank_status(self, intent, state="CHARGE", **extra):
        return {
            "resultCode": "100",
            "transaction": {
                "id": "tx-" + str(intent.pk),
                "invoiceID": intent.invoice_id,
                "terminalID": intent.terminal_id,
                "currency": "KZT",
                "amount": str(intent.amount),
                "statusName": state,
                **extra,
            },
        }

    def query_bank(self, intent, state="CHARGE", **extra):
        with patch(
            "apps.payments.providers.request_json",
            side_effect=[
                {"access_token": "test-only", "token_type": "Bearer"},
                self.bank_status(intent, state, **extra),
            ],
        ):
            return services.refresh_intent(intent.pk)


class LedgerTests(PaymentFixture):
    def test_methods_disabled_without_configuration_except_local_mock(self):
        self.api.force_authenticate(None)
        data = self.api.get(ROOT + "payments/methods/").json()
        self.assertEqual(
            data,
            {
                "mock": True,
                "kaspi_manual": False,
                "halyk": False,
                "advance_amount": "2000.00",
                "currency": "KZT",
                "halyk_environment": None,
            },
        )
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            self.assertFalse(self.api.get(ROOT + "payments/methods/").json()["mock"])

    def test_advance_balance_and_full_refund_idempotency(self):
        order, advance = self.agreed_paid()
        b = services.balance(order)
        self.assertEqual(b["received"], Decimal("20000"))
        self.assertEqual(b["amount_due"], 0)
        self.assertEqual(order.payment_intents.last().amount, Decimal("18000"))
        initial_count = order.events.count()
        services.simulate_payment(advance.pk)
        self.assertEqual(order.events.count(), initial_count)
        self.assertEqual(Receipt.objects.count(), 2)
        order = orders.cancel_order(self.owner, order.pk, order.version, "TEST")
        self.assertEqual(services.balance(order)["refunded"], 0)
        self.assertEqual(services.balance(order)["refund_pending"], Decimal("20000"))
        for refund in Refund.objects.all():
            services.simulate_refund(refund.pk)
            services.simulate_refund(refund.pk)
        self.assertEqual(RefundEntry.objects.count(), 2)
        self.assertEqual(services.balance(order)["refunded"], Decimal("20000"))
        self.assertEqual(services.balance(order)["net_paid"], 0)

    def test_creation_is_idempotent_and_amount_cannot_be_supplied(self):
        order = self.awaiting()
        key = str(uuid.uuid4())
        first = self.payment(order, client_request_id=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.payment(order, client_request_id=key).status_code, 200)
        self.assertEqual(self.payment(order, client_request_id=key, purpose="balance").status_code, 409)
        self.assertEqual(self.payment(order, amount="1.00").status_code, 400)
        order.refresh_from_db()
        self.assertEqual(self.payment(order).status_code, 409)
        self.assertEqual(PaymentIntent.objects.count(), 1)

    def test_cannot_pay_draft_or_balance_before_agreement(self):
        order = Order.objects.get(pk=self.draft()["id"])
        self.assertEqual(self.payment(order).status_code, 409)
        order = self.awaiting()
        self.assertEqual(self.payment(order, purpose="balance").status_code, 409)
        self.assertEqual(self.payment(order, provider="halyk").status_code, 503)

    def test_advance_snapshot_does_not_change_with_config(self):
        order = self.awaiting()
        intent = self.intent(order)
        with override_settings(PAYMENT_ADVANCE_KZT="5000.00"):
            self.assertEqual(services.balance(order)["advance_amount"], Decimal("2000"))
            self.assertEqual(intent.amount, Decimal("2000"))
        self.assertEqual(PaymentAccount.objects.count(), 1)

    def test_legacy_simulation_is_not_migrated_to_fake_money(self):
        order = self.submitted()
        b = services.balance(order)
        self.assertTrue(b["legacy_simulation"])
        self.assertEqual(b["received"], 0)
        self.assertFalse(Receipt.objects.exists())

    def test_late_payment_after_cancellation_never_reopens_order(self):
        order = self.awaiting()
        intent = self.intent(order)
        order = orders.cancel_order(self.owner, order.pk, order.version)
        services.simulate_payment(intent.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertIsNone(order.review_unlocked_at)
        self.assertEqual(Refund.objects.get().amount, Decimal("2000"))

    def test_expired_attempt_can_be_replaced_but_late_settlement_is_accounted(self):
        order = self.awaiting()
        old = self.intent(order)
        PaymentIntent.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        new = self.intent(order)
        services.simulate_payment(new.pk)
        services.simulate_payment(old.pk)
        order.refresh_from_db()
        self.assertEqual(services.balance(order)["received"], Decimal("4000"))
        self.assertEqual(order.status, "submitted")
        order = orders.cancel_order(self.owner, order.pk, order.version)
        self.assertEqual(services.balance(order)["refund_pending"], Decimal("4000"))

    def test_price_below_advance_reserves_only_excess_and_rounds_tiyn(self):
        order, intent = self.agreed_paid(price="1500.11", balance=False)
        self.assertEqual(services.balance(order)["amount_due"], 0)
        self.assertEqual(Refund.objects.get().amount, Decimal("499.89"))
        services.simulate_refund(Refund.objects.get().pk)
        self.assertEqual(services.balance(order)["net_paid"], Decimal("1500.11"))
        services.require_funded(order)

    def test_unpaid_balance_blocks_start_but_keeps_scheduling_available(self):
        order, advance = self.agreed_paid(balance=False)
        start = timezone.now() + timedelta(days=1)
        appt, order, _ = scheduling.propose(
            self.owner,
            order.pk,
            {
                "expected_version": order.version,
                "client_request_id": uuid.uuid4(),
                "mode": "onsite",
                "starts_at": start,
                "ends_at": start + timedelta(hours=1),
            },
        )
        appt, order = scheduling.confirm_request(
            self.reviewer,
            order.pk,
            appt.pk,
            {
                "expected_version": order.version,
                "starts_at": start,
                "ends_at": start + timedelta(hours=1),
                "report_hours": 24,
                "buffer_before_minutes": 0,
                "buffer_after_minutes": 0,
            },
        )
        with patch("apps.scheduling.services.timezone.now", return_value=start + timedelta(minutes=1)):
            with self.assertRaises(orders.Conflict):
                scheduling.start_inspection(self.reviewer, order.pk, appt.pk, order.version)
        intent = self.intent(order, purpose="balance")
        services.simulate_payment(intent.pk)
        order.refresh_from_db()
        with patch("apps.scheduling.services.timezone.now", return_value=start + timedelta(minutes=1)):
            appt, order = scheduling.start_inspection(self.reviewer, order.pk, appt.pk, order.version)
        self.assertEqual(order.status, "in_progress")

    def test_owner_isolation_and_no_public_payment_confirmation(self):
        order, intent = self.paid_advance()
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(ROOT + f"orders/{order.pk}/payments/").status_code, 404)
        self.assertEqual(
            self.api.post(ROOT + f"orders/{order.pk}/payments/{intent.pk}/checkout/", {}).status_code, 404
        )
        self.api.force_authenticate(self.owner)
        self.assertEqual(
            self.api.post(
                ROOT + f"review/orders/{order.pk}/payments/{intent.pk}/confirm-manual/", {}
            ).status_code,
            403,
        )
        self.assertEqual(self.api.get(ROOT + f"orders/{order.pk}/payments/history/").status_code, 200)
        self.api.force_authenticate(None)
        self.assertEqual(self.api.get(ROOT + f"orders/{order.pk}/payments/").status_code, 401)

    def test_simulations_disabled_in_production_and_cannot_mix_real(self):
        order, intent = self.paid_advance()
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            with self.assertRaises(PermissionDenied) as caught:
                services.simulate_payment(intent.pk)
            self.assertEqual(caught.exception.status_code, 403)
        with override_settings(**KASPI):
            order, advance = self.agreed_paid(balance=False)
            self.assertEqual(self.payment(order, purpose="balance", provider="kaspi_manual").status_code, 409)

    def test_demo_payments_rolls_back_every_financial_record(self):
        before = (
            PaymentIntent.objects.count(),
            Receipt.objects.count(),
            Refund.objects.count(),
            PaymentAudit.objects.count(),
        )
        output = io.StringIO()
        call_command("demo_payments", stdout=output)
        self.assertIn("refunded: 20000", output.getvalue())
        self.assertIn("awaiting_report", output.getvalue())
        self.assertEqual(
            before,
            (
                PaymentIntent.objects.count(),
                Receipt.objects.count(),
                Refund.objects.count(),
                PaymentAudit.objects.count(),
            ),
        )
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            with self.assertRaises(CommandError):
                call_command("demo_payments", stdout=io.StringIO())


@override_settings(**KASPI)
class KaspiTests(PaymentFixture):
    def test_checkout_amount_is_manual_and_staff_checks_bank(self):
        order = self.awaiting()
        intent = self.intent(order, provider="kaspi_manual")
        data = self.api.post(ROOT + f"orders/{order.pk}/payments/{intent.pk}/checkout/", {}).json()
        self.assertFalse(data["amount_prefilled"])
        self.assertEqual(data["amount"], "2000.00")
        self.assertEqual(data["url"], KASPI["KASPI_PAYMENT_URL"])
        self.assertFalse(Receipt.objects.exists())
        self.api.force_authenticate(self.reviewer)
        path = ROOT + f"review/orders/{order.pk}/payments/{intent.pk}/confirm-manual/"
        body = {
            "expected_version": order.version,
            "transaction_id": "KASPI-TEST-1",
            "amount": "2000.00",
            "confirmed_in_bank": True,
        }
        self.assertEqual(self.api.post(path, {**body, "confirmed_in_bank": False}).status_code, 400)
        self.assertEqual(self.api.post(path, {**body, "amount": "0.00"}).status_code, 400)
        response = self.api.post(path, body)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(self.api.post(path, body).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "submitted")
        self.assertEqual(order.payment_status, "partially_paid")
        self.assertEqual(Receipt.objects.get().verification, "manual_bank_review")
        self.assertFalse(order.is_test)

    def test_manual_reference_cannot_pay_two_orders(self):
        first = self.awaiting()
        a = self.intent(first, provider="kaspi_manual")
        services.confirm_manual(
            self.reviewer, first.pk, a.pk, first.version, "SAME-REF", Decimal("2000"), True
        )
        second = self.awaiting()
        b = self.intent(second, provider="kaspi_manual")
        with self.assertRaises(orders.Conflict):
            services.confirm_manual(
                self.reviewer, second.pk, b.pk, second.version, "SAME-REF", Decimal("2000"), True
            )
        self.assertEqual(Receipt.objects.count(), 1)
        second.refresh_from_db()
        self.assertEqual(second.status, "awaiting_payment")

    def test_manual_refund_requires_actual_bank_confirmation(self):
        order = self.awaiting()
        intent = self.intent(order, provider="kaspi_manual")
        receipt, order = services.confirm_manual(
            self.reviewer, order.pk, intent.pk, order.version, "KASPI-TEST-2", Decimal("2000"), True
        )
        order = orders.cancel_order(self.owner, order.pk, order.version)
        refund = Refund.objects.get()
        self.api.force_authenticate(self.reviewer)
        path = ROOT + f"review/orders/{order.pk}/refunds/{refund.pk}/confirm-manual/"
        body = {
            "expected_version": order.version,
            "transaction_id": "KASPI-REFUND-1",
            "amount": "2000.00",
            "confirmed_in_bank": True,
        }
        self.assertEqual(self.api.post(path, {**body, "confirmed_in_bank": False}).status_code, 400)
        self.assertEqual(self.api.post(path, body).status_code, 200)
        self.assertEqual(self.api.post(path, body).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(RefundEntry.objects.get().verification, "manual_bank_review")

    def test_bank_urls_cannot_point_to_other_hosts_or_http(self):
        for url in [
            "http://pay.kaspi.kz/a",
            "https://pay.kaspi.kz.evil.invalid/a",
            "https://user:password@pay.kaspi.kz/a",
            "https://pay.kaspi.kz:444/a",
        ]:
            with override_settings(KASPI_PAYMENT_URL=url):
                self.assertFalse(providers.kaspi_configured())


@override_settings(**HALYK)
class HalykTests(PaymentFixture):
    def test_callback_is_hint_only_and_cannot_credit_forged_paid_status(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        self.api.force_authenticate(None)
        path = ROOT + "payments/halyk/postlink/"
        body = {
            "invoiceId": intent.invoice_id,
            "secret_hash": "forged",
            "code": "ok",
            "amount": "2000",
            "currency": "KZT",
        }
        self.assertEqual(self.api.post(path, body).status_code, 403)
        body["secret_hash"] = providers.callback_secret(intent)
        self.assertEqual(self.api.post(path, body).status_code, 202)
        self.assertEqual(self.api.post(path, body).status_code, 202)
        intent.refresh_from_db()
        self.assertIsNotNone(intent.sync_requested_at)
        self.assertFalse(Receipt.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.status, "awaiting_payment")
        self.query_bank(intent)
        order.refresh_from_db()
        self.assertEqual(order.status, "submitted")

    def test_checkout_never_returns_merchant_secret_and_preserves_server_amount(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        with patch(
            "apps.payments.providers.request_json",
            return_value={"access_token": "scoped-test", "token_type": "Bearer", "expires_in": 7200},
        ) as transport:
            response = self.api.post(ROOT + f"orders/{order.pk}/payments/{intent.pk}/checkout/", {})
        self.assertEqual(response.status_code, 200, response.json())
        self.assertNotIn(HALYK["HALYK_CLIENT_SECRET"], str(response.json()))
        self.assertNotIn("secret_hash", str(response.json()))
        self.assertEqual(response.json()["payment_object"]["amount"], "2000.00")
        self.assertEqual(transport.call_args.kwargs["fields"]["invoiceID"], intent.invoice_id)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertTrue(response.json()["is_test"])

    def test_only_charge_is_credited_and_duplicate_poll_is_safe(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        self.query_bank(intent, state="AUTH")
        self.assertFalse(Receipt.objects.exists())
        self.query_bank(intent)
        self.query_bank(intent)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(Receipt.objects.get().verification, "bank_api")

    def test_mismatched_amount_currency_terminal_invoice_never_credited(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        for extra in [
            {"amount": "1.00"},
            {"currency": "USD"},
            {"terminalID": "someone-else"},
            {"invoiceID": "999999"},
            {"amount": "NaN"},
        ]:
            with self.assertRaises(providers.ProviderMismatch):
                self.query_bank(intent, **extra)
        self.assertFalse(Receipt.objects.exists())
        intent.refresh_from_db()
        self.assertTrue(intent.needs_review)

    def test_provider_unavailable_never_claims_payment_failure_or_success(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        with patch("apps.payments.providers.request_json", side_effect=providers.ProviderUnavailable):
            response = self.api.post(ROOT + f"orders/{order.pk}/payments/{intent.pk}/refresh/", {})
        self.assertEqual(response.status_code, 503)
        intent.refresh_from_db()
        self.assertEqual(intent.state, "pending")
        self.assertFalse(Receipt.objects.exists())

    def test_full_refund_is_pending_until_bank_status_verifies_it(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        self.query_bank(intent)
        order.refresh_from_db()
        order = orders.cancel_order(self.owner, order.pk, order.version)
        refund = Refund.objects.get()
        with patch("apps.payments.providers.Halyk.refund_full", return_value={}):
            refund, order = services.execute_refund(self.reviewer, order.pk, refund.pk, order.version, True)
        self.assertEqual(refund.state, "submitted")
        self.assertFalse(RefundEntry.objects.exists())
        self.query_bank(intent, state="REFUND")
        self.query_bank(intent, state="REFUND")
        self.assertEqual(RefundEntry.objects.count(), 1)
        self.assertEqual(services.balance(order)["net_paid"], 0)

    def test_uncertain_refund_is_not_retried_and_can_be_reconciled(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        self.query_bank(intent)
        order.refresh_from_db()
        order = orders.cancel_order(self.owner, order.pk, order.version)
        refund = Refund.objects.get()
        with patch(
            "apps.payments.providers.Halyk.refund_full", side_effect=providers.ProviderUnavailable
        ) as transport:
            refund, order = services.execute_refund(self.reviewer, order.pk, refund.pk, order.version, True)
            self.assertEqual(refund.state, "unknown")
            with self.assertRaises(orders.Conflict):
                services.execute_refund(self.reviewer, order.pk, refund.pk, order.version, True)
            self.assertEqual(transport.call_count, 1)
        self.query_bank(intent, state="REFUND")
        self.assertEqual(RefundEntry.objects.count(), 1)

    def test_external_refund_needs_review_and_is_not_guessed(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        self.query_bank(intent)
        self.query_bank(intent, state="REFUND")
        intent.refresh_from_db()
        self.assertTrue(intent.needs_review)
        self.assertFalse(RefundEntry.objects.exists())

    def test_invoice_sequence_never_recycles_last_six_digits(self):
        InvoiceSequence.objects.filter(pk=1).update(next_value=999999)
        first = self.awaiting()
        a = self.intent(first, provider="halyk")
        self.assertEqual(a.invoice_id, "999999")
        second = self.awaiting()
        self.assertEqual(self.payment(second, provider="halyk").status_code, 503)
        self.assertEqual(PaymentIntent.objects.count(), 1)

    def test_terminal_rotation_and_production_reject_test_credentials(self):
        order = self.awaiting()
        intent = self.intent(order, provider="halyk")
        with override_settings(HALYK_TERMINAL_ID="new-terminal"):
            with self.assertRaises(providers.ProviderUnavailable):
                providers.Halyk(intent)
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            self.assertFalse(providers.halyk_configured())


class CancellationTests(PaymentFixture):
    def started(self):
        order, advance = self.agreed_paid()
        start = timezone.now() + timedelta(days=1)
        appointment, order, _ = scheduling.propose(
            self.owner,
            order.pk,
            {
                "expected_version": order.version,
                "client_request_id": uuid.uuid4(),
                "mode": "onsite",
                "starts_at": start,
                "ends_at": start + timedelta(hours=1),
            },
        )
        appointment, order = scheduling.confirm_request(
            self.reviewer,
            order.pk,
            appointment.pk,
            {
                "expected_version": order.version,
                "starts_at": start,
                "ends_at": start + timedelta(hours=1),
                "report_hours": 24,
                "buffer_before_minutes": 0,
                "buffer_after_minutes": 0,
            },
        )
        with patch("apps.scheduling.services.timezone.now", return_value=start + timedelta(minutes=1)):
            appointment, order = scheduling.start_inspection(
                self.reviewer, order.pk, appointment.pk, order.version
            )
        return order, appointment

    def test_partial_refund_requires_client_agreement_and_stops_calendar(self):
        order, appointment = self.started()
        self.api.force_authenticate(self.owner)
        path = ROOT + f"orders/{order.pk}/cancellations/"
        payload = {
            "expected_version": order.version,
            "client_request_id": str(uuid.uuid4()),
            "reason": "Больше не требуется",
        }
        response = self.api.post(path, payload)
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(self.api.post(path, payload).status_code, 200)
        case_id = response.json()["case"]["id"]
        order.refresh_from_db()
        self.api.force_authenticate(self.reviewer)
        offered = self.api.post(
            ROOT + f"review/orders/{order.pk}/cancellations/{case_id}/offer/",
            {
                "expected_version": order.version,
                "retained_amount": "5000.25",
                "comment": "Осмотр выполнен, предлагаю удержать согласованную стоимость выезда и работы",
            },
        )
        self.assertEqual(offered.status_code, 200, offered.json())
        order.refresh_from_db()
        self.assertFalse(Refund.objects.exists())
        self.api.force_authenticate(self.owner)
        body = {"expected_version": order.version, "action": "accept", "confirmed": False}
        self.assertEqual(self.api.post(path + case_id + "/decide/", body).status_code, 400)
        body["confirmed"] = True
        accepted = self.api.post(path + case_id + "/decide/", body)
        self.assertEqual(accepted.status_code, 200, accepted.json())
        self.assertEqual(self.api.post(path + case_id + "/decide/", body).status_code, 409)
        order.refresh_from_db()
        appointment.refresh_from_db()
        self.assertEqual(order.status, "service_stopped")
        self.assertEqual(appointment.state, "cancelled")
        self.assertEqual(order.schedule_events.filter(action="service_stopped").count(), 1)
        self.assertEqual(services.balance(order)["refund_pending"], Decimal("14999.75"))
        for refund in Refund.objects.all():
            services.simulate_refund(refund.pk)
        self.assertEqual(services.balance(order)["net_paid"], Decimal("5000.25"))
        self.assertEqual(services.balance(order)["retained_amount"], Decimal("5000.25"))
        self.assertEqual(services.balance(order)["amount_due"], 0)

    def test_withdraw_case_keeps_work_and_prework_uses_normal_cancel(self):
        order, advance = self.agreed_paid()
        with self.assertRaises(orders.Conflict):
            services.request_cancellation(self.owner, order.pk, order.version, uuid.uuid4(), "TEST")
        order, appointment = self.started()
        case, order, _ = services.request_cancellation(
            self.owner, order.pk, order.version, uuid.uuid4(), "TEST"
        )
        case, order = services.decide_cancellation(self.owner, order.pk, case.pk, order.version, "withdraw")
        self.assertEqual(case.state, "withdrawn")
        self.assertEqual(order.status, "in_progress")
        self.assertFalse(Refund.objects.exists())

    def test_retained_amount_cannot_exceed_paid_and_offer_change_invalidates_old_accept(self):
        order, appointment = self.started()
        case, order, _ = services.request_cancellation(
            self.owner, order.pk, order.version, uuid.uuid4(), "TEST"
        )
        self.api.force_authenticate(self.reviewer)
        path = ROOT + f"review/orders/{order.pk}/cancellations/{case.pk}/offer/"
        response = self.api.post(
            path, {"expected_version": order.version, "retained_amount": "20000.01", "comment": "TEST"}
        )
        self.assertEqual(response.status_code, 400)
        case, order = services.offer_cancellation(
            self.reviewer, order.pk, case.pk, order.version, Decimal("5000"), "TEST"
        )
        oldversion = order.version
        case, order = services.offer_cancellation(
            self.reviewer, order.pk, case.pk, order.version, Decimal("4000"), "TEST revised"
        )
        with self.assertRaises(orders.Conflict):
            services.decide_cancellation(self.owner, order.pk, case.pk, oldversion, "accept", True)
        self.assertFalse(Refund.objects.exists())

    def test_finance_queues_are_reviewer_only_and_include_unpaid_manual_work(self):
        with override_settings(**KASPI):
            order = self.awaiting()
            intent = self.intent(order, provider="kaspi_manual")
        self.assertEqual(self.api.get(ROOT + "review/payments/").status_code, 403)
        self.api.force_authenticate(self.reviewer)
        response = self.api.get(ROOT + "review/payments/?state=pending&provider=kaspi_manual")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["results"][0]["id"], str(intent.pk))
        self.assertEqual(self.api.get(ROOT + "review/refunds/").status_code, 200)
        self.assertEqual(self.api.get(ROOT + "review/cancellations/").status_code, 200)

    def test_legacy_payment_simulator_cannot_relabel_a_payment_intent(self):
        order = self.awaiting()
        self.intent(order)
        with self.assertRaises(orders.Conflict):
            orders.simulate_payment(order.pk)


@override_settings(**KASPI)
class ManualAmountTests(PaymentFixture):
    def test_kaspi_underpayment_stays_locked_and_next_intent_only_collects_missing_amount(self):
        order = self.awaiting()
        first = self.intent(order, provider="kaspi_manual")
        receipt, order = services.confirm_manual(
            self.reviewer, order.pk, first.pk, order.version, "PARTIAL-1", Decimal("1200.10"), True
        )
        self.assertEqual(receipt.amount, Decimal("1200.10"))
        self.assertEqual(order.status, "awaiting_payment")
        self.assertIsNone(order.review_unlocked_at)
        self.assertEqual(services.balance(order)["amount_due"], Decimal("799.90"))
        second = self.intent(order, provider="kaspi_manual")
        self.assertEqual(second.amount, Decimal("799.90"))
        receipt, order = services.confirm_manual(
            self.reviewer, order.pk, second.pk, order.version, "PARTIAL-2", Decimal("799.90"), True
        )
        self.assertEqual(order.status, "submitted")
        self.assertEqual(services.balance(order)["net_paid"], Decimal("2000.00"))
        with self.assertRaises(orders.Conflict):
            services.confirm_manual(
                self.reviewer, order.pk, second.pk, order.version, "PARTIAL-2", Decimal("1000"), True
            )

    def test_kaspi_overpayment_is_recorded_as_received_and_returned_on_cancel(self):
        order = self.awaiting()
        intent = self.intent(order, provider="kaspi_manual")
        receipt, order = services.confirm_manual(
            self.reviewer, order.pk, intent.pk, order.version, "OVER-1", Decimal("3000.25"), True
        )
        self.assertEqual(services.balance(order)["received"], Decimal("3000.25"))
        order = orders.cancel_order(self.owner, order.pk, order.version)
        self.assertEqual(Refund.objects.get().amount, Decimal("3000.25"))
