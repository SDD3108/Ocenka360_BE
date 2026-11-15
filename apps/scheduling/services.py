import hashlib
import json
from copy import copy
from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.security import reserve_quota
from apps.materials import services as materials
from apps.materials.models import Material
from apps.materials.storage import PrivateStorage
from apps.orders import services as orders
from apps.orders.models import Order
from apps.pricing.models import PriceQuote

from .models import Appointment, CalendarBlock, CalendarLock, Inspection, InspectionMaterial, ScheduleEvent

PENDING = [Appointment.State.REQUESTED, Appointment.State.OFFERED]
ACTIVE = [Appointment.State.CONFIRMED, Appointment.State.IN_PROGRESS]
SCHEDULABLE = [Order.Status.PRICE_AGREED, Order.Status.INSPECTION_SCHEDULED]


def fingerprint(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def lock_calendar():
    # Migration guarantees row 1 exists. A missing row fails closed, never an unlocked booking.
    CalendarLock.objects.select_for_update().get(pk=1)


def locked_order(actor, order_id, review, consent=True):
    actor = orders.actor_for_write(actor, consent=consent and not review, reviewer=review)
    order = materials.get_order(actor, order_id, review=review, lock=True)
    orders.supported(order)
    return actor, order


def require_schedulable(order):
    if order.status not in SCHEDULABLE:
        raise orders.Conflict(
            "Agree the price before scheduling; work must not have started.", code="invalid_transition"
        )
    if not order.price_quotes.filter(state=PriceQuote.State.ACCEPTED).exists():
        raise orders.Conflict("An accepted price quote is required.", code="price_required")


def validate_interval(starts_at, ends_at, max_hours=12, future=True):
    if timezone.is_naive(starts_at) or timezone.is_naive(ends_at):
        raise ValidationError("An explicit UTC offset is required.")
    if not timedelta(0) < ends_at - starts_at <= timedelta(hours=max_hours):
        raise ValidationError({"ends_at": f"Interval must be positive and at most {max_hours} hours."})
    if future and not timezone.now() < starts_at <= timezone.now() + timedelta(days=180):
        raise ValidationError({"starts_at": "Choose a future time within 180 days."})


def ensure_free(start, end, exclude_order=None):
    query = Appointment.objects.filter(
        state__in=ACTIVE + [Appointment.State.COMPLETED], busy_from__lt=end, busy_until__gt=start
    )
    if exclude_order:
        query = query.exclude(order=exclude_order)
    if (
        query.exists()
        or CalendarBlock.objects.filter(
            cancelled_at__isnull=True, starts_at__lt=end, ends_at__gt=start
        ).exists()
    ):
        # No other customer's identity, address or schedule is exposed in the conflict response.
        raise orders.Conflict("The appraiser is unavailable for this interval.", code="calendar_conflict")


def snapshot(appointment):
    return {
        "id": str(appointment.pk),
        "state": appointment.state,
        "mode": appointment.mode,
        "starts_at": appointment.starts_at.isoformat(),
        "ends_at": appointment.ends_at.isoformat(),
        "busy_from": appointment.busy_from.isoformat(),
        "busy_until": appointment.busy_until.isoformat(),
        "report_hours": appointment.report_hours,
        "note": appointment.note,
    }


def record(actor, action, order=None, appointment=None, extra=None):
    ScheduleEvent.objects.create(
        actor=actor,
        order=order,
        action=action,
        snapshot={"appointment": snapshot(appointment) if appointment else None, **(extra or {})},
    )
    if order:
        orders.changed(order, actor, action, order._schedule_before)


def prepare_change(order, expected_version):
    orders.check_version(order, expected_version)
    require_schedulable(order)
    order._schedule_before = order.status


def set_buffers(appointment, before, after):
    appointment.busy_from = appointment.starts_at - timedelta(minutes=before)
    appointment.busy_until = appointment.ends_at + timedelta(minutes=after)


@transaction.atomic
def propose(actor, order_id, data, review=False):
    actor, order = locked_order(actor, order_id, review)
    original = {k: v for k, v in data.items() if k != "expected_version"}
    digest = fingerprint(original)
    existing = order.appointments.filter(client_request_id=data["client_request_id"]).first()
    if existing:
        if existing.created_by_id != actor.pk or existing.request_fingerprint != digest:
            raise orders.Conflict("Request id already used with other data.", code="idempotency_conflict")
        return existing, order, False
    prepare_change(order, data["expected_version"])
    validate_interval(data["starts_at"], data["ends_at"])
    if order.appointments.count() >= 100:
        raise orders.Conflict("Scheduling revision limit reached.", code="schedule_limit")
    reserve_quota("schedule-propose", str(actor.pk), 30, 3600)
    lock_calendar()
    appointment = Appointment(
        order=order,
        created_by=actor,
        client_request_id=data["client_request_id"],
        request_fingerprint=digest,
        mode=data["mode"],
        state="offered" if review else "requested",
        starts_at=data["starts_at"],
        ends_at=data["ends_at"],
        report_hours=data.get("report_hours"),
        note=data.get("note", ""),
    )
    set_buffers(appointment, data.get("buffer_before_minutes", 0), data.get("buffer_after_minutes", 0))
    if review:
        ensure_free(appointment.busy_from, appointment.busy_until, order)
    # A pending alternative never releases the existing confirmed booking.
    order.appointments.filter(state__in=PENDING).update(state=Appointment.State.SUPERSEDED)
    appointment.save()
    record(actor, "inspection_offered" if review else "inspection_requested", order, appointment)
    return appointment, order, True


def activate(actor, order, appointment):
    validate_interval(appointment.starts_at, appointment.ends_at)
    ensure_free(appointment.busy_from, appointment.busy_until, order)
    order.appointments.filter(state=Appointment.State.CONFIRMED).exclude(pk=appointment.pk).update(
        state=Appointment.State.SUPERSEDED
    )
    appointment.state = Appointment.State.CONFIRMED
    appointment.confirmed_at = timezone.now()
    appointment.save()
    order.status = Order.Status.INSPECTION_SCHEDULED
    record(actor, "inspection_confirmed", order, appointment)
    return appointment, order


@transaction.atomic
def confirm_request(actor, order_id, appointment_id, data):
    actor, order = locked_order(actor, order_id, True)
    prepare_change(order, data["expected_version"])
    appointment = get_object_or_404(order.appointments, pk=appointment_id)
    if appointment.state != Appointment.State.REQUESTED:
        raise orders.Conflict("Only the current client request can be confirmed.", code="invalid_transition")
    validate_interval(data["starts_at"], data["ends_at"])
    if not appointment.starts_at <= data["starts_at"] < data["ends_at"] <= appointment.ends_at:
        raise ValidationError("Choose a time inside the client's interval or make a new offer.")
    lock_calendar()
    appointment.starts_at = data["starts_at"]
    appointment.ends_at = data["ends_at"]
    appointment.report_hours = data["report_hours"]
    set_buffers(appointment, data["buffer_before_minutes"], data["buffer_after_minutes"])
    return activate(actor, order, appointment)


@transaction.atomic
def accept_offer(actor, order_id, appointment_id, expected_version, confirmed):
    actor, order = locked_order(actor, order_id, False)
    prepare_change(order, expected_version)
    if not confirmed:
        raise ValidationError({"confirmed": "Explicit agreement is required."})
    appointment = get_object_or_404(order.appointments, pk=appointment_id)
    if appointment.state != Appointment.State.OFFERED:
        raise orders.Conflict("Only a current appraiser offer can be accepted.", code="invalid_transition")
    lock_calendar()
    return activate(actor, order, appointment)


@transaction.atomic
def cancel_appointment(actor, order_id, appointment_id, expected_version, reason, review=False):
    actor, order = locked_order(actor, order_id, review, consent=False)
    prepare_change(order, expected_version)
    appointment = get_object_or_404(order.appointments, pk=appointment_id)
    if appointment.state not in PENDING + [Appointment.State.CONFIRMED]:
        raise orders.Conflict("This appointment can no longer be cancelled here.", code="invalid_transition")
    lock_calendar()
    if appointment.state == Appointment.State.CONFIRMED:
        order.appointments.filter(state__in=PENDING).update(state=Appointment.State.CANCELLED)
        order.status = Order.Status.PRICE_AGREED
    appointment.state = Appointment.State.CANCELLED
    appointment.save(update_fields=["state"])
    record(actor, "inspection_cancelled", order, appointment, {"reason": reason})
    return appointment, order


def close_order_schedule(order, actor):
    """Called by order cancellation/rejection inside its transaction, before promo locks."""
    ids = list(
        order.appointments.filter(state__in=PENDING + [Appointment.State.CONFIRMED]).values_list(
            "id", flat=True
        )
    )
    if ids:
        lock_calendar()
        order.appointments.filter(pk__in=ids).update(state=Appointment.State.CANCELLED)
        ScheduleEvent.objects.create(
            actor=actor,
            order=order,
            action="order_schedule_closed",
            snapshot={"appointment_ids": [str(i) for i in ids]},
        )


def pin_materials(inspection, rows):
    snapshot_rows = []
    for m in rows:
        InspectionMaterial.objects.get_or_create(inspection=inspection, material=m)
        snapshot_rows.append(
            {"id": str(m.pk), "sha256": m.sha256, "kind": m.kind, "slot_key": m.slot_key, "source": m.source}
        )
    inspection.materials_snapshot = snapshot_rows


def remote_evidence(order):
    # Use the remote guide even when the original form preferred an onsite visit.
    proxy = copy(order)
    proxy.answers = {**order.answers, "inspection_preference": "remote"}
    guide = materials.requirements(proxy, order.language)
    required = {
        (kind, s["key"])
        for group, kind in (("documents", "document"), ("photos", "photo"))
        for s in guide[group]
        if s["required"]
    }
    if not any(kind == "photo" for kind, key in required):
        raise orders.Conflict(
            "No remote photo guide exists for this service; use an onsite inspection.",
            code="remote_guide_required",
        )
    storage = PrivateStorage()
    rows = [
        m
        for m in materials.available(order)
        if m.source == Material.Source.CLIENT
        and m.review_status == Material.Review.ACCEPTED
        and storage.exists(m.storage_key)
    ]
    missing = required - {(m.kind, m.slot_key) for m in rows}
    if missing:
        raise ValidationError({"materials": [{"kind": k, "slot_key": s} for k, s in sorted(missing)]})
    return rows


@transaction.atomic
def start_inspection(actor, order_id, appointment_id, expected_version, materials_adequate=False):
    actor, order = locked_order(actor, order_id, True)
    orders.check_version(order, expected_version)
    if order.status != Order.Status.INSPECTION_SCHEDULED:
        raise orders.Conflict("A confirmed inspection is required.", code="invalid_transition")
    from apps.payments.services import require_funded

    require_funded(order)
    appointment = get_object_or_404(order.appointments, pk=appointment_id, state=Appointment.State.CONFIRMED)
    now = timezone.now()
    if not appointment.starts_at <= now < appointment.ends_at:
        raise orders.Conflict(
            "Start inside the confirmed interval; otherwise reschedule first.",
            code="outside_inspection_interval",
        )
    lock_calendar()
    # Do not start another job while the sole appraiser has an unfinished inspection.
    if Appointment.objects.filter(state=Appointment.State.IN_PROGRESS).exists():
        raise orders.Conflict("Finish the active inspection first.", code="inspection_in_progress")
    rows = []
    if appointment.mode == Appointment.Mode.REMOTE:
        if not materials_adequate:
            raise ValidationError({"materials_adequate": "The appraiser must confirm suitability."})
        rows = remote_evidence(order)
    inspection = Inspection.objects.create(
        order=order,
        appointment=appointment,
        started_by=actor,
        started_at=now,
        report_due_at=now + timedelta(hours=appointment.report_hours),
        remote_materials_adequate=appointment.mode == Appointment.Mode.REMOTE,
    )
    pin_materials(inspection, rows)
    inspection.save(update_fields=["materials_snapshot"])
    appointment.state = Appointment.State.IN_PROGRESS
    appointment.save(update_fields=["state"])
    order._schedule_before = order.status
    order.status = Order.Status.IN_PROGRESS
    record(
        actor,
        "inspection_started",
        order,
        appointment,
        {"started_at": now.isoformat(), "report_due_at": inspection.report_due_at.isoformat()},
    )
    return appointment, order


@transaction.atomic
def finish_inspection(actor, order_id, appointment_id, expected_version, note):
    actor, order = locked_order(actor, order_id, True)
    orders.check_version(order, expected_version)
    if order.status != Order.Status.IN_PROGRESS:
        raise orders.Conflict("An inspection must be in progress.", code="invalid_transition")
    appointment = get_object_or_404(
        order.appointments, pk=appointment_id, state=Appointment.State.IN_PROGRESS
    )
    inspection = Inspection.objects.get(appointment=appointment)
    lock_calendar()
    # Evidence already pinned at remote start is retained; new safe appraiser photos are added.
    rows = {m.pk: m for m in Material.objects.filter(inspection_links__inspection=inspection)}
    storage = PrivateStorage()
    rows.update({m.pk: m for m in materials.available(order) if storage.exists(m.storage_key)})
    pin_materials(inspection, rows.values())
    inspection.completed_at = timezone.now()
    inspection.completed_by = actor
    inspection.completion_note = note
    inspection.save()
    appointment.state = Appointment.State.COMPLETED
    appointment.save(update_fields=["state"])
    order._schedule_before = order.status
    order.status = Order.Status.AWAITING_REPORT
    record(actor, "inspection_completed", order, appointment, {"note": note})
    return appointment, order


@transaction.atomic
def create_block(actor, data):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    digest = fingerprint(data)
    lock_calendar()
    existing = CalendarBlock.objects.filter(client_request_id=data["client_request_id"]).first()
    if existing:
        if existing.created_by_id != actor.pk or existing.request_fingerprint != digest:
            raise orders.Conflict("Request id already used.", code="idempotency_conflict")
        return existing, False
    validate_interval(data["starts_at"], data["ends_at"], max_hours=24 * 31)
    ensure_free(data["starts_at"], data["ends_at"])
    reserve_quota("calendar-block", str(actor.pk), 30, 3600)
    block = CalendarBlock.objects.create(created_by=actor, request_fingerprint=digest, **data)
    record(
        actor,
        "calendar_block_created",
        extra={
            "block_id": str(block.pk),
            "starts_at": block.starts_at.isoformat(),
            "ends_at": block.ends_at.isoformat(),
            "reason": block.reason,
        },
    )
    return block, True


@transaction.atomic
def cancel_block(actor, block_id, reason):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    lock_calendar()
    block = get_object_or_404(CalendarBlock, pk=block_id)
    if not block.cancelled_at:
        block.cancelled_at = timezone.now()
        block.save(update_fields=["cancelled_at"])
        record(actor, "calendar_block_cancelled", extra={"block_id": str(block.pk), "reason": reason})
    return block
