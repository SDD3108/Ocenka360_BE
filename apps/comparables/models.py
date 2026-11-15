import uuid

from django.conf import settings
from django.db import models

from apps.orders.models import AppendOnlyRecord


class Estimate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="market_estimates")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client_request_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    state = models.CharField(
        max_length=12,
        choices=[("draft", "Draft"), ("shared", "Shared with client"), ("withdrawn", "Withdrawn")],
        default="draft",
    )
    version = models.PositiveIntegerField(default=1)
    order_version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    shared_at = models.DateTimeField(null=True)
    withdrawn_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(fields=["created_by", "client_request_id"], name="market_request_unique")
        ]


class EstimateEvent(AppendOnlyRecord):
    estimate = models.ForeignKey(Estimate, on_delete=models.PROTECT, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=15)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
