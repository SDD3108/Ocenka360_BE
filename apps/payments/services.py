import hashlib
import json
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.security import reserve_quota
from apps.orders import services as orders
from apps.orders.models import Order
from apps.pricing.models import MAX_MONEY, PriceQuote

from . import providers
from .models import (
    CancellationCase,
    InvoiceSequence,
    PaymentAccount,
    PaymentAudit,
    PaymentIntent,
    Receipt,
    Refund,
    RefundEntry,
)

ZERO = Decimal("0.00")
CLOSED = {Order.Status.CANCELLED, Order.Status.REJECTED, Order.Status.SERVICE_STOPPED}
AFTER_START = {Order.Status.IN_PROGRESS, Order.Status.AWAITING_REPORT}


def default_advance():
    try:
        value = Decimal(settings.PAYMENT_ADVANCE_KZT)
        if (
            not value.is_finite()
            or value <= 0
            or value > MAX_MONEY
            or value != value.quantize(Decimal(".01"))
        ):
            raise ValueError
        return value
    except (ValueError, InvalidOperation):
        raise providers.ProviderUnavailable("Invalid advance configuration.") from None


def audit(order, actor, action, reference=None):
    PaymentAudit.objects.create(order=order, actor=actor, action=action, reference_id=reference)


def get_order(actor, order_id, review=False, lock=False, queue=True):
    if review:
        if actor.role not in {"appraiser", "admin"}:
            raise PermissionDenied("Reviewer role required.")
        query = orders.review_queryset() if queue else Order.objects.all()
        if not settings.ALLOW_ORDER_SIMULATION:
            query = query.filter(is_test=False)
    else:
        query = Order.objects.filter(owner=actor)
    return get_object_or_404(query.select_for_update() if lock else query, pk=order_id)


def total(query):
    return (query.aggregate(v=Sum("amount"))["v"] or ZERO).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)


def balance(order):
    account = PaymentAccount.objects.filter(order=order).first()
    advance = account.advance_amount if account else default_advance()
    receipts = Receipt.objects.filter(intent__order=order)
    paid = total(receipts)
    refunded = total(RefundEntry.objects.filter(refund__receipt__intent__order=order))
    reserved = total(Refund.objects.filter(receipt__intent__order=order).exclude(state="completed"))
    net = paid - refunded
    quote = order.price_quotes.filter(state=PriceQuote.State.ACCEPTED).first()
    price = quote.total if quote else None
    if order.status in CLOSED:
        case = order.cancellation_cases.filter(state="accepted").first()
        target = case.retained_amount if case else ZERO
    else:
        target = price
    due = max(ZERO, (advance if order.status == Order.Status.AWAITING_PAYMENT else target or ZERO) - net)
    if order.status in CLOSED:
        due = ZERO
    return {
        "currency": "KZT",
        "advance_amount": advance,
        "agreed_total": price,
        "retained_amount": target if order.status in CLOSED else None,
        "received": paid,
        "refunded": refunded,
        "net_paid": net,
        "refund_pending": reserved,
        "amount_due": due,
        "overpayment": max(ZERO, net - target) if target is not None else ZERO,
        "is_test": order.is_test,
        "legacy_simulation": order.is_test and not order.payment_intents.exists(),
        "fiscal_status": "not_connected",
    }


def update_payment_status(order):
    b = balance(order)
    if b["received"] == 0:
        return  # Never invent payments for legacy Part 4 simulation.
    if order.is_test:
        order.payment_status = Order.PaymentStatus.TEST_CONFIRMED
    elif b["net_paid"] == 0:
        order.payment_status = Order.PaymentStatus.REFUNDED
    elif b["agreed_total"] is not None and b["net_paid"] >= b["agreed_total"]:
        order.payment_status = Order.PaymentStatus.PAID
    else:
        order.payment_status = Order.PaymentStatus.PARTIALLY_PAID
    order.save(update_fields=["payment_status"])


def queue_excess_refunds(order, actor=None):
    """Order lock is held. Reserve the excess once; this function NEVER sends money."""
    b = balance(order)
    remaining = b["overpayment"] - b["refund_pending"]
    if remaining <= 0:
        return
    for receipt in Receipt.objects.filter(intent__order=order).order_by("created_at", "id"):
        available = receipt.amount - total(receipt.refunds.all())
        amount = min(remaining, available)
        if amount <= 0:
            continue
        refund = Refund.objects.create(
            receipt=receipt,
            amount=amount,
            created_by=actor,
            reason="Order closed" if order.status in CLOSED else "Payment exceeds agreed price",
        )
        audit(order, actor, "refund_approved", refund.pk)
        remaining -= amount
        if remaining <= 0:
            break


def on_order_closed(order, actor):
    # Must be called in existing transaction after calendar/promo locks, never a bank API call.
    order.payment_intents.filter(state="pending").update(state="closed")
    queue_excess_refunds(order, actor)
    update_payment_status(order)


def on_price_accepted(order, actor):
    queue_excess_refunds(order, actor)
    update_payment_status(order)


def require_funded(order):
    # Legacy local simulations remain explicit non-financial fixtures, not migrated fake receipts.
    if order.is_test and settings.ALLOW_ORDER_SIMULATION and not order.payment_intents.exists():
        return
    b = balance(order)
    if b["agreed_total"] is None or b["net_paid"] - b["refund_pending"] < b["agreed_total"]:
        raise orders.Conflict("Pay the agreed balance before work starts.", code="payment_required")
    if (
        order.payment_intents.filter(needs_review=True).exists()
        or order.cancellation_cases.filter(state__in=["requested", "offered"]).exists()
    ):
        raise orders.Conflict(
            "Resolve the payment or cancellation review first.", code="payment_requires_review"
        )


def invoice_number():
    sequence = InvoiceSequence.objects.select_for_update().get(pk=1)
    if sequence.next_value > 999999:
        raise providers.ProviderUnavailable(
            "Invoice range exhausted; contact the acquiring bank before allocating a new range."
        )
    value = str(sequence.next_value)
    sequence.next_value += 1
    sequence.save(update_fields=["next_value"])
    return value


@transaction.atomic
def create_intent(actor, order_id, data):
    actor = orders.actor_for_write(actor)
    order = get_order(actor, order_id, lock=True)
    orders.supported(order)
    fingerprint = hashlib.sha256(
        json.dumps({k: str(v) for k, v in data.items() if k != "expected_version"}, sort_keys=True).encode()
    ).hexdigest()
    existing = order.payment_intents.filter(client_request_id=data["client_request_id"]).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise orders.Conflict("Request id used with different data.", code="idempotency_conflict")
        return existing, order, False
    orders.check_version(order, data["expected_version"])
    if not providers.available_methods().get(data["provider"]):
        raise providers.ProviderUnavailable()
    purpose = data["purpose"]
    if purpose == "advance" and order.status != Order.Status.AWAITING_PAYMENT:
        raise orders.Conflict("The advance is only payable after submitting the order.")
    if purpose == "balance" and order.status not in {
        Order.Status.PRICE_AGREED,
        Order.Status.INSPECTION_SCHEDULED,
    }:
        raise orders.Conflict("Agree the final price first.")
    if order.cancellation_cases.filter(state__in=["requested", "offered"]).exists():
        raise orders.Conflict("Cancellation review is open.")
    if order.payment_intents.filter(needs_review=True).exists():
        raise orders.Conflict("Resolve payment reconciliation before another attempt.")
    if order.payment_intents.count() >= 100:
        raise orders.Conflict("Payment attempt limit reached.")
    reserve_quota("payment-create", str(actor.pk), 20, 3600)
    PaymentAccount.objects.get_or_create(order=order, defaults={"advance_amount": default_advance()})
    amount = balance(order)["amount_due"]
    if amount <= 0:
        raise orders.Conflict("No amount is due.", code="nothing_to_pay")
    now = timezone.now()
    order.payment_intents.filter(state="pending", expires_at__lte=now).update(state="closed")
    if order.payment_intents.filter(state="pending").exists():
        raise orders.Conflict("Use the existing pending payment.", code="payment_pending")
    provider = data["provider"]
    environment = (
        "local" if provider == "mock" else settings.HALYK_ENVIRONMENT if provider == "halyk" else "live"
    )
    is_test = environment != "live"
    if (
        order.is_test
        and not is_test
        or (is_test and order.payment_intents.filter(environment="live").exists())
    ):
        raise orders.Conflict("Test and real payments cannot be mixed.")
    if is_test and not settings.ALLOW_ORDER_SIMULATION:
        raise PermissionDenied("Test payments are disabled.")
    if is_test:
        order.is_test = True
    intent = PaymentIntent.objects.create(
        order=order,
        client_request_id=data["client_request_id"],
        request_fingerprint=fingerprint,
        provider=provider,
        environment=environment,
        purpose=purpose,
        amount=amount,
        terminal_id=settings.HALYK_TERMINAL_ID if provider == "halyk" else "",
        invoice_id=invoice_number() if provider == "halyk" else None,
        expires_at=now + timedelta(hours=2),
    )
    orders.changed(order, actor, "payment_requested", order.status, str(intent.pk))
    audit(order, actor, "payment_requested", intent.pk)
    return intent, order, True


def checkout(actor, order_id, intent_id):
    with transaction.atomic():
        actor = orders.actor_for_write(actor)
        order = get_order(actor, order_id, lock=True)
        orders.supported(order)
        intent = get_object_or_404(order.payment_intents, pk=intent_id)
        if intent.state != "pending" or intent.expires_at <= timezone.now() or order.status in CLOSED:
            raise orders.Conflict("Payment is no longer offered.")
        reserve_quota("payment-checkout", str(actor.pk), 30, 3600)
    # Network outside DB locks. A simultaneous cancellation is handled as a late payment/refund.
    if intent.provider == "mock":
        raise providers.ProviderUnavailable(
            "Use the local simulation command; there is no browser payment endpoint for mock."
        )
    if intent.provider == "kaspi_manual":
        if not providers.kaspi_configured():
            raise providers.ProviderUnavailable()
        return {
            "kind": "kaspi_manual",
            "url": settings.KASPI_PAYMENT_URL,
            "amount": str(intent.amount),
            "reference": str(intent.pk),
            "amount_prefilled": False,
            "verification": "manual_bank_review",
            "is_test": False,
        }
    return providers.Halyk(intent).checkout()


def record_receipt(order, intent, transaction_id, verification, actor=None, actual_amount=None):
    existing = Receipt.objects.filter(intent=intent).first()
    if existing:
        if existing.transaction_id != transaction_id:
            raise providers.ProviderMismatch()
        return existing, False
    try:
        with transaction.atomic():
            receipt = Receipt.objects.create(
                intent=intent,
                provider=intent.provider,
                environment=intent.environment,
                transaction_id=transaction_id,
                amount=intent.amount if actual_amount is None else actual_amount,
                verification=verification,
                verified_by=actor,
            )
    except IntegrityError:
        raise orders.Conflict(
            "Bank transaction already allocated to another payment.", code="duplicate_bank_reference"
        ) from None
    before = order.status
    intent.state = "paid"
    intent.needs_review = False
    intent.save(update_fields=["state", "needs_review"])
    if (
        order.status == Order.Status.AWAITING_PAYMENT
        and balance(order)["net_paid"] >= balance(order)["advance_amount"]
    ):
        order.status = Order.Status.SUBMITTED
        order.review_unlocked_at = timezone.now()
    update_payment_status(order)
    orders.changed(order, actor, "payment_recorded", before, str(receipt.pk))
    audit(order, actor, "payment_recorded", receipt.pk)
    queue_excess_refunds(order, actor)
    return receipt, True


@transaction.atomic
def confirm_manual(actor, order_id, intent_id, expected_version, transaction_id, amount, confirmed_in_bank):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    # An unpaid order is deliberately visible ONLY via this finance endpoint, not the work queue.
    order = get_order(actor, order_id, review=True, lock=True, queue=False)
    intent = get_object_or_404(
        order.payment_intents, pk=intent_id, provider="kaspi_manual", environment="live"
    )
    if not confirmed_in_bank or amount <= ZERO or amount > MAX_MONEY:
        raise ValidationError("Confirm the actual positive amount received in the merchant bank account.")
    existing = Receipt.objects.filter(intent=intent).first()
    if existing and existing.transaction_id == transaction_id:
        if existing.amount != amount:
            raise orders.Conflict("Recorded receipt amount cannot be changed.")
        return existing, order
    orders.check_version(order, expected_version)
    receipt, _ = record_receipt(
        order, intent, transaction_id, "manual_bank_review", actor, actual_amount=amount
    )
    return receipt, order


@transaction.atomic
def simulate_payment(intent_id):
    if not settings.ALLOW_ORDER_SIMULATION:
        raise PermissionDenied("Simulation is disabled.")
    intent_info = get_object_or_404(PaymentIntent, pk=intent_id, provider="mock", environment="local")
    order = get_object_or_404(Order.objects.select_for_update(), pk=intent_info.order_id, is_test=True)
    intent = PaymentIntent.objects.get(pk=intent_id)
    receipt, _ = record_receipt(order, intent, "mock-" + str(intent.pk), "simulation")
    return receipt


def settle_refund(order, refund, reference, verification, actor=None):
    existing = RefundEntry.objects.filter(refund=refund).first()
    if existing:
        if existing.transaction_id != reference:
            raise orders.Conflict("Refund already recorded under another reference.")
        return existing
    receipt = refund.receipt
    if total(RefundEntry.objects.filter(refund__receipt=receipt)) + refund.amount > receipt.amount:
        raise orders.Conflict("Refund would exceed received amount.")
    try:
        with transaction.atomic():
            entry = RefundEntry.objects.create(
                refund=refund,
                provider=receipt.provider,
                environment=receipt.environment,
                transaction_id=reference,
                amount=refund.amount,
                verification=verification,
                verified_by=actor,
            )
    except IntegrityError:
        raise orders.Conflict("Refund bank reference already used.") from None
    refund.state = "completed"
    refund.save(update_fields=["state", "updated_at"])
    update_payment_status(order)
    orders.changed(order, actor, "refund_recorded", order.status, str(refund.pk))
    audit(order, actor, "refund_recorded", refund.pk)
    return entry


@transaction.atomic
def confirm_manual_refund(
    actor, order_id, refund_id, expected_version, transaction_id, amount, confirmed_in_bank
):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    order = get_order(actor, order_id, review=True, lock=True, queue=False)
    refund = get_object_or_404(
        Refund.objects.select_related("receipt"), pk=refund_id, receipt__intent__order=order
    )
    if refund.receipt.environment != "live" or not confirmed_in_bank or amount != refund.amount:
        raise ValidationError("Confirm an actual bank refund and its exact amount.")
    existing = RefundEntry.objects.filter(refund=refund).first()
    if existing and existing.transaction_id == transaction_id:
        return existing, order
    orders.check_version(order, expected_version)
    if refund.state == "processing":
        raise orders.Conflict("Wait for the bank request to finish before manual reconciliation.")
    return settle_refund(order, refund, transaction_id, "manual_bank_review", actor), order


@transaction.atomic
def simulate_refund(refund_id):
    if not settings.ALLOW_ORDER_SIMULATION:
        raise PermissionDenied("Simulation is disabled.")
    info = get_object_or_404(
        Refund.objects.select_related("receipt__intent"),
        pk=refund_id,
        receipt__provider="mock",
        receipt__environment="local",
    )
    order = Order.objects.select_for_update().get(pk=info.receipt.intent.order_id, is_test=True)
    refund = Refund.objects.select_related("receipt").get(pk=refund_id)
    return settle_refund(order, refund, "mock-refund-" + str(refund.pk), "simulation")


def refresh_intent(intent_id):
    intent = get_object_or_404(PaymentIntent.objects.select_related("order"), pk=intent_id, provider="halyk")
    try:
        evidence = providers.Halyk(intent).status()
    except providers.ProviderMismatch:
        with transaction.atomic():
            Order.objects.select_for_update().get(pk=intent.order_id)
            PaymentIntent.objects.filter(pk=intent.pk).update(
                needs_review=True, last_checked_at=timezone.now()
            )
        raise
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=intent.order_id)
        intent = PaymentIntent.objects.get(pk=intent_id)
        intent.last_checked_at = timezone.now()
        intent.sync_requested_at = None
        intent.save(update_fields=["last_checked_at", "sync_requested_at"])
        if evidence["state"] == "paid":
            existing = Receipt.objects.filter(intent=intent).first()
            if existing and existing.transaction_id != evidence["transaction_id"]:
                intent.needs_review = True
                intent.save(update_fields=["needs_review"])
            elif not existing:
                try:
                    with transaction.atomic():
                        record_receipt(order, intent, evidence["transaction_id"], "bank_api")
                except (providers.ProviderMismatch, orders.Conflict):
                    intent.needs_review = True
                    intent.save(update_fields=["needs_review"])
        elif evidence["state"] == "refunded":
            receipt = Receipt.objects.filter(intent=intent, transaction_id=evidence["transaction_id"]).first()
            refunds = list(receipt.refunds.all()) if receipt else []
            # Only a single full refund we initiated is unambiguous in this endpoint.
            if (
                len(refunds) == 1
                and refunds[0].amount == receipt.amount
                and refunds[0].state in {"processing", "submitted", "unknown", "completed"}
            ):
                if refunds[0].state != "completed":
                    settle_refund(order, refunds[0], "halyk-full-" + receipt.transaction_id, "bank_api")
            else:
                intent.needs_review = True
                intent.save(update_fields=["needs_review"])
        return intent, order


def refresh_for_actor(actor, order_id, intent_id, review=False):
    with transaction.atomic():
        actor = orders.actor_for_write(actor, consent=False, reviewer=review)
        order = get_order(actor, order_id, review=review, lock=True, queue=False)
        orders.supported(order)
        get_object_or_404(order.payment_intents, pk=intent_id, provider="halyk")
        reserve_quota("payment-sync", str(actor.pk), 20, 3600)
    return refresh_intent(intent_id)


def execute_refund(actor, order_id, refund_id, expected_version, confirmed):
    if not confirmed:
        raise ValidationError({"confirmed": "Explicit refund confirmation is required."})
    with transaction.atomic():
        actor = orders.actor_for_write(actor, consent=False, reviewer=True)
        order = get_order(actor, order_id, review=True, lock=True, queue=False)
        orders.supported(order)
        orders.check_version(order, expected_version)
        refund = get_object_or_404(
            Refund.objects.select_related("receipt__intent__order"),
            pk=refund_id,
            receipt__intent__order=order,
        )
        if refund.state != "approved":
            raise orders.Conflict("Already submitted or uncertain; reconcile before retrying.")
        adapter = providers.Halyk(refund.receipt.intent)
        if (
            refund.amount != refund.receipt.amount
            or refund.receipt.refunds.exclude(pk=refund.pk).exists()
            or refund.amount < Decimal("10.00")
        ):
            raise orders.Conflict(
                "This refund requires bank-cabinet execution and explicit manual verification."
            )
        refund.state = "processing"
        refund.save(update_fields=["state", "updated_at"])
        orders.changed(order, actor, "refund_requested", order.status, str(refund.pk))
        audit(order, actor, "refund_requested", refund.pk)
    # Never auto-retry this POST on timeout. A crash leaves processing visible for reconciliation.
    try:
        adapter.refund_full(refund)
        state = "submitted"
    except providers.ProviderUnavailable:
        state = "unknown"
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        refund = Refund.objects.get(pk=refund_id)
        if refund.state == "processing":
            refund.state = state
            refund.save(update_fields=["state", "updated_at"])
            audit(order, actor, "refund_" + state, refund.pk)
        return refund, order


@transaction.atomic
def request_cancellation(actor, order_id, expected_version, client_request_id, reason):
    actor = orders.actor_for_write(actor, consent=False)
    order = get_order(actor, order_id, lock=True)
    orders.supported(order)
    existing = order.cancellation_cases.filter(client_request_id=client_request_id).first()
    if existing:
        if existing.reason != reason:
            raise orders.Conflict("Request id used with different data.")
        return existing, order, False
    orders.check_version(order, expected_version)
    if order.status not in AFTER_START:
        raise orders.Conflict("Before work starts use the ordinary order cancellation endpoint.")
    if order.cancellation_cases.filter(state__in=["requested", "offered"]).exists():
        raise orders.Conflict("An open cancellation request already exists.")
    reserve_quota("cancellation-case", str(actor.pk), 10, 3600)
    if order.cancellation_cases.count() >= 30:
        raise orders.Conflict("Cancellation request limit reached.")
    case = CancellationCase.objects.create(order=order, client_request_id=client_request_id, reason=reason)
    orders.changed(order, actor, "cancellation_requested", order.status, str(case.pk))
    audit(order, actor, "cancellation_requested", case.pk)
    return case, order, True


@transaction.atomic
def offer_cancellation(actor, order_id, case_id, expected_version, retained_amount, comment):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    order = get_order(actor, order_id, review=True, lock=True)
    orders.supported(order)
    orders.check_version(order, expected_version)
    case = get_object_or_404(order.cancellation_cases, pk=case_id)
    if order.status not in AFTER_START or case.state not in {"requested", "offered"}:
        raise orders.Conflict("Cancellation cannot be offered in this state.")
    b = balance(order)
    if retained_amount > min(b["net_paid"] - b["refund_pending"], b["agreed_total"] or ZERO):
        raise ValidationError({"retained_amount": "Cannot exceed available paid funds or agreed price."})
    case.retained_amount = retained_amount
    case.review_comment = comment
    case.reviewed_by = actor
    case.state = "offered"
    case.save()
    orders.changed(order, actor, "cancellation_offered", order.status, str(case.pk))
    audit(order, actor, "cancellation_offered", case.pk)
    return case, order


@transaction.atomic
def decide_cancellation(actor, order_id, case_id, expected_version, action, confirmed=False):
    actor = orders.actor_for_write(actor, consent=False)
    order = get_order(actor, order_id, lock=True)
    orders.supported(order)
    orders.check_version(order, expected_version)
    case = get_object_or_404(order.cancellation_cases, pk=case_id)
    if order.status not in AFTER_START or case.state not in {"requested", "offered"}:
        raise orders.Conflict("Cancellation is no longer actionable.")
    before = order.status
    if action == "accept":
        if case.state != "offered" or not confirmed:
            raise ValidationError("Explicit agreement with the offered retained amount is required.")
        from apps.pricing.services import close_order_pricing
        from apps.scheduling.models import Appointment, ScheduleEvent
        from apps.scheduling.services import lock_calendar

        lock_calendar()
        order.appointments.filter(state__in=["requested", "offered", "confirmed", "in_progress"]).update(
            state=Appointment.State.CANCELLED
        )
        ScheduleEvent.objects.create(
            order=order,
            actor=actor,
            action="service_stopped",
            snapshot={"cancellation_case_id": str(case.pk)},
        )
        close_order_pricing(order, actor)
        order.status = Order.Status.SERVICE_STOPPED
        case.state = "accepted"
    else:
        case.state = "withdrawn"
    case.decided_at = timezone.now()
    case.save()
    orders.changed(order, actor, "cancellation_" + action, before, str(case.pk))
    audit(order, actor, "cancellation_" + action, case.pk)
    if action == "accept":
        on_order_closed(order, actor)
    return case, order
