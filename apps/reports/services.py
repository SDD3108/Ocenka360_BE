import hashlib
import uuid
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.accounts.security import reserve_quota
from apps.communications.services import require_resolved
from apps.materials.models import Material
from apps.materials.services import get_order
from apps.materials.storage import PrivateStorage
from apps.materials.validation import validated_file
from apps.orders import services as orders
from apps.orders.models import Order
from apps.payments.services import require_funded

from .models import Original, Report, ReportAccess


def prepare(actor, order_id, review=True):
    actor = orders.actor_for_write(actor, consent=False, reviewer=review)
    order = get_order(actor, order_id, review, lock=True)
    orders.supported(order)
    return actor, order


def upload_allowed(order):
    if order.status not in {Order.Status.AWAITING_REPORT, Order.Status.COMPLETED}:
        raise orders.Conflict("Finish the inspection before uploading a report.")
    if not hasattr(order, "inspection") or not order.inspection.completed_at:
        raise orders.Conflict("A completed inspection is required.")


def upload(actor, order_id, data):
    with transaction.atomic():
        actor, order = prepare(actor, order_id)
        old = order.reports.filter(client_upload_id=data["client_upload_id"]).first()
        if not old:
            orders.check_version(order, data["expected_version"])
            upload_allowed(order)
        reserve_quota("report-upload", str(actor.pk), 20, 3600)
    if Path(data["file"].name).suffix.lower() != ".pdf":
        raise ValidationError({"file": "Reports must be PDF files."})
    stored = None
    storage = PrivateStorage()
    with validated_file(data["file"], "document") as (path, metadata):
        if metadata["mime_type"] != "application/pdf":
            raise ValidationError({"file": "Reports must be PDF files."})
        try:
            with transaction.atomic():
                actor, order = prepare(actor, order_id)
                old = order.reports.filter(client_upload_id=data["client_upload_id"]).first()
                if old:
                    if (
                        old.uploaded_by_id != actor.pk
                        or old.sha256 != metadata["sha256"]
                        or any(
                            getattr(old, k) != data[k]
                            for k in ["report_number", "title", "issue_date", "note"]
                        )
                    ):
                        raise orders.Conflict(
                            "Upload id used with different content.", code="idempotency_conflict"
                        )
                    return old, order, False
                orders.check_version(order, data["expected_version"])
                upload_allowed(order)
                if order.reports.count() >= 50 or order.reports.filter(state="draft").count() >= 5:
                    raise orders.Conflict("Report version limit reached.")
                report_id = uuid.uuid4()
                with path.open("rb") as stream:
                    stored = storage.save(f"reports/{order.pk}/{report_id}.pdf", File(stream))
                report = Report.objects.create(
                    id=report_id,
                    order=order,
                    uploaded_by=actor,
                    client_upload_id=data["client_upload_id"],
                    revision=(order.reports.aggregate(v=Max("revision"))["v"] or 0) + 1,
                    storage_key=stored,
                    sha256=metadata["sha256"],
                    byte_size=metadata["byte_size"],
                    page_count=metadata["page_count"],
                    scan_status=metadata["scan_status"],
                    **{k: data[k] for k in ["report_number", "title", "issue_date", "note"]},
                )
                orders.changed(order, actor, "report_uploaded", order.status, str(report.pk))
                return report, order, True
        except Exception:
            if stored:
                storage.delete(stored)
            raise


def verified_stream(report):
    if not settings.ALLOW_UNSCANNED_UPLOADS and report.scan_status != Material.Scan.CLEAN:
        raise NotFound()
    try:
        stream = PrivateStorage().open(report.storage_key, "rb")
    except FileNotFoundError as exc:
        raise NotFound() from exc
    digest, size = hashlib.sha256(), 0
    try:
        for chunk in iter(lambda: stream.read(65536), b""):
            size += len(chunk)
            if size > report.byte_size:
                raise orders.Conflict("Stored report integrity check failed.", code="report_integrity")
            digest.update(chunk)
        if size != report.byte_size or digest.hexdigest() != report.sha256:
            raise orders.Conflict("Stored report integrity check failed.", code="report_integrity")
        stream.seek(0)
        return stream
    except Exception:
        stream.close()
        raise


@transaction.atomic
def publish(actor, order_id, report_id, expected_version, confirmed, reason=""):
    actor, order = prepare(actor, order_id)
    report = get_object_or_404(order.reports, pk=report_id)
    if not confirmed:
        raise ValidationError({"confirmed": "Check the file and explicitly confirm publication."})
    if report.state == "published":
        return report, order
    orders.check_version(order, expected_version)
    upload_allowed(order)
    if report.state != "draft":
        raise orders.Conflict("Only a draft report can be published.")
    require_resolved(order)
    # Always check disputes, including legacy local fixtures bypassed by require_funded.
    if order.cancellation_cases.filter(state__in=["requested", "offered"]).exists():
        raise orders.Conflict("Resolve the cancellation request before publishing.")
    require_funded(order)
    last_revision = order.reports.filter(published_at__isnull=False).aggregate(v=Max("revision"))["v"]
    if last_revision is not None and report.revision <= last_revision:
        raise orders.Conflict(
            "Upload a new revision instead of publishing an older draft.", code="stale_report_revision"
        )
    previous = last_revision is not None
    if previous and not reason.strip():
        raise ValidationError({"reason": "Explain why the previous report is being replaced."})
    with verified_stream(report):
        pass
    order.reports.filter(state="published").update(state="superseded")
    now = timezone.now()
    report.state, report.published_at, report.published_by, report.change_reason = (
        "published",
        now,
        actor,
        reason,
    )
    report.save(update_fields=["state", "published_at", "published_by", "change_reason"])
    Original.objects.create(report=report)
    before = order.status
    order.status = Order.Status.COMPLETED
    order.completed_at = order.completed_at or now
    orders.changed(order, actor, "report_published", before, str(report.pk))
    return report, order


@transaction.atomic
def withdraw(actor, order_id, report_id, expected_version, confirmed, reason):
    actor, order = prepare(actor, order_id)
    report = get_object_or_404(order.reports, pk=report_id)
    if not confirmed or not reason.strip():
        raise ValidationError({"reason": "Confirm withdrawal and explain the reason."})
    if report.state == "withdrawn":
        if report.change_reason != reason:
            raise orders.Conflict("The report was withdrawn with a different reason.")
        return report, order
    orders.check_version(order, expected_version)
    if report.state not in {"draft", "published"}:
        raise orders.Conflict("This report has already been superseded.")
    was_public = report.state == "published"
    report.state, report.withdrawn_at, report.change_reason = "withdrawn", timezone.now(), reason
    report.save(update_fields=["state", "withdrawn_at", "change_reason"])
    before = order.status
    if was_public:
        order.status, order.completed_at = Order.Status.AWAITING_REPORT, None
    orders.changed(
        order, actor, "report_withdrawn" if was_public else "report_draft_discarded", before, str(report.pk)
    )
    return report, order


@transaction.atomic
def open_report(actor, order_id, report_id, review=False):
    actor, order = prepare(actor, order_id, review)
    query = order.reports.all() if review else order.reports.filter(state="published")
    report = get_object_or_404(query, pk=report_id)
    reserve_quota("report-download", str(actor.pk), 60, 3600)
    stream = verified_stream(report)
    try:
        ReportAccess.objects.create(report=report, actor=actor)
    except Exception:
        stream.close()
        raise
    return report, stream


@transaction.atomic
def original_action(actor, order_id, report_id, expected_version, action, confirmed, method="", note=""):
    actor, order = prepare(actor, order_id)
    report = get_object_or_404(order.reports, pk=report_id, state="published")
    original = report.original
    if not confirmed:
        raise ValidationError({"confirmed": "Explicit confirmation required."})
    if action == "ready" and original.state == "ready":
        return original, order
    if action == "handover" and original.state == "handed_over":
        if original.handover_method != method or original.handover_note != note:
            raise orders.Conflict("Handover already recorded with different details.")
        return original, order
    orders.check_version(order, expected_version)
    if action == "ready":
        if original.state != "preparing":
            raise orders.Conflict("Original is no longer awaiting preparation.")
        original.state, original.ready_at = "ready", timezone.now()
    else:
        if original.state != "ready" or method not in {"pickup", "courier"}:
            raise orders.Conflict("Mark the original ready and select pickup or courier.")
        original.state, original.handed_over_at = "handed_over", timezone.now()
        original.handover_method, original.handover_note = method, note
    original.save()
    orders.changed(
        order,
        actor,
        "original_ready" if action == "ready" else "original_handed_over",
        order.status,
        str(report.pk),
    )
    return original, order


@transaction.atomic
def request_delivery(actor, order_id, report_id, expected_version, client_request_id, confirmed, note=""):
    actor, order = prepare(actor, order_id, False)
    report = get_object_or_404(order.reports, pk=report_id, state="published")
    original = report.original
    if not confirmed:
        raise ValidationError({"confirmed": "Confirm that delivery is arranged and paid separately."})
    if original.delivery_request_id == client_request_id:
        if original.delivery_note != note:
            raise orders.Conflict("Request id used with different details.")
        return original, order
    orders.check_version(order, expected_version)
    if original.state == "handed_over" or original.delivery_requested_at:
        raise orders.Conflict("Delivery already requested or original handed over. Contact the appraiser.")
    original.delivery_requested_at, original.delivery_request_id, original.delivery_note = (
        timezone.now(),
        client_request_id,
        note,
    )
    original.save()
    orders.changed(order, actor, "original_delivery_requested", order.status, str(report.pk))
    return original, order


def delivery_contact(order):
    return "https://wa.me/77017700026?" + urlencode(
        {"text": f"Здравствуйте! Хочу согласовать доставку оригинала по заявке {order.number}."}
    )
