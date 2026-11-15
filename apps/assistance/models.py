import uuid

from django.conf import settings
from django.db import models

from apps.orders.models import AppendOnlyRecord


class Permission(models.Model):
    order = models.OneToOneField(
        "orders.Order", primary_key=True, on_delete=models.PROTECT, related_name="ai_permission"
    )
    policy_version = models.CharField(max_length=50)
    version = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=False)
    allow_photos = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class PermissionEvent(AppendOnlyRecord):
    permission = models.ForeignKey(Permission, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    policy_version = models.CharField(max_length=50)
    enabled = models.BooleanField()
    allow_photos = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["permission", "version"], name="ai_permission_version")
        ]


class Job(models.Model):
    class Kind(models.TextChoices):
        SERVICE = "service_guide", "Подбор услуги"
        FORM = "form_help", "Помощь с полем анкеты"
        CHECK = "material_check", "Объяснение комплектности"
        PHOTO = "photo_review", "Предварительный анализ фото"
        DRAFT = "description_draft", "Черновик описания"

    class State(models.TextChoices):
        QUEUED = "queued", "В очереди"
        RUNNING = "running", "Обрабатывается"
        SUCCEEDED = "succeeded", "Готово"
        FAILED = "failed", "Ошибка"
        UNKNOWN = "unknown", "Результат внешнего запроса неизвестен"
        CANCELLED = "cancelled", "Отменено"
        STALE = "stale", "Исходные данные изменились"
        EXPIRED = "expired", "Содержимое удалено по сроку"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="ai_jobs")
    order = models.ForeignKey("orders.Order", null=True, on_delete=models.PROTECT, related_name="ai_jobs")
    audience = models.CharField(max_length=10, choices=[("client", "Client"), ("review", "Reviewer")])
    kind = models.CharField(max_length=24, choices=Kind.choices)
    client_request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    policy_version = models.CharField(max_length=50)
    permission_version = models.PositiveIntegerField(null=True)
    order_version = models.PositiveIntegerField(null=True)
    language = models.CharField(max_length=2, choices=settings.LANGUAGES)
    # Only minimized context, never full order answers, IDs or document bytes.
    context = models.JSONField(default=dict)
    photo_refs = models.JSONField(default=list)
    result = models.JSONField(default=dict)
    state = models.CharField(max_length=12, choices=State.choices, default=State.QUEUED)
    error_code = models.CharField(max_length=60, blank=True)
    model_name = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=30)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField()
    decision = models.CharField(
        max_length=10, choices=[("accepted", "Accepted draft"), ("rejected", "Rejected")], blank=True
    )
    decision_note = models.CharField(max_length=500, blank=True)
    decided_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(fields=["actor", "client_request_id"], name="ai_request_unique")
        ]
        indexes = [
            models.Index(fields=["state", "created_at"], name="ai_queue_idx"),
            models.Index(fields=["expires_at"], name="ai_expiry_idx"),
        ]


JOB_KIND_CHOICES = Job.Kind.choices
JOB_STATE_CHOICES = Job.State.choices
