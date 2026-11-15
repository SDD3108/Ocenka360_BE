from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from . import provider
from .messages import body
from .models import PushDelivery, PushDevice
from .services import delivery_allowed

KNOWN_ERRORS = {
    "DeviceNotRegistered",
    "MessageTooBig",
    "MessageRateExceeded",
    "MismatchSenderId",
    "InvalidCredentials",
}


def locked(query):
    return (
        query.select_for_update(skip_locked=True)
        if connection.features.has_select_for_update_skip_locked
        else query.select_for_update()
    )


def error_code(response):
    details = response.get("details")
    code = details.get("error") if isinstance(details, dict) else None
    return code if code in KNOWN_ERRORS else "provider_error"


def disable_device(delivery):
    # A stale receipt must not disable a token that has since rotated or changed owner.
    PushDevice.objects.filter(pk=delivery.device_id, revision=delivery.device_revision).update(active=False)


def send_pending(limit=50):
    if not provider.enabled():
        return {"disabled": True, "processed": 0}
    now = timezone.now()
    # A crash after dispatch is ambiguous; automatic replay could duplicate a push.
    PushDelivery.objects.filter(state="sending", claimed_at__lt=now - timedelta(minutes=5)).update(
        state="unknown", error_code="worker_interrupted", updated_at=now
    )
    processed = 0
    for _ in range(limit):
        with transaction.atomic():
            row = locked(
                PushDelivery.objects.filter(state="queued", next_attempt_at__lte=timezone.now())
            ).first()
            if not row:
                break
            row = PushDelivery.objects.select_related(
                "device__user", "device__session", "notification__event__order"
            ).get(pk=row.pk)
            if row.created_at < timezone.now() - timedelta(hours=24) or not delivery_allowed(row):
                row.state, row.error_code = "skipped", "expired_or_unavailable"
                row.save(update_fields=["state", "error_code", "updated_at"])
                processed += 1
                continue
            row.state, row.claimed_at = "sending", timezone.now()
            row.attempts += 1
            row.save(update_fields=["state", "claimed_at", "attempts", "updated_at"])
            payload = {
                "to": row.device.token,
                "title": "Оценка 360",
                "body": body(row.notification.kind, row.device.user.preferred_language),
                "sound": "default",
                "ttl": 3600,
                "data": {"notification_id": str(row.notification_id)},
            }
        # No database lock/transaction is held during the network call.
        try:
            response = provider.send(payload)
            failure = None
        except provider.PushError as exc:
            response, failure = None, exc
        with transaction.atomic():
            current = PushDelivery.objects.select_for_update().get(pk=row.pk)
            if current.state != "sending":
                continue
            if failure:
                current.error_code = failure.code
                if failure.retryable and current.attempts < 5:
                    current.state = "queued"
                    current.next_attempt_at = timezone.now() + timedelta(seconds=30 * 2**current.attempts)
                else:
                    current.state = "unknown" if failure.uncertain else "failed"
            elif (
                response.get("status") == "ok"
                and isinstance(response.get("id"), str)
                and 0 < len(response["id"]) <= 200
            ):
                current.state, current.ticket_id = "ticket", response["id"]
                current.receipt_due_at = timezone.now() + timedelta(minutes=15)
            elif response.get("status") == "error":
                current.error_code = error_code(response)
                current.state = "failed"
                if current.error_code == "DeviceNotRegistered":
                    disable_device(current)
                elif current.error_code == "MessageRateExceeded" and current.attempts < 5:
                    current.state = "queued"
                    current.next_attempt_at = timezone.now() + timedelta(seconds=30 * 2**current.attempts)
            else:
                current.state, current.error_code = "unknown", "invalid_ticket"
            current.save()
        processed += 1
    return {"disabled": False, "processed": processed}


def check_receipts(limit=100):
    if not provider.enabled():
        return {"disabled": True, "processed": 0}
    # Lease with a conditional update; concurrent receipt workers don't starve the next batch.
    ids = list(
        PushDelivery.objects.filter(state="ticket", receipt_due_at__lte=timezone.now()).values_list(
            "pk", flat=True
        )[:limit]
    )
    claimed = []
    for pk in ids:
        if PushDelivery.objects.filter(pk=pk, state="ticket", receipt_due_at__lte=timezone.now()).update(
            receipt_due_at=timezone.now() + timedelta(minutes=15)
        ):
            claimed.append(pk)
    rows = list(PushDelivery.objects.filter(pk__in=claimed))
    if not rows:
        return {"disabled": False, "processed": 0}
    try:
        responses = provider.receipts([r.ticket_id for r in rows])
    except provider.PushError:
        responses = {}
    for row in rows:
        with transaction.atomic():
            current = PushDelivery.objects.select_for_update().get(pk=row.pk)
            if current.state != "ticket":
                continue
            response = responses.get(row.ticket_id)
            if isinstance(response, dict) and response.get("status") == "ok":
                current.state, current.error_code = "accepted", ""
            elif isinstance(response, dict) and response.get("status") == "error":
                current.state, current.error_code = "failed", error_code(response)
                if current.error_code == "DeviceNotRegistered":
                    disable_device(current)
            elif current.claimed_at < timezone.now() - timedelta(hours=23):
                current.state, current.error_code = "unknown", "receipt_missing"
            current.save()
    return {"disabled": False, "processed": len(rows)}
