import uuid

from django.conf import settings
from django.db import models

from apps.orders.models import AppendOnlyRecord


class Comment(AppendOnlyRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    author_role = models.CharField(max_length=16)
    body = models.TextField(max_length=2000)
    client_request_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=["order", "author", "client_request_id"], name="comment_request_unique"
            )
        ]


class MaterialRequest(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Нужен ответ клиента"
        SUBMITTED = "submitted", "Материалы отправлены"
        RESOLVED = "resolved", "Приняты оценщиком"
        CANCELLED = "cancelled", "Запрос закрыт"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="material_requests")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client_request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    message = models.TextField(max_length=2000)
    items = models.JSONField()
    state = models.CharField(max_length=12, choices=State.choices, default=State.OPEN)
    review_comment = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=["order", "author", "client_request_id"], name="material_request_id_unique"
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(state__in=["open", "submitted"]),
                name="material_one_active_request",
            ),
        ]


class MaterialReply(AppendOnlyRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(MaterialRequest, on_delete=models.PROTECT, related_name="replies")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client_request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    body = models.TextField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(fields=["request", "client_request_id"], name="material_reply_id_unique")
        ]


class ReplyMaterial(AppendOnlyRecord):
    reply = models.ForeignKey(MaterialReply, on_delete=models.PROTECT, related_name="links")
    material = models.ForeignKey("materials.Material", on_delete=models.PROTECT, related_name="reply_links")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["reply", "material"], name="reply_material_unique")]
