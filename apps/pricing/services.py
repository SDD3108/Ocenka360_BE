import hashlib
import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError as ModelValidationError
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.accounts.security import reserve_quota
from apps.catalog.models import Service
from apps.orders import services as orders
from apps.orders.models import Order

from .calculator import amount, estimate
from .models import (
    MAX_MONEY,
    PriceQuote,
    PricingAudit,
    PromoCode,
    PromoRedemption,
    PromoSelection,
    ServiceTariff,
    TariffRevision,
)


def save_validated(obj):
    try:
        obj.save()
    except ModelValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
    return obj


def audit(actor, action, target):
    PricingAudit.objects.create(actor=actor, action=action, target_id=target.pk)


def admin_actor(actor):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    if actor.role != User.Role.ADMIN:
        raise PermissionDenied("Admin role required.")
    return actor


@transaction.atomic
def create_tariff(actor, data):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    data = dict(data)
    service = get_object_or_404(Service.objects.select_for_update(), pk=data.pop("service_id"))
    version = (service.tariffs.aggregate(v=Max("version"))["v"] or 0) + 1
    tariff = save_validated(TariffRevision(service=service, version=version, created_by=actor, **data))
    audit(actor, "tariff_created", tariff)
    return tariff


@transaction.atomic
def publish_tariff(actor, tariff_id, expected_current_id):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    candidate = get_object_or_404(TariffRevision, pk=tariff_id)
    service = Service.objects.select_for_update().get(pk=candidate.service_id)
    tariff = TariffRevision.objects.select_for_update().get(pk=tariff_id)
    binding = ServiceTariff.objects.filter(service=service).first()
    current_id = binding.current_id if binding else None
    if current_id == tariff.pk:
        return tariff
    if current_id != expected_current_id:
        raise orders.Conflict("Current tariff changed. Reload pricing.", code="tariff_conflict")
    if tariff.published_at:
        raise orders.Conflict("Create a new revision to restore an older price.", code="tariff_published")
    tariff.full_clean()
    tariff.published_at = timezone.now()
    tariff.published_by = actor
    save_validated(tariff)
    ServiceTariff.objects.update_or_create(service=service, defaults={"current": tariff})
    audit(actor, "tariff_published", tariff)
    return tariff


@transaction.atomic
def create_promo(actor, data):
    actor = admin_actor(actor)
    data = dict(data)
    service_id = data.pop("service_id", None)
    service = get_object_or_404(Service, pk=service_id) if service_id else None
    promo = save_validated(PromoCode(created_by=actor, service=service, **data))
    audit(actor, "promo_created", promo)
    return promo


@transaction.atomic
def toggle_promo(actor, promo_id, expected_version, is_active):
    actor = admin_actor(actor)
    promo = get_object_or_404(PromoCode.objects.select_for_update(), pk=promo_id)
    orders.check_version(promo, expected_version)
    if promo.is_active != is_active:
        promo.is_active = is_active
        save_validated(promo)
        audit(actor, "promo_enabled" if is_active else "promo_disabled", promo)
    return promo


def promo_available(promo, order):
    now = timezone.now()
    if (
        not promo
        or not promo.is_active
        or not promo.starts_at <= now < promo.ends_at
        or (promo.service_id and promo.service_id != order.service_id)
    ):
        raise orders.Conflict("This promotion is not available for this order.", code="promo_unavailable")
    uses = promo.redemptions.filter(released_at__isnull=True)
    if (promo.max_uses is not None and uses.count() >= promo.max_uses) or uses.filter(
        user_id=order.owner_id
    ).count() >= promo.per_user_limit:
        raise orders.Conflict("This promotion is not available for this order.", code="promo_unavailable")


def promo_discount(promo, order, appraisal_subtotal):
    promo_available(promo, order)
    if appraisal_subtotal < promo.minimum_subtotal:
        raise orders.Conflict("This promotion is not applicable to this subtotal.", code="promo_unavailable")
    discount = (
        promo.value
        if promo.kind == PromoCode.Kind.FIXED
        else Decimal(amount(appraisal_subtotal * promo.value / 100))
    )
    if promo.maximum_discount is not None:
        discount = min(discount, promo.maximum_discount)
    if discount <= 0 or discount >= appraisal_subtotal:
        raise orders.Conflict(
            "Promotion cannot make the appraisal fee zero or negative.", code="promo_unavailable"
        )
    return discount


def selected_promo(order, lock=False):
    selection = PromoSelection.objects.filter(order=order).first()
    if not selection or not selection.promo_id:
        return None
    query = PromoCode.objects.select_for_update() if lock else PromoCode.objects
    return query.get(pk=selection.promo_id)


def select_promo(actor, order_id, expected_version, code):
    # Commit this quota even if the following lookup is invalid: prevents guessing codes for free.
    with transaction.atomic():
        actor = orders.actor_for_write(actor)
        get_object_or_404(Order, pk=order_id, owner=actor)
        reserve_quota("promo-selection", str(actor.pk), 30, 3600)
    with transaction.atomic():
        actor = orders.actor_for_write(actor)
        order = get_object_or_404(Order.objects.select_for_update(), pk=order_id, owner=actor)
        orders.supported(order)
        orders.check_version(order, expected_version)
        if order.status not in {Order.Status.DRAFT, Order.Status.NEEDS_INFORMATION, Order.Status.ACCEPTED}:
            raise orders.Conflict("Promotion cannot change in this status.", code="invalid_transition")
        promo = PromoCode.objects.select_for_update().filter(code=code).first() if code else None
        if code:
            promo_available(promo, order)
        current = selected_promo(order)
        if (current.pk if current else None) == (promo.pk if promo else None):
            return order
        PromoSelection.objects.update_or_create(order=order, defaults={"promo": promo})
        order.price_quotes.filter(state=PriceQuote.State.OFFERED).update(
            state=PriceQuote.State.INVALIDATED, decided_at=timezone.now(), decided_by=actor
        )
        return orders.changed(order, actor, "promo_selected" if promo else "promo_removed", order.status)


def estimate_order(order, language=None):
    answers = order.answers
    result = estimate(
        order.service,
        answers.get("city", ""),
        answers.get("urgency", "regular"),
        answers.get("area_sqm"),
        language or order.language,
    )
    promo = selected_promo(order)
    result.update(
        promo_code=promo.code if promo else None,
        promo_status="not_selected",
        discount="0.00",
        total=result["subtotal"],
    )
    if promo:
        if result["subtotal"] is None:
            result["promo_status"] = "requires_quote"
        else:
            try:
                discount = promo_discount(promo, order, Decimal(result["subtotal"]))
                result.update(
                    promo_status="estimated",
                    discount=amount(discount),
                    total=amount(Decimal(result["subtotal"]) - discount),
                )
            except orders.Conflict:
                result["promo_status"] = "unavailable"
    return result


def quote_fingerprint(data):
    return hashlib.sha256(
        json.dumps(
            {k: v for k, v in data.items() if k != "expected_version"},
            sort_keys=True,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@transaction.atomic
def issue_quote(actor, order_id, data):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    order = get_object_or_404(orders.review_queryset().select_for_update(), pk=order_id)
    orders.supported(order)
    fingerprint = quote_fingerprint(data)
    existing = order.price_quotes.filter(client_request_id=data["client_request_id"]).first()
    if existing:
        if existing.request_fingerprint != fingerprint or existing.created_by_id != actor.pk:
            raise orders.Conflict("Request id has different terms.", code="idempotency_conflict")
        return existing, order, False
    orders.check_version(order, data["expected_version"])
    if order.status != Order.Status.ACCEPTED:
        raise orders.Conflict("Accept the order for negotiation first.", code="invalid_transition")
    if order.price_quotes.count() >= 50:
        raise orders.Conflict("Quote revision limit reached.", code="quote_limit")
    reserve_quota("quote-create", str(actor.pk), 30, 3600)
    now = timezone.now()
    if not now < data["expires_at"] <= now + timedelta(days=30):
        raise ValidationError({"expires_at": "Offer must expire within the next 30 days."})
    subtotal = sum(line["amount"] for line in data["lines"])
    appraisal = sum(line["amount"] for line in data["lines"] if line["kind"] == "appraisal")
    if appraisal <= 0 or subtotal > MAX_MONEY:
        raise ValidationError({"lines": "Include a positive appraisal fee; total is too large or invalid."})
    promo = selected_promo(order, lock=True)
    discount = promo_discount(promo, order, appraisal) if promo else Decimal("0.00")
    if promo and data["expires_at"] > promo.ends_at:
        raise ValidationError({"expires_at": "Offer with promotion must expire no later than the promotion."})
    snapshot = (
        {
            "code": promo.code,
            "version": promo.version,
            "kind": promo.kind,
            "value": amount(promo.value),
            "minimum_subtotal": amount(promo.minimum_subtotal),
            "maximum_discount": amount(promo.maximum_discount)
            if promo.maximum_discount is not None
            else None,
            "ends_at": promo.ends_at.isoformat(),
        }
        if promo
        else {}
    )
    lines = [{**line, "amount": amount(line["amount"])} for line in data["lines"]]
    order.price_quotes.filter(state=PriceQuote.State.OFFERED).update(
        state=PriceQuote.State.SUPERSEDED, decided_at=now, decided_by=actor
    )
    revision = (order.price_quotes.aggregate(v=Max("revision"))["v"] or 0) + 1
    quote = PriceQuote.objects.create(
        order=order,
        revision=revision,
        client_request_id=data["client_request_id"],
        request_fingerprint=fingerprint,
        issued_order_version=order.version + 1,
        created_by=actor,
        lines=lines,
        subtotal=subtotal,
        appraisal_subtotal=appraisal,
        discount=discount,
        total=subtotal - discount,
        terms=data["terms"],
        expires_at=data["expires_at"],
        promo=promo,
        promo_snapshot=snapshot,
        context_snapshot={
            "submission_id": str(order.submissions.last().pk) if order.submissions.exists() else None,
            "form_id": str(order.form_id),
            "form_checksum": order.form.checksum,
            "answers": deepcopy(order.answers),
            "asset": deepcopy(order.asset_snapshot),
            "service": deepcopy(order.service_snapshot),
        },
    )
    orders.changed(order, actor, "price_offered", order.status, str(quote.pk))
    return quote, order, True


@transaction.atomic
def decide_quote(
    actor, order_id, quote_id, expected_version, action, confirmed=False, comment="", review=False
):
    actor = orders.actor_for_write(actor, consent=(action == "accept"), reviewer=review)
    query = orders.review_queryset() if review else Order.objects.filter(owner=actor)
    order = get_object_or_404(query.select_for_update(), pk=order_id)
    orders.supported(order)
    quote = get_object_or_404(order.price_quotes, pk=quote_id)
    if review != (action == "withdraw"):
        raise PermissionDenied("This action is not available here.")
    if action == "accept" and not confirmed:
        raise ValidationError({"confirmed": "Explicit price confirmation is required."})
    # A lost successful response can safely be retried without consuming another promotion use.
    if (
        action == "accept"
        and quote.state == PriceQuote.State.ACCEPTED
        and order.status == Order.Status.PRICE_AGREED
    ):
        return quote, order
    orders.check_version(order, expected_version)
    if order.status != Order.Status.ACCEPTED or quote.state != PriceQuote.State.OFFERED:
        raise orders.Conflict("This price offer is no longer actionable.", code="invalid_transition")
    if action == "accept":
        if quote.expires_at <= timezone.now():
            raise orders.Conflict("Request a new offer from the appraiser.", code="quote_expired")
        if quote.issued_order_version != order.version:
            raise orders.Conflict("Order changed since this offer.", code="quote_stale")
        if quote.promo_id:
            promo = PromoCode.objects.select_for_update().get(pk=quote.promo_id)
            if promo.version != quote.promo_snapshot["version"]:
                raise orders.Conflict("Promotion changed; request a new offer.", code="promo_unavailable")
            discount = promo_discount(promo, order, quote.appraisal_subtotal)
            if discount != quote.discount:
                raise orders.Conflict("Promotion changed; request a new offer.", code="promo_unavailable")
            PromoRedemption.objects.create(order=order, quote=quote, promo=promo, user=actor)
        quote.state = PriceQuote.State.ACCEPTED
        before = order.status
        order.status = Order.Status.PRICE_AGREED
    else:
        before = order.status
        quote.state = PriceQuote.State.WITHDRAWN if review else PriceQuote.State.DECLINED
    quote.decided_at = timezone.now()
    quote.decided_by = actor
    quote.decision_comment = comment
    quote.save()
    orders.changed(order, actor, "price_" + action, before, f"{quote.pk}: {comment}")
    if action == "accept":
        from apps.payments.services import on_price_accepted

        on_price_accepted(order, actor)
    return quote, order


def close_order_pricing(order, actor):
    """Called only inside the existing order cancellation/rejection transaction. No bank/refund actions."""
    now = timezone.now()
    for redemption in PromoRedemption.objects.filter(order=order, released_at__isnull=True).order_by(
        "promo_id"
    ):
        PromoCode.objects.select_for_update().get(pk=redemption.promo_id)
        redemption.released_at = now
        redemption.save(update_fields=["released_at"])
    order.price_quotes.filter(state__in=[PriceQuote.State.OFFERED, PriceQuote.State.ACCEPTED]).update(
        state=PriceQuote.State.CANCELLED,
        decided_at=now,
        decided_by=actor,
        decision_comment="Order cancelled or rejected; no payment/refund is performed by pricing.",
    )
