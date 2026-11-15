import hashlib
import json
from copy import deepcopy

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.accounts.security import reserve_quota
from apps.catalog.models import Category, Service
from apps.catalog.schema import evaluate
from apps.legal.services import requires_consent

from .models import Asset, Order, OrderEvent, OrderSubmission


class Conflict(APIException):
    status_code = 409
    default_code = "conflict"
    default_detail = "The record changed. Reload it before trying again."


def actor_for_write(actor, consent=True, reviewer=False):
    # Serialize actor writes while allowing recipient foreign-key checks in other transactions.
    # PostgreSQL FOR NO KEY UPDATE prevents actor -> order -> recipient FK deadlocks.
    user = get_object_or_404(User.objects.select_for_update(no_key=True), pk=actor.pk, is_active=True)
    if reviewer and user.role not in {User.Role.APPRAISER, User.Role.ADMIN}:
        raise PermissionDenied("Reviewer role required.")
    if not user.email_verified_at:
        raise PermissionDenied("Verify your email first.", code="email_unverified")
    if consent and requires_consent(user):
        raise PermissionDenied("Accept current documents first.", code="consent_required")
    return user


def asset_snapshot(asset):
    return {
        "id": str(asset.id),
        "title": asset.title,
        "kind": asset.kind,
        "city": asset.city,
        "address": asset.address,
        "description": asset.description,
        "version": asset.version,
    }


def service_snapshot(service):
    return {
        "id": str(service.id),
        "slug": service.slug,
        "title": deepcopy(service.title),
        "category": service.category.slug,
    }


def check_version(obj, expected):
    if obj.version != expected:
        raise Conflict(code="version_conflict")


def supported(order):
    if order.is_test and not settings.ALLOW_ORDER_SIMULATION:
        raise PermissionDenied(
            "Test orders cannot be changed in this environment.", code="test_order_disabled"
        )


def current_service(service_id, form_id):
    service = get_object_or_404(Service.objects.select_for_update(), pk=service_id)
    category = Category.objects.select_for_update().get(pk=service.category_id)
    if not service.is_active or not category.is_active or not service.current_form_id:
        raise Conflict("This service is unavailable.", code="service_unavailable")
    form = service.current_form
    if form.id != form_id or form.service_id != service.id or not form.published_at:
        raise Conflict("Fetch the current form and review your answers.", code="stale_form")
    return service, form


def validate_answers(form, answers, complete=False):
    result = evaluate(form.definition, answers)
    errors = result["errors"]
    if not complete:
        errors = {k: v for k, v in errors.items() if v != "required"}
    if errors:
        raise ValidationError({"answers": errors})
    return result["cleaned_answers"]


def event(order, actor, action, before, message=""):
    record = OrderEvent.objects.create(
        order=order,
        version=order.version,
        actor=actor,
        actor_role=actor.role if actor else "system",
        action=action,
        from_status=before,
        to_status=order.status,
        message=message,
    )

    from apps.communications.services import CLOSED, close_requests
    from apps.notifications.services import notify_event

    if order.status in CLOSED:
        close_requests(order)
    notify_event(record)


def changed(order, actor, action, before, message=""):
    order.version += 1
    order.save()
    event(order, actor, action, before, message)
    return order


@transaction.atomic
def create_asset(actor, data):
    actor = actor_for_write(actor)
    if Asset.objects.filter(owner=actor, is_archived=False).count() >= settings.MAX_ACTIVE_ASSETS:
        raise Conflict("Archive unused assets before creating another.", code="asset_limit")
    reserve_quota("asset-create", str(actor.pk), 30, 3600)
    return Asset.objects.create(owner=actor, **data)


@transaction.atomic
def update_asset(actor, asset_id, data):
    actor = actor_for_write(actor)
    asset = get_object_or_404(Asset.objects.select_for_update(), pk=asset_id, owner=actor)
    data = dict(data)
    check_version(asset, data.pop("expected_version"))
    if asset.is_archived and data.get("is_archived") is False:
        if Asset.objects.filter(owner=actor, is_archived=False).count() >= settings.MAX_ACTIVE_ASSETS:
            raise Conflict("Active asset limit reached.", code="asset_limit")
    for key, value in data.items():
        setattr(asset, key, value)
    asset.version += 1
    asset.save()
    return asset


@transaction.atomic
def create_order(actor, data):
    actor = actor_for_write(actor)
    fingerprint = hashlib.sha256(
        json.dumps(
            data, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    existing = Order.objects.filter(owner=actor, client_request_id=data["client_request_id"]).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise Conflict("Request id already used with different data.", code="idempotency_conflict")
        return existing, False
    if (
        Order.objects.filter(owner=actor)
        .exclude(status__in=[Order.Status.CANCELLED, Order.Status.REJECTED, Order.Status.SERVICE_STOPPED])
        .count()
        >= settings.MAX_OPEN_ORDERS
    ):
        raise Conflict("Close unused orders before creating another.", code="order_limit")
    reserve_quota("order-create", str(actor.pk), 30, 3600)
    service, form = current_service(data["service_id"], data["form_id"])
    asset = get_object_or_404(Asset.objects.select_for_update(), pk=data["asset_id"], owner=actor)
    if asset.is_archived:
        raise Conflict("Unarchive the asset first.", code="asset_archived")
    answers = validate_answers(form, data["answers"])
    order = Order(
        owner=actor,
        asset=asset,
        service=service,
        form=form,
        language=data["language"],
        answers=answers,
        asset_snapshot=asset_snapshot(asset),
        service_snapshot=service_snapshot(service),
        client_request_id=data["client_request_id"],
        request_fingerprint=fingerprint,
    )
    # Full UUID suffix, not a short collision-prone counter; contains no personal data.
    order.number = "O360-" + order.id.hex.upper()
    order.save()
    event(order, actor, "created", "")
    return order, True


@transaction.atomic
def update_order(actor, order_id, data):
    actor = actor_for_write(actor)
    order = get_object_or_404(Order.objects.select_for_update(), pk=order_id, owner=actor)
    supported(order)
    data = dict(data)
    check_version(order, data.pop("expected_version"))
    if order.status not in {Order.Status.DRAFT, Order.Status.NEEDS_INFORMATION}:
        raise Conflict("Answers cannot be changed in this status.", code="invalid_transition")
    before = order.status
    if "form_id" in data:
        if order.status != Order.Status.DRAFT:
            raise Conflict("A submitted order keeps its original form.", code="form_pinned")
        _, form = current_service(order.service_id, data["form_id"])
        order.form = form
    if "answers" in data:
        # Replacement, not deep merge: removed/hidden answers must not linger.
        order.answers = validate_answers(order.form, data["answers"])
    elif "form_id" in data:
        order.answers = validate_answers(order.form, order.answers)
    return changed(order, actor, "answers_updated", before)


def snapshot_submission(order):
    from apps.materials.services import capture_manifest

    submission = OrderSubmission.objects.create(
        order=order,
        version=order.version,
        form=order.form,
        form_checksum=order.form.checksum,
        answers=deepcopy(order.answers),
        asset_snapshot=deepcopy(order.asset_snapshot),
        service_snapshot=deepcopy(order.service_snapshot),
    )

    capture_manifest(submission)
    from apps.pricing.models import SubmissionEstimate
    from apps.pricing.services import estimate_order

    SubmissionEstimate.objects.create(submission=submission, snapshot=estimate_order(order))


@transaction.atomic
def submit_order(actor, order_id, expected_version, confirmed):
    actor = actor_for_write(actor)
    order = get_object_or_404(Order.objects.select_for_update(), pk=order_id, owner=actor)
    supported(order)
    check_version(order, expected_version)
    if not confirmed:
        raise ValidationError({"confirmed": "Explicit confirmation is required."})
    before = order.status
    if before not in {Order.Status.DRAFT, Order.Status.NEEDS_INFORMATION}:
        raise Conflict("This order cannot be submitted in its current status.", code="invalid_transition")
    if before == Order.Status.DRAFT:
        service, form = current_service(order.service_id, order.form_id)
        asset = get_object_or_404(Asset.objects.select_for_update(), pk=order.asset_id, owner=actor)
        if asset.is_archived:
            raise Conflict("Unarchive the asset first.", code="asset_archived")
        order.form = form
        order.asset_snapshot = asset_snapshot(asset)
        order.service_snapshot = service_snapshot(service)
        order.status = Order.Status.AWAITING_PAYMENT
    else:
        if not order.review_unlocked_at:
            raise Conflict("Review is not unlocked.", code="payment_required")
        order.status = Order.Status.SUBMITTED
    order.answers = validate_answers(order.form, order.answers, complete=True)
    from apps.communications.services import require_answered
    from apps.materials.services import require_materials

    require_answered(order)
    require_materials(order)
    order.confirmed_at = timezone.now()
    changed(order, actor, "confirmed" if before == Order.Status.DRAFT else "resubmitted", before)
    snapshot_submission(order)
    return order


@transaction.atomic
def cancel_order(actor, order_id, expected_version, reason=""):
    # Cancellation remains possible if legal documents changed after the order was created.
    actor = actor_for_write(actor, consent=False)
    order = get_object_or_404(Order.objects.select_for_update(), pk=order_id, owner=actor)
    supported(order)
    check_version(order, expected_version)
    cancellable = {
        Order.Status.DRAFT,
        Order.Status.AWAITING_PAYMENT,
        Order.Status.SUBMITTED,
        Order.Status.UNDER_REVIEW,
        Order.Status.NEEDS_INFORMATION,
        Order.Status.ACCEPTED,
        Order.Status.PRICE_AGREED,
        Order.Status.INSPECTION_SCHEDULED,
    }
    if order.status not in cancellable:
        raise Conflict("Cancellation is not available in this status.", code="invalid_transition")
    before = order.status
    order.status = Order.Status.CANCELLED
    order = changed(order, actor, "cancelled", before, reason)
    from apps.pricing.services import close_order_pricing
    from apps.scheduling.services import close_order_schedule

    close_order_schedule(order, actor)
    close_order_pricing(order, actor)
    from apps.payments.services import on_order_closed

    on_order_closed(order, actor)
    return order


def review_queryset():
    query = Order.objects.filter(review_unlocked_at__isnull=False)
    if not settings.ALLOW_ORDER_SIMULATION:
        query = query.filter(is_test=False)
    return query


@transaction.atomic
def review_action(actor, order_id, expected_version, action, message=""):
    actor = actor_for_write(actor, consent=False, reviewer=True)
    order = get_object_or_404(review_queryset().select_for_update(), pk=order_id)
    supported(order)
    check_version(order, expected_version)
    transitions = {
        "start_review": ({Order.Status.SUBMITTED}, Order.Status.UNDER_REVIEW),
        "request_information": ({Order.Status.UNDER_REVIEW}, Order.Status.NEEDS_INFORMATION),
        "accept": ({Order.Status.UNDER_REVIEW}, Order.Status.ACCEPTED),
        "reject": (
            {
                Order.Status.SUBMITTED,
                Order.Status.UNDER_REVIEW,
                Order.Status.NEEDS_INFORMATION,
                Order.Status.ACCEPTED,
                Order.Status.PRICE_AGREED,
                Order.Status.INSPECTION_SCHEDULED,
            },
            Order.Status.REJECTED,
        ),
    }
    allowed, target = transitions[action]
    if order.status not in allowed:
        raise Conflict("Action is not available in this status.", code="invalid_transition")
    if action in {"request_information", "reject"} and not message.strip():
        raise ValidationError({"message": "Explain what is missing or why the request was rejected."})
    if action == "accept":
        from apps.communications.services import require_resolved

        require_resolved(order)
    before = order.status
    order.status = target
    order = changed(order, actor, action, before, message)
    if action == "reject":
        from apps.pricing.services import close_order_pricing
        from apps.scheduling.services import close_order_schedule

        close_order_schedule(order, actor)
        close_order_pricing(order, actor)
        from apps.payments.services import on_order_closed

        on_order_closed(order, actor)
    return order


@transaction.atomic
def simulate_payment(order_id):
    # Only a developer command, never an HTTP endpoint or production environment toggle.
    if not settings.ALLOW_ORDER_SIMULATION:
        raise PermissionDenied("Order simulation is disabled in this environment.")
    order = get_object_or_404(Order.objects.select_for_update(), pk=order_id)
    if order.status != Order.Status.AWAITING_PAYMENT:
        raise Conflict("Only awaiting_payment orders can be simulated.", code="invalid_transition")
    if order.payment_intents.exists():
        raise Conflict("Use the payment-ledger simulation command for this order.")
    before = order.status
    order.is_test = True
    order.payment_status = Order.PaymentStatus.TEST_CONFIRMED
    order.review_unlocked_at = timezone.now()
    order.status = Order.Status.SUBMITTED
    return changed(order, None, "payment_simulated", before, "LOCAL TEST: no money was charged or received.")
