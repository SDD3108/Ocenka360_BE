import uuid

from django.conf import settings
from django.db import models

from apps.orders.models import AppendOnlyRecord


class Report(models.Model):
    class State(models.TextChoices):
        DRAFT = "draft", "Черновик"
        PUBLISHED = "published", "Доступен клиенту"
        SUPERSEDED = "superseded", "Заменён новой версией"
        WITHDRAWN = "withdrawn", "Отозван"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="reports")
    revision = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_reports"
    )
    client_upload_id = models.UUIDField()
    report_number = models.CharField(max_length=80)
    title = models.CharField(max_length=160)
    issue_date = models.DateField()
    note = models.TextField(max_length=1000, blank=True)
    state = models.CharField(max_length=12, choices=State.choices, default=State.DRAFT)
    storage_key = models.CharField(max_length=200, unique=True, editable=False)
    sha256 = models.CharField(max_length=64)
    byte_size = models.PositiveBigIntegerField()
    page_count = models.PositiveIntegerField()
    scan_status = models.CharField(max_length=20)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="published_reports"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    change_reason = models.TextField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-revision",)
        constraints = [
            models.UniqueConstraint(fields=["order", "revision"], name="report_order_revision"),
            models.UniqueConstraint(fields=["order", "client_upload_id"], name="report_upload_unique"),
            models.UniqueConstraint(
                fields=["order"], condition=models.Q(state="published"), name="report_one_current"
            ),
            models.CheckConstraint(condition=models.Q(byte_size__gt=0), name="report_size_positive"),
            models.CheckConstraint(
                condition=~models.Q(state__in=["published", "superseded"])
                | models.Q(published_at__isnull=False, published_by__isnull=False),
                name="report_publication_evidence",
            ),
        ]


class Original(models.Model):
    class State(models.TextChoices):
        PREPARING = "preparing", "Готовится"
        READY = "ready", "Готов к выдаче"
        HANDED_OVER = "handed_over", "Передан"

    report = models.OneToOneField(Report, primary_key=True, on_delete=models.PROTECT, related_name="original")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PREPARING)
    ready_at = models.DateTimeField(null=True, blank=True)
    handed_over_at = models.DateTimeField(null=True, blank=True)
    handover_method = models.CharField(
        max_length=12, choices=[("pickup", "Самовывоз"), ("courier", "Передан курьеру")], blank=True
    )
    handover_note = models.TextField(max_length=500, blank=True)
    delivery_requested_at = models.DateTimeField(null=True, blank=True)
    delivery_request_id = models.UUIDField(null=True, blank=True)
    delivery_note = models.TextField(max_length=500, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(state="handed_over")
                | models.Q(handed_over_at__isnull=False, ready_at__isnull=False),
                name="original_handover_evidence",
            )
        ]


class ReportAccess(AppendOnlyRecord):
    report = models.ForeignKey(Report, on_delete=models.PROTECT, related_name="accesses")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")


REPORT_STATE_CHOICES = Report.State.choices
ORIGINAL_STATE_CHOICES = Original.State.choices
