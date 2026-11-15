import hashlib
import json

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.security import reserve_quota
from apps.materials.guides import requirements
from apps.materials.services import available, get_order
from apps.materials.storage import PrivateStorage
from apps.orders import services as orders
from apps.orders.models import Order

from .models import Comment, MaterialReply, MaterialRequest, ReplyMaterial

CLOSED = {Order.Status.CANCELLED, Order.Status.REJECTED, Order.Status.SERVICE_STOPPED}


def fingerprint(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def prepare(actor, order_id, review):
    actor = orders.actor_for_write(actor, consent=not review, reviewer=review)
    order = get_order(actor, order_id, review, lock=True)
    orders.supported(order)
    if not order.review_unlocked_at:
        raise orders.Conflict("Communication opens after the advance is confirmed.", code="payment_required")
    return actor, order


@transaction.atomic
def comment(actor, order_id, client_request_id, body, review=False):
    actor, order = prepare(actor, order_id, review)
    old = Comment.objects.filter(order=order, author=actor, client_request_id=client_request_id).first()
    if old:
        if old.body != body:
            raise orders.Conflict("Request id already used.", code="idempotency_conflict")
        return old, order, False
    reserve_quota("comment", str(actor.pk), 30, 3600)
    if order.comments.count() >= 1000:
        raise orders.Conflict("Comment limit reached.")
    result = Comment.objects.create(
        order=order, author=actor, author_role=actor.role, client_request_id=client_request_id, body=body
    )
    orders.changed(order, actor, "comment_added", order.status, str(result.pk))
    return result, order, True


@transaction.atomic
def request_materials(actor, order_id, expected_version, client_request_id, message, items):
    actor, order = prepare(actor, order_id, True)
    digest = fingerprint(dict(message=message, items=items))
    old = order.material_requests.filter(author=actor, client_request_id=client_request_id).first()
    if old:
        if old.fingerprint != digest:
            raise orders.Conflict("Request id already used.", code="idempotency_conflict")
        return old, order, False
    orders.check_version(order, expected_version)
    if order.status != Order.Status.UNDER_REVIEW:
        raise orders.Conflict("Start reviewing the order first.")
    if order.material_requests.filter(state__in=["open", "submitted"]).exists():
        raise orders.Conflict("Resolve or close the existing request first.")
    if order.material_requests.count() >= 50:
        raise orders.Conflict("Request limit reached.")
    reserve_quota("material-request", str(actor.pk), 30, 3600)
    guide = requirements(order, order.language)
    slots = {
        (kind, item["key"])
        for group, kind in [("documents", "document"), ("photos", "photo")]
        for item in guide[group]
    }
    for item in items:
        if item["kind"] != "text" and (item["kind"], item["slot_key"]) not in slots:
            raise ValidationError({"items": "Use material slots from the current checklist."})
    result = MaterialRequest.objects.create(
        order=order,
        author=actor,
        client_request_id=client_request_id,
        fingerprint=digest,
        message=message,
        items=items,
    )
    before = order.status
    order.status = Order.Status.NEEDS_INFORMATION
    orders.changed(order, actor, "materials_requested", before, str(result.pk))
    return result, order, True


@transaction.atomic
def respond(actor, order_id, request_id, expected_version, client_request_id, body, material_ids, confirmed):
    actor, order = prepare(actor, order_id, False)
    req = get_object_or_404(order.material_requests, pk=request_id)
    digest = fingerprint(dict(body=body, material_ids=sorted(str(x) for x in material_ids)))
    old = req.replies.filter(client_request_id=client_request_id).first()
    if old:
        if old.fingerprint != digest:
            raise orders.Conflict("Request id already used.", code="idempotency_conflict")
        return old, order, False
    orders.check_version(order, expected_version)
    if not confirmed:
        raise ValidationError({"confirmed": "Explicit confirmation required."})
    if req.state != "open" or order.status != Order.Status.NEEDS_INFORMATION:
        raise orders.Conflict("Request is not awaiting a response.")
    files = list(available(order).filter(pk__in=material_ids, source="client"))
    storage = PrivateStorage()
    if len(files) != len(material_ids) or any(not storage.exists(f.storage_key) for f in files):
        raise ValidationError({"material_ids": "Choose available client files belonging to this order."})
    needed = {(i["kind"], i["slot_key"]) for i in req.items if i["kind"] != "text"}
    provided = {(f.kind, f.slot_key) for f in files}
    if needed != provided:
        raise ValidationError(
            {"material_ids": "Provide files for every requested slot, without unrelated files."}
        )
    if any(i["kind"] == "text" for i in req.items) and not body.strip():
        raise ValidationError({"body": "Answer the clarification request."})
    if req.replies.count() >= 30:
        raise orders.Conflict("Response limit reached.")
    reserve_quota("material-reply", str(actor.pk), 20, 3600)
    result = MaterialReply.objects.create(
        request=req, author=actor, client_request_id=client_request_id, fingerprint=digest, body=body
    )
    ReplyMaterial.objects.bulk_create([ReplyMaterial(reply=result, material=f) for f in files])
    req.state = "submitted"
    req.save(update_fields=["state", "updated_at"])
    # One transaction: the reply, pinned files, complete form/manifest and resubmission.
    orders.submit_order(actor, order.pk, order.version, True)
    order.refresh_from_db()
    return result, order, True


@transaction.atomic
def decide(actor, order_id, request_id, expected_version, decision, comment):
    actor, order = prepare(actor, order_id, True)
    orders.check_version(order, expected_version)
    req = get_object_or_404(order.material_requests, pk=request_id)
    if req.state not in {"open", "submitted"} or order.status in CLOSED:
        raise orders.Conflict("Request is already closed.")
    before = order.status
    if decision == "cancel":
        req.state = "cancelled"
    else:
        if req.state != "submitted" or order.status != Order.Status.UNDER_REVIEW:
            raise orders.Conflict("Start reviewing the submitted response first.")
        if decision == "accept":
            reply = req.replies.first()
            files = list(available(order).filter(reply_links__reply=reply))
            if len(files) != reply.links.count() or any(
                f.review_status != "accepted" or not PrivateStorage().exists(f.storage_key) for f in files
            ):
                raise orders.Conflict(
                    "Review and accept every attached file first.", code="materials_not_accepted"
                )
            req.state = "resolved"
        else:
            req.state = "open"
            order.status = Order.Status.NEEDS_INFORMATION
    req.review_comment = comment
    req.save(update_fields=["state", "review_comment", "updated_at"])
    orders.changed(order, actor, "material_request_" + decision, before, str(req.pk))
    return req, order


def require_answered(order):
    if order.material_requests.filter(state="open").exists():
        raise orders.Conflict("Respond to the material request before submitting.", code="request_open")


def require_resolved(order):
    if order.material_requests.filter(state__in=["open", "submitted"]).exists():
        raise orders.Conflict(
            "Resolve the material request before accepting the order.", code="request_unresolved"
        )


def close_requests(order):
    order.material_requests.filter(state__in=["open", "submitted"]).update(
        state="cancelled", updated_at=timezone.now()
    )
