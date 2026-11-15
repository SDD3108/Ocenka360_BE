import base64
import hashlib
import io
import json
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from apps.accounts.security import digest, reserve_quota
from apps.catalog.schema import localized_definition
from apps.catalog.services import published_services
from apps.materials.models import Material
from apps.materials.services import checklist, get_order
from apps.materials.storage import PrivateStorage
from apps.orders import services as orders

from .models import Job, Permission, PermissionEvent
from .policy import POLICY_VERSION, PROMPT_VERSION, enabled


class Unavailable(APIException):
    status_code = 503
    default_code = "ai_unavailable"
    default_detail = "AI is not configured. The normal application process remains available."


def redact(text):
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)", "[identifier]", text)
    return text


def permission_for(order, photos=False):
    value = Permission.objects.filter(order=order, enabled=True, policy_version=POLICY_VERSION).first()
    if not value or (photos and not value.allow_photos):
        raise PermissionDenied(
            "The owner must enable optional AI for this order and selected photos.",
            code="ai_consent_required",
        )
    return value


@transaction.atomic
def set_permission(actor, order_id, expected_version, policy_version, enabled, allow_photos, confirmed):
    actor = orders.actor_for_write(actor, consent=enabled)
    order = get_order(actor, order_id, False, lock=True)
    orders.supported(order)
    if not confirmed:
        raise ValidationError("Explicit confirmation required.")
    if enabled and policy_version != POLICY_VERSION:
        raise orders.Conflict("Read the current AI notice.", code="stale_ai_notice")
    if allow_photos and not enabled:
        raise ValidationError("Photos require AI permission.")
    obj = Permission.objects.filter(order=order).first()
    if expected_version != (obj.version if obj else 0):
        raise orders.Conflict(code="version_conflict")
    if obj:
        obj.version += 1
        obj.policy_version = POLICY_VERSION
        obj.enabled, obj.allow_photos = enabled, allow_photos
        obj.save()
    else:
        obj = Permission.objects.create(
            order=order, enabled=enabled, allow_photos=allow_photos, policy_version=POLICY_VERSION
        )
    PermissionEvent.objects.create(
        permission=obj,
        actor=actor,
        version=obj.version,
        policy_version=POLICY_VERSION,
        enabled=enabled,
        allow_photos=allow_photos,
    )
    # Every permission revision invalidates pending work and removes retained generated content.
    Job.objects.filter(order=order).exclude(state="expired").update(
        state="cancelled",
        context={},
        photo_refs=[],
        result={},
        decision_note="",
        error_code="permission_changed",
        finished_at=timezone.now(),
    )
    return obj


def visible(actor, review=False):
    if review and actor.role not in {"appraiser", "admin"}:
        raise PermissionDenied()
    query = Job.objects.filter(actor=actor, audience="review" if review else "client")
    access = Q(order__isnull=True)
    access |= Q(order__in=orders.review_queryset()) if review else Q(order__owner=actor)
    query = query.filter(access)
    if not settings.ALLOW_ORDER_SIMULATION:
        query = query.filter(Q(order__isnull=True) | Q(order__is_test=False))
    return query.select_related("order")


def build_context(actor, data, order):
    language = actor.preferred_language
    context = {
        "language": language,
        "task": data["kind"],
        "question": redact(data["question"]),
        "allowed_services": [],
    }
    photo_refs = []
    if data["kind"] == "service_guide":
        services = list(published_services()[:60])
        context["allowed_services"] = [{"slug": s.slug, "title": s.title[language]} for s in services]
    elif data["kind"] == "form_help":
        service = order.service if order else get_object_or_404(published_services(), pk=data["service_id"])
        form = order.form if order else service.current_form
        field = next(
            (
                f
                for f in localized_definition(form.definition, language)["fields"]
                if f["key"] == data["field_key"]
            ),
            None,
        )
        if not field:
            raise ValidationError({"field_key": "Unknown field."})
        context.update(service_slug=service.slug, form_checksum=form.checksum, field=field)
    else:
        context["service_slug"] = order.service_snapshot["slug"]
        info = checklist(order, language)
        context["checklist"] = {k: info[k] for k in ["documents", "photos", "missing", "complete", "notice"]}
        # No answers, city, address, owner identity, order number, notes or document bytes.
    for ident in data["photo_ids"]:
        photo = get_object_or_404(order.materials, pk=ident, kind="photo", withdrawn_at__isnull=True)
        if photo.slot_key == "vin" or photo.review_status == "rejected":
            raise ValidationError("VIN/rejected photos cannot be sent to AI.")
        if photo.mime_type not in {"image/jpeg", "image/png"}:
            raise ValidationError("Only validated JPEG/PNG photos are supported.")
        if not settings.ALLOW_UNSCANNED_UPLOADS and photo.scan_status != "clean":
            raise ValidationError("Photo must pass antivirus checks.")
        photo_refs.append({"id": str(photo.pk), "sha256": photo.sha256, "slot_key": photo.slot_key})
    context["photos"] = [{"label": f"Photo {i + 1}", "slot": r["slot_key"]} for i, r in enumerate(photo_refs)]
    if len(json.dumps(context, ensure_ascii=False).encode()) > 48000:
        raise ValidationError("Context exceeds the AI limit.")
    return context, photo_refs


@transaction.atomic
def create_job(actor, data, review=False):
    actor = orders.actor_for_write(actor, consent=True, reviewer=review)
    if not actor.adult_confirmed_at:
        raise PermissionDenied("Adult confirmation required.")
    if not data["confirmed"] or data["policy_version"] != POLICY_VERSION:
        raise ValidationError("Read and explicitly accept the current AI notice.")
    if data["kind"] == "description_draft" and not review:
        raise PermissionDenied("Description drafts are for the appraiser.")
    fingerprint = digest("ai-request", json.dumps({**data, "review": review}, sort_keys=True, default=str))
    old = visible(actor, review).filter(client_request_id=data["client_request_id"]).first()
    if old:
        if old.fingerprint != fingerprint:
            raise orders.Conflict(code="idempotency_conflict")
        return old, False
    if Job.objects.filter(actor=actor, client_request_id=data["client_request_id"]).exists():
        raise orders.Conflict(code="idempotency_conflict")
    if not enabled():
        raise Unavailable()
    order = None
    permission = None
    if data.get("order_id"):
        order = get_order(actor, data["order_id"], review, lock=True)
        orders.supported(order)
        orders.check_version(order, data["expected_version"])
        permission = permission_for(order, bool(data["photo_ids"]))
    context, photos = build_context(actor, data, order)
    reserve_quota("ai-user-hour", str(actor.pk), 5, 3600)
    reserve_quota("ai-user-day", str(actor.pk), settings.GEMINI_USER_DAILY_LIMIT, 86400)
    reserve_quota("ai-global-day", "all", settings.GEMINI_GLOBAL_DAILY_LIMIT, 86400)
    obj = Job.objects.create(
        actor=actor,
        order=order,
        audience="review" if review else "client",
        kind=data["kind"],
        client_request_id=data["client_request_id"],
        fingerprint=fingerprint,
        policy_version=POLICY_VERSION,
        permission_version=permission.version if permission else None,
        order_version=order.version if order else None,
        language=actor.preferred_language,
        context=context,
        photo_refs=photos,
        model_name=settings.GEMINI_MODEL,
        prompt_version=PROMPT_VERSION,
        expires_at=timezone.now() + timedelta(days=settings.AI_CONTENT_RETENTION_DAYS),
    )
    return obj, True


def authorized(job):
    # Called with actor/order locks by worker, and as a freshness check when reading results.
    if job.policy_version != POLICY_VERSION or job.expires_at <= timezone.now():
        raise orders.Conflict("Expired AI request.")
    if job.order_id:
        order = get_order(job.actor, job.order_id, job.audience == "review")
        orders.supported(order)
        permission = permission_for(order, bool(job.photo_refs))
        if permission.version != job.permission_version or order.version != job.order_version:
            raise orders.Conflict("AI input changed.")
    if job.kind == "service_guide":
        allowed = {s["slug"] for s in job.context.get("allowed_services", [])}
        if not allowed <= {s.slug for s in published_services()}:
            raise orders.Conflict("Catalog changed.")
    if job.kind == "form_help" and not job.order_id:
        service = published_services().filter(slug=job.context.get("service_slug")).first()
        if not service or service.current_form.checksum != job.context.get("form_checksum"):
            raise orders.Conflict("Form changed.")


def read_images(job):
    images = []
    for ref in job.photo_refs:
        photo = get_object_or_404(
            Material, pk=ref["id"], order_id=job.order_id, kind="photo", withdrawn_at__isnull=True
        )
        if photo.review_status == "rejected" or photo.slot_key == "vin":
            raise orders.Conflict("Photo is unavailable.")
        if not settings.ALLOW_UNSCANNED_UPLOADS and photo.scan_status != "clean":
            raise orders.Conflict("Photo is unscanned.")
        with PrivateStorage().open(photo.storage_key, "rb") as stream:
            raw = stream.read(settings.MATERIAL_MAX_FILE_BYTES + 1)
        if len(raw) != photo.byte_size or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise orders.Conflict("Photo integrity mismatch.")
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 24000000:
                raise orders.Conflict("Photo too large.")
            image = image.convert("RGB")
            image.thumbnail((1024, 1024))
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=85, exif=b"")
        images.append(base64.b64encode(out.getvalue()).decode("ascii"))
    return images


@transaction.atomic
def cancel_job(actor, job_id, review=False):
    actor = orders.actor_for_write(actor, consent=False, reviewer=review)
    ref = get_object_or_404(visible(actor, review), pk=job_id)
    if ref.order_id:
        get_order(actor, ref.order_id, review, lock=True)
    job = Job.objects.select_for_update().get(pk=ref.pk)
    if job.state == "expired":
        return job
    job.state = "cancelled"
    job.error_code = "cancelled_by_user"
    job.context = {}
    job.photo_refs = []
    job.result = {}
    job.decision_note = ""
    job.finished_at = timezone.now()
    job.save()
    return job


@transaction.atomic
def decide_job(actor, job_id, decision, confirmed, note):
    actor = orders.actor_for_write(actor, consent=True, reviewer=True)
    ref = get_object_or_404(visible(actor, True), pk=job_id)
    if ref.order_id:
        get_order(actor, ref.order_id, True, lock=True)
    job = Job.objects.select_for_update(of=("self",)).select_related("actor", "order").get(pk=ref.pk)
    authorized(job)
    if not confirmed or job.state != "succeeded":
        raise orders.Conflict("Confirm a successful current draft.")
    if job.decision:
        if (job.decision, job.decision_note) != (decision, note):
            raise orders.Conflict("Decision already recorded.")
        return job
    job.decision, job.decision_note, job.decided_at = decision, note, timezone.now()
    job.save(update_fields=["decision", "decision_note", "decided_at"])
    # Acceptance records a human review only; never writes a report, price or Material.review_status.
    return job
