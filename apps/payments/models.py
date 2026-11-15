import uuid

from django.conf import settings
from django.db import models

from apps.orders.models import AppendOnlyRecord
from apps.pricing.models import money


class PaymentAccount(models.Model):
    order = models.OneToOneField(
        "orders.Order", primary_key=True, on_delete=models.PROTECT, related_name="payment_account"
    )
    advance_amount = money()
    created_at = models.DateTimeField(auto_now_add=True)


class InvoiceSequence(models.Model):
    # Never recycle the last six digits: ePay explicitly requires uniqueness there as well.
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    next_value = models.PositiveIntegerField(default=100000)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="invoice_sequence_singleton")]


class PaymentIntent(models.Model):
    class Provider(models.TextChoices):
        MOCK = "mock", "Локальная имитация"
        KASPI = "kaspi_manual", "Kaspi Pay — ручная сверка"
        HALYK = "halyk", "Halyk ePay"

    class State(models.TextChoices):
        PENDING = "pending", "Ожидается оплата"
        PAID = "paid", "Платёж учтён"
        CLOSED = "closed", "Оплата больше не предлагается"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payment_intents")
    client_request_id = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    purpose = models.CharField(max_length=10, choices=[("advance", "Аванс"), ("balance", "Остаток")])
    provider = models.CharField(max_length=16, choices=Provider.choices)
    environment = models.CharField(
        max_length=8, choices=[("local", "Local"), ("test", "Bank test"), ("live", "Live")]
    )
    terminal_id = models.CharField(max_length=80, blank=True)
    invoice_id = models.CharField(max_length=15, unique=True, null=True, blank=True)
    amount = money()
    currency = models.CharField(max_length=3, default="KZT")
    state = models.CharField(max_length=10, choices=State.choices, default=State.PENDING)
    expires_at = models.DateTimeField()
    sync_requested_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    needs_review = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["order", "client_request_id"], name="payment_order_request_unique"
            ),
            models.UniqueConstraint(
                fields=["order"], condition=models.Q(state="pending"), name="payment_one_pending"
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(condition=models.Q(currency="KZT"), name="payment_kzt_only"),
        ]


class Receipt(AppendOnlyRecord):
    """Verified payment entry, NOT a fiscal receipt. No PAN/CVV or raw bank response."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intent = models.OneToOneField(PaymentIntent, on_delete=models.PROTECT, related_name="receipt")
    provider = models.CharField(max_length=16, choices=PaymentIntent.Provider.choices)
    environment = models.CharField(max_length=8)
    transaction_id = models.CharField(max_length=128)
    amount = money()
    verification = models.CharField(
        max_length=20,
        choices=[
            ("bank_api", "Bank API"),
            ("manual_bank_review", "Manual bank review"),
            ("simulation", "Simulation"),
        ],
    )
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "environment", "transaction_id"], name="payment_bank_transaction_unique"
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="receipt_amount_positive"),
        ]


class Refund(models.Model):
    class State(models.TextChoices):
        APPROVED = "approved", "К возврату"
        PROCESSING = "processing", "Запрос выполняется"
        SUBMITTED = "submitted", "Запрос принят банком"
        UNKNOWN = "unknown", "Нужна сверка результата"
        COMPLETED = "completed", "Возврат подтверждён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, related_name="refunds")
    amount = money()
    reason = models.CharField(max_length=300)
    state = models.CharField(max_length=12, choices=State.choices, default=State.APPROVED)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="refund_amount_positive")
        ]


class RefundEntry(AppendOnlyRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    refund = models.OneToOneField(Refund, on_delete=models.PROTECT, related_name="entry")
    provider = models.CharField(max_length=16)
    environment = models.CharField(max_length=8)
    transaction_id = models.CharField(max_length=128)
    amount = money()
    verification = models.CharField(max_length=20)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "environment", "transaction_id"], name="refund_bank_transaction_unique"
            )
        ]


class CancellationCase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="cancellation_cases")
    client_request_id = models.UUIDField()
    reason = models.CharField(max_length=1000)
    state = models.CharField(
        max_length=10,
        choices=[
            ("requested", "Запрос"),
            ("offered", "Условия предложены"),
            ("accepted", "Согласовано"),
            ("withdrawn", "Отозвано"),
        ],
        default="requested",
    )
    retained_amount = money(nullable=True)
    review_comment = models.CharField(max_length=1000, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["order", "client_request_id"], name="cancellation_request_unique"
            ),
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(state__in=["requested", "offered"]),
                name="cancellation_one_open",
            ),
        ]


class PaymentAudit(AppendOnlyRecord):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payment_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    reference_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")


PAYMENT_PROVIDER_CHOICES = PaymentIntent.Provider.choices
PAYMENT_STATE_CHOICES = PaymentIntent.State.choices
REFUND_STATE_CHOICES = Refund.State.choices
