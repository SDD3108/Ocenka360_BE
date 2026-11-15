from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import (
    CancellationCase,
    PaymentAccount,
    PaymentAudit,
    PaymentIntent,
    Receipt,
    Refund,
    RefundEntry,
)


@admin.register(PaymentIntent)
class PaymentIntentAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "order",
        "purpose",
        "provider",
        "environment",
        "amount",
        "state",
        "needs_review",
        "created_at",
    )
    list_filter = ("provider", "environment", "state", "needs_review")
    search_fields = ("order__number", "invoice_id")


@admin.register(Receipt)
class ReceiptAdmin(ReadOnlyAdmin):
    list_display = ("id", "intent", "amount", "verification", "transaction_id", "created_at")
    list_filter = ("provider", "environment", "verification")


@admin.register(Refund)
class RefundAdmin(ReadOnlyAdmin):
    list_display = ("id", "receipt", "amount", "state", "reason", "updated_at")
    list_filter = ("state",)


@admin.register(RefundEntry)
class RefundEntryAdmin(ReadOnlyAdmin):
    list_display = ("id", "refund", "amount", "verification", "created_at")


@admin.register(CancellationCase)
class CancellationCaseAdmin(ReadOnlyAdmin):
    list_display = ("id", "order", "state", "retained_amount", "created_at")
    list_filter = ("state",)


@admin.register(PaymentAccount)
class PaymentAccountAdmin(ReadOnlyAdmin):
    list_display = ("order", "advance_amount", "created_at")


@admin.register(PaymentAudit)
class PaymentAuditAdmin(ReadOnlyAdmin):
    list_display = ("order", "actor", "action", "reference_id", "created_at")
