import uuid

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User
from apps.accounts.security import reserve_quota
from apps.orders import services as orders
from apps.orders.models import Order

from .guides import requirements
from .models import Material, MaterialManifest, SubmissionMaterial
from .storage import PrivateStorage
from .validation import validated_file


def get_order(actor, order_id, review=False, lock=False):
    if review:
        if actor.role not in {User.Role.APPRAISER, User.Role.ADMIN}:
            raise PermissionDenied("Reviewer role required.")
        query = orders.review_queryset()
    else:
        query = Order.objects.filter(owner=actor)
    # Do not lock joined tables: all writers consistently lock actor, then the order itself.
    if lock:
        query = query.select_for_update()
    return get_object_or_404(query, pk=order_id)


def available(order):
    query = order.materials.filter(withdrawn_at__isnull=True).exclude(review_status=Material.Review.REJECTED)
    if not settings.ALLOW_UNSCANNED_UPLOADS:
        query = query.filter(scan_status=Material.Scan.CLEAN)
    return query


def checklist(order, language=None):
    from copy import copy

    effective = copy(order)
    if order.appointments.filter(state__in=["confirmed", "in_progress"], mode="remote").exists():
        effective.answers = {**order.answers, "inspection_preference": "remote"}
    result = requirements(effective, language or order.language)
    storage = PrivateStorage()
    present = {
        (m.kind, m.slot_key)
        for m in available(order)
        if m.source == Material.Source.CLIENT and storage.exists(m.storage_key)
    }
    missing = []
    for group, kind in (("documents", "document"), ("photos", "photo")):
        for slot in result[group]:
            slot["provided"] = (kind, slot["key"]) in present
            if slot["required"] and not slot["provided"]:
                missing.append({"kind": kind, "slot_key": slot["key"]})
    result.update(
        missing=missing,
        complete=not missing,
        order_version=order.version,
        allowed_types=["application/pdf", "image/jpeg", "image/png"],
        max_file_bytes=settings.MATERIAL_MAX_FILE_BYTES,
    )
    return result


def require_materials(order):
    missing = checklist(order)["missing"]
    if missing:
        raise ValidationError({"materials": missing})


def capture_manifest(submission):
    order = submission.order
    materials = list(available(order))
    manifest = MaterialManifest.objects.create(
        submission=submission,
        snapshot={
            "guide_version": 1,
            "requirements": requirements(order, order.language),
            "files": [
                {
                    "id": str(m.pk),
                    "kind": m.kind,
                    "slot_key": m.slot_key,
                    "source": m.source,
                    "sha256": m.sha256,
                    "byte_size": m.byte_size,
                    "mime_type": m.mime_type,
                    "scan_status": m.scan_status,
                    "review_status": m.review_status,
                }
                for m in materials
            ],
        },
    )
    SubmissionMaterial.objects.bulk_create(
        [SubmissionMaterial(manifest=manifest, material=m) for m in materials]
    )


def editable(order, review):
    allowed = (
        {Order.Status.UNDER_REVIEW, Order.Status.INSPECTION_SCHEDULED, Order.Status.IN_PROGRESS}
        if review
        else {Order.Status.DRAFT, Order.Status.NEEDS_INFORMATION}
    )
    if (
        not review
        and order.status == Order.Status.INSPECTION_SCHEDULED
        and order.appointments.filter(state="confirmed", mode="remote").exists()
    ):
        allowed.add(Order.Status.INSPECTION_SCHEDULED)
    if order.status not in allowed:
        raise orders.Conflict("Files cannot be changed in this status.", code="invalid_transition")


def validate_slot(order, data, review):
    if review and data["kind"] != Material.Kind.PHOTO:
        raise ValidationError({"kind": "The appraiser can upload inspection photographs here."})
    group = "documents" if data["kind"] == Material.Kind.DOCUMENT else "photos"
    if data["slot_key"] not in {s["key"] for s in requirements(order, order.language)[group]}:
        raise ValidationError({"slot_key": "Use a slot from this order's current material requirements."})


def upload(actor, order_id, data, review=False):
    # Cheap authorization/state checks before invoking file parsers or the antivirus.
    with transaction.atomic():
        actor = orders.actor_for_write(actor, consent=not review, reviewer=review)
        order = get_order(actor, order_id, review, lock=True)
        orders.supported(order)
        existing = order.materials.filter(client_upload_id=data["client_upload_id"]).first()
        if not existing:
            orders.check_version(order, data["expected_version"])
            editable(order, review)
            validate_slot(order, data, review)
        reserve_quota("material-upload", str(actor.pk), 60, 3600)
    storage = PrivateStorage()
    stored_key = None
    with validated_file(data["file"], data["kind"]) as (path, metadata):
        try:
            with transaction.atomic():
                actor = orders.actor_for_write(actor, consent=not review, reviewer=review)
                order = get_order(actor, order_id, review, lock=True)
                orders.supported(order)
                existing = order.materials.filter(client_upload_id=data["client_upload_id"]).first()
                if existing:
                    if (
                        existing.uploaded_by_id != actor.pk
                        or existing.withdrawn_at
                        or existing.kind != data["kind"]
                        or existing.slot_key != data["slot_key"]
                        or existing.original_sha256 != metadata["original_sha256"]
                    ):
                        raise orders.Conflict(
                            "Upload id already used for another file.", code="idempotency_conflict"
                        )
                    return existing, order, False
                orders.check_version(order, data["expected_version"])
                editable(order, review)
                validate_slot(order, data, review)
                total = (
                    order.materials.filter(withdrawn_at__isnull=True).aggregate(total=Sum("byte_size"))[
                        "total"
                    ]
                    or 0
                )
                if (
                    order.materials.count() >= settings.MATERIAL_MAX_ORDER_FILES
                    or total + metadata["byte_size"] > settings.MATERIAL_MAX_ORDER_BYTES
                ):
                    raise orders.Conflict("Order file quota exceeded.", code="material_limit")
                extension = metadata.pop("extension")
                material_id = uuid.uuid4()
                key = f"{order.pk}/{material_id}.{extension}"
                with path.open("rb") as stream:
                    stored_key = storage.save(key, File(stream))
                material = Material.objects.create(
                    id=material_id,
                    order=order,
                    uploaded_by=actor,
                    client_upload_id=data["client_upload_id"],
                    kind=data["kind"],
                    slot_key=data["slot_key"],
                    source=Material.Source.APPRAISER if review else Material.Source.CLIENT,
                    storage_key=stored_key,
                    **metadata,
                )
                orders.changed(order, actor, "material_uploaded", order.status, str(material.pk))
                return material, order, True
        except Exception:
            if stored_key:
                storage.delete(stored_key)
            raise


def safe_delete(key):
    # A failed unlink leaves an inaccessible orphan for reconciliation, not an HTTP failure after commit.
    try:
        PrivateStorage().delete(key)
    except OSError:
        pass


@transaction.atomic
def withdraw(actor, order_id, material_id, expected_version, review=False):
    actor = orders.actor_for_write(actor, consent=not review, reviewer=review)
    order = get_order(actor, order_id, review, lock=True)
    orders.supported(order)
    orders.check_version(order, expected_version)
    editable(order, review)
    material = get_object_or_404(
        order.materials, pk=material_id, uploaded_by=actor, withdrawn_at__isnull=True
    )
    if material.submission_links.exists() or material.inspection_links.exists():
        raise orders.Conflict(
            "A confirmed file is part of the order history and cannot be removed here.",
            code="material_pinned",
        )
    material.withdrawn_at = timezone.now()
    material.save(update_fields=["withdrawn_at"])
    orders.changed(order, actor, "material_withdrawn", order.status, str(material.pk))
    transaction.on_commit(lambda: safe_delete(material.storage_key))
    return order


@transaction.atomic
def review_material(actor, order_id, material_id, expected_version, decision, comment):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    order = get_order(actor, order_id, review=True, lock=True)
    orders.supported(order)
    orders.check_version(order, expected_version)
    editable(order, True)
    material = get_object_or_404(order.materials, pk=material_id, withdrawn_at__isnull=True)
    if material.source != Material.Source.CLIENT:
        raise ValidationError({"decision": "This action reviews client material."})
    if decision == Material.Review.REJECTED and not comment.strip():
        raise ValidationError({"comment": "Explain what to replace and, if helpful, where to obtain it."})
    if (
        decision == Material.Review.ACCEPTED
        and not settings.ALLOW_UNSCANNED_UPLOADS
        and material.scan_status != Material.Scan.CLEAN
    ):
        raise orders.Conflict("File has not passed antivirus checks.", code="scan_required")
    material.review_status = decision
    material.review_comment = comment
    material.reviewed_by = actor
    material.reviewed_at = timezone.now()
    material.save()
    orders.changed(order, actor, "material_" + decision, order.status, f"{material.pk}: {comment}")
    return material, order
