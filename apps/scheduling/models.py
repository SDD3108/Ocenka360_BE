import uuid

from django.conf import settings
from django.db import models

from apps.orders.models import AppendOnlyRecord


class CalendarLock(models.Model):
    """One appraiser, one calendar. Seeded by migration; never recreated during a booking."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="calendar_singleton")]


class Appointment(models.Model):
    class State(models.TextChoices):
        REQUESTED = "requested", "Пожелание клиента"
        OFFERED = "offered", "Предложено оценщиком"
        CONFIRMED = "confirmed", "Подтверждено"
        IN_PROGRESS = "in_progress", "Осмотр начат"
        COMPLETED = "completed", "Осмотр завершён"
        CANCELLED = "cancelled", "Отменено"
        DECLINED = "declined", "Отклонено"
        SUPERSEDED = "superseded", "Заменено"

    class Mode(models.TextChoices):
        ONSITE = "onsite", "Выезд"
        REMOTE = "remote", "По материалам клиента"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="appointments")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client_request_id = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    mode = models.CharField(max_length=12, choices=Mode.choices)
    state = models.CharField(max_length=16, choices=State.choices)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    # Occupied range includes explicitly agreed appraiser travel buffers.
    busy_from = models.DateTimeField()
    busy_until = models.DateTimeField()
    report_hours = models.PositiveSmallIntegerField(null=True, blank=True)
    note = models.TextField(max_length=1000, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(fields=["order", "client_request_id"], name="appointment_request_unique"),
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(state__in=["requested", "offered"]),
                name="appointment_one_pending",
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(state__in=["confirmed", "in_progress"]),
                name="appointment_one_active",
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")), name="appointment_positive_interval"
            ),
            models.CheckConstraint(
                condition=models.Q(busy_from__lte=models.F("starts_at"), busy_until__gte=models.F("ends_at")),
                name="appointment_buffer_contains",
            ),
            models.CheckConstraint(
                condition=models.Q(report_hours__isnull=True)
                | models.Q(report_hours__gte=1, report_hours__lte=48),
                name="appointment_report_hours_range",
            ),
            models.CheckConstraint(
                condition=~models.Q(state__in=["offered", "confirmed", "in_progress", "completed"])
                | models.Q(report_hours__isnull=False),
                name="appointment_promised_hours",
            ),
        ]
        indexes = [models.Index(fields=["state", "busy_from", "busy_until"], name="appointment_overlap_idx")]


class CalendarBlock(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client_request_id = models.UUIDField(unique=True)
    request_fingerprint = models.CharField(max_length=64)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.CharField(max_length=300)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("starts_at", "id")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")), name="calendar_block_positive"
            )
        ]


class Inspection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField("orders.Order", on_delete=models.PROTECT, related_name="inspection")
    appointment = models.OneToOneField(Appointment, on_delete=models.PROTECT)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="started_inspections"
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="completed_inspections",
        null=True,
        blank=True,
    )
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    report_due_at = models.DateTimeField()
    remote_materials_adequate = models.BooleanField(default=False)
    materials_snapshot = models.JSONField(default=list)
    completion_note = models.TextField(max_length=1000, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(report_due_at__gt=models.F("started_at")),
                name="inspection_deadline_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(completed_at__isnull=True)
                | models.Q(completed_at__gte=models.F("started_at")),
                name="inspection_completion_after_start",
            ),
        ]


class InspectionMaterial(models.Model):
    inspection = models.ForeignKey(Inspection, on_delete=models.PROTECT, related_name="material_links")
    material = models.ForeignKey(
        "materials.Material", on_delete=models.PROTECT, related_name="inspection_links"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["inspection", "material"], name="inspection_material_unique")
        ]


class ScheduleEvent(AppendOnlyRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        "orders.Order", on_delete=models.PROTECT, related_name="schedule_events", null=True, blank=True
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")


APPOINTMENT_STATE_CHOICES = Appointment.State.choices
INSPECTION_MODE_CHOICES = Appointment.Mode.choices
