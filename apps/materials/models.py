import uuid

from django.conf import settings
from django.db import models

from apps.orders.models import AppendOnlyRecord


class Material(models.Model):
    class Kind(models.TextChoices):
        DOCUMENT = "document", "Документ"
        PHOTO = "photo", "Фотография"

    class Source(models.TextChoices):
        CLIENT = "client", "Клиент"
        APPRAISER = "appraiser", "Оценщик"

    class Scan(models.TextChoices):
        CLEAN = "clean", "Антивирусная проверка пройдена"
        LOCAL_UNSCANNED = "local_unscanned", "Локальный тест — без антивируса"

    class Review(models.TextChoices):
        PENDING = "pending", "Ожидает проверки"
        ACCEPTED = "accepted", "Принят оценщиком"
        REJECTED = "rejected", "Нужно заменить"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="materials")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    client_upload_id = models.UUIDField()
    kind = models.CharField(max_length=10, choices=Kind.choices)
    slot_key = models.CharField(max_length=80)
    source = models.CharField(max_length=10, choices=Source.choices)
    # Never exposed by an API serializer; generated UUID path, no user-supplied filename.
    storage_key = models.CharField(max_length=200, unique=True, editable=False)
    mime_type = models.CharField(max_length=30)
    byte_size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    original_sha256 = models.CharField(max_length=64)
    width = models.PositiveIntegerField(null=True)
    height = models.PositiveIntegerField(null=True)
    page_count = models.PositiveIntegerField(null=True)
    scan_status = models.CharField(max_length=20, choices=Scan.choices)
    review_status = models.CharField(max_length=10, choices=Review.choices, default=Review.PENDING)
    review_comment = models.TextField(blank=True, max_length=1000)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="reviewed_materials"
    )
    reviewed_at = models.DateTimeField(null=True)
    withdrawn_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(fields=["order", "client_upload_id"], name="material_upload_unique"),
            models.CheckConstraint(condition=models.Q(byte_size__gt=0), name="material_size_positive"),
        ]
        indexes = [models.Index(fields=["order", "kind", "slot_key"], name="material_order_slot_idx")]

    def __str__(self):
        return str(self.id)


class MaterialManifest(AppendOnlyRecord):
    submission = models.OneToOneField(
        "orders.OrderSubmission", on_delete=models.PROTECT, related_name="material_manifest"
    )
    snapshot = models.JSONField()


class SubmissionMaterial(AppendOnlyRecord):
    manifest = models.ForeignKey(MaterialManifest, on_delete=models.PROTECT, related_name="links")
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name="submission_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["manifest", "material"], name="manifest_material_unique")
        ]


MATERIAL_KIND_CHOICES = Material.Kind.choices
MATERIAL_SOURCE_CHOICES = Material.Source.choices
MATERIAL_SCAN_CHOICES = Material.Scan.choices
MATERIAL_REVIEW_CHOICES = Material.Review.choices
