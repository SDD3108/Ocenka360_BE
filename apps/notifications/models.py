import uuid

from django.conf import settings
from django.db import models


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="notifications"
    )
    event = models.ForeignKey("orders.OrderEvent", on_delete=models.PROTECT, related_name="notifications")
    audience = models.CharField(max_length=10, choices=[("client", "Client"), ("review", "Reviewer")])
    kind = models.CharField(max_length=40)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(fields=["recipient", "event"], name="notification_recipient_event")
        ]
        indexes = [
            models.Index(fields=["recipient", "read_at", "-created_at"], name="notification_unread_idx")
        ]


class DeviceLock(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)


class PushDevice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation_id = models.UUIDField(unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_devices")
    session = models.ForeignKey("accounts.AuthSession", on_delete=models.CASCADE)
    token = models.CharField(max_length=256, unique=True)
    platform = models.CharField(max_length=10, choices=[("ios", "iOS"), ("android", "Android")])
    project_id = models.UUIDField()
    active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)


class PushDelivery(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued", "В очереди"
        SENDING = "sending", "Отправляется"
        TICKET = "ticket", "Ожидает квитанции Expo"
        ACCEPTED = "accepted", "Принято APNs/FCM"
        FAILED = "failed", "Ошибка"
        UNKNOWN = "unknown", "Результат неизвестен"
        SKIPPED = "skipped", "Пропущено"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification = models.ForeignKey(Notification, on_delete=models.PROTECT, related_name="deliveries")
    device = models.ForeignKey(PushDevice, null=True, on_delete=models.SET_NULL)
    device_revision = models.PositiveIntegerField()
    state = models.CharField(max_length=12, choices=State.choices, default=State.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    ticket_id = models.CharField(max_length=200, blank=True)
    error_code = models.CharField(max_length=60, blank=True)
    claimed_at = models.DateTimeField(null=True)
    receipt_due_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(fields=["notification", "device"], name="push_notification_device")
        ]
        indexes = [models.Index(fields=["state", "next_attempt_at"], name="push_pending_idx")]
