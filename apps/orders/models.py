import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Asset(models.Model):
    class Kind(models.TextChoices):
        APARTMENT = "apartment", "Квартира"
        HOUSE = "house", "Дом"
        LAND = "land", "Земля"
        COMMERCIAL = "commercial", "Нежилой объект"
        VEHICLE = "vehicle", "Транспорт"
        EQUIPMENT = "equipment", "Оборудование"
        BUSINESS = "business", "Бизнес"
        INTANGIBLE = "intangible", "Нематериальные активы"
        BIOLOGICAL = "biological", "Биологические активы"
        OTHER = "other", "Другое"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="assets")
    title = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    city = models.CharField(max_length=100)
    address = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True, max_length=2000)
    is_archived = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=["owner", "is_archived", "-created_at"], name="order_asset_owner_idx")]
        constraints = [
            models.CheckConstraint(condition=models.Q(version__gte=1), name="asset_version_positive")
        ]
        verbose_name = "объект"
        verbose_name_plural = "Объекты"

    def __str__(self):
        return str(self.id)


class Order(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        AWAITING_PAYMENT = "awaiting_payment", "Ожидает оплаты"
        SUBMITTED = "submitted", "Отправлена оценщику"
        UNDER_REVIEW = "under_review", "На рассмотрении"
        NEEDS_INFORMATION = "needs_information", "Нужны уточнения"
        ACCEPTED = "accepted", "Принята для согласования условий"
        PRICE_AGREED = "price_agreed", "Цена согласована клиентом"
        INSPECTION_SCHEDULED = "inspection_scheduled", "Осмотр согласован"
        IN_PROGRESS = "in_progress", "Работа начата"
        AWAITING_REPORT = "awaiting_report", "Готовится отчёт"
        SERVICE_STOPPED = "service_stopped", "Услуга прекращена по согласованию"
        REJECTED = "rejected", "Отказ компании"
        CANCELLED = "cancelled", "Отменена клиентом"

    class PaymentStatus(models.TextChoices):
        NOT_PAID = "not_paid", "Не оплачено"
        PARTIALLY_PAID = "partially_paid", "Частично оплачено"
        PAID = "paid", "Оплачено"
        REFUNDED = "refunded", "Возвращено"
        TEST_CONFIRMED = "test_confirmed", "Тестовая имитация — денег нет"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=40, unique=True, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="orders")
    service = models.ForeignKey("catalog.Service", on_delete=models.PROTECT, related_name="orders")
    form = models.ForeignKey("catalog.FormRevision", on_delete=models.PROTECT, related_name="orders")
    language = models.CharField(
        max_length=2, choices=[("ru", "Русский"), ("kk", "Қазақша"), ("en", "English")]
    )
    answers = models.JSONField(default=dict, blank=True)
    asset_snapshot = models.JSONField(default=dict)
    service_snapshot = models.JSONField(default=dict)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)
    version = models.PositiveIntegerField(default=1)
    client_request_id = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64, editable=False)
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.NOT_PAID
    )
    is_test = models.BooleanField(default=False)
    review_unlocked_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(fields=["owner", "status", "-created_at"], name="order_owner_status_idx"),
            models.Index(fields=["status", "review_unlocked_at"], name="order_review_queue_idx"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["owner", "client_request_id"], name="order_owner_request_unique"),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="order_version_positive"),
            models.CheckConstraint(
                condition=~models.Q(payment_status="test_confirmed") | models.Q(is_test=True),
                name="order_test_payment_only",
            ),
        ]
        verbose_name = "заявка"
        verbose_name_plural = "Заявки"

    def __str__(self):
        return self.number


class AppendOnlyRecord(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("History is immutable. Append a new record instead.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("History cannot be deleted through this operation.")


class OrderEvent(AppendOnlyRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="events")
    version = models.PositiveIntegerField()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="order_events"
    )
    actor_role = models.CharField(max_length=20)
    action = models.CharField(max_length=40)
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30)
    message = models.TextField(blank=True, max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("version",)
        constraints = [
            models.UniqueConstraint(fields=["order", "version"], name="order_event_version_unique")
        ]
        verbose_name = "событие заявки"
        verbose_name_plural = "История заявок"


class OrderSubmission(AppendOnlyRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="submissions")
    version = models.PositiveIntegerField()
    form = models.ForeignKey("catalog.FormRevision", on_delete=models.PROTECT)
    form_checksum = models.CharField(max_length=64)
    answers = models.JSONField()
    asset_snapshot = models.JSONField()
    service_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("version",)
        constraints = [
            models.UniqueConstraint(fields=["order", "version"], name="order_submission_version_unique")
        ]
        verbose_name = "подтверждение анкеты"
        verbose_name_plural = "Подтверждённые анкеты"


# Module-level choices for drf-spectacular imports.
ASSET_KIND_CHOICES = Asset.Kind.choices
ORDER_STATUS_CHOICES = Order.Status.choices
