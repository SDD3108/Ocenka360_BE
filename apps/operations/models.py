import uuid

from django.conf import settings
from django.db import models

from apps.orders.models import AppendOnlyRecord


class Task(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Открыта"
        DONE = "done", "Выполнена"
        CANCELLED = "cancelled", "Отменена"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="review_tasks")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client_request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    title = models.CharField(max_length=160)
    note = models.TextField(max_length=1000, blank=True)
    due_at = models.DateTimeField()
    state = models.CharField(max_length=12, choices=State.choices, default=State.OPEN)
    version = models.PositiveIntegerField(default=1)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("due_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["created_by", "client_request_id"], name="review_task_request_unique"
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="review_task_version_positive"),
        ]
        indexes = [models.Index(fields=["state", "due_at"], name="review_task_due_idx")]


class TaskEvent(AppendOnlyRecord):
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="history")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("version",)
        constraints = [models.UniqueConstraint(fields=["task", "version"], name="review_task_history_unique")]


class Reminder(AppendOnlyRecord):
    key = models.CharField(max_length=120, primary_key=True)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT)
    task = models.ForeignKey(Task, null=True, on_delete=models.PROTECT)
    local_date = models.DateField()
    kind = models.CharField(max_length=20, choices=[("report", "Отчёт"), ("task", "Задача")])
    created_at = models.DateTimeField(auto_now_add=True)


TASK_STATE_CHOICES = Task.State.choices
