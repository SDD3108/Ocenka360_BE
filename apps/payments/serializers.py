from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer
from apps.orders.serializers import VersionSerializer
from apps.pricing.serializers import MoneyField

from .models import CancellationCase, PaymentAudit, PaymentIntent, Receipt, Refund, RefundEntry


class PaymentCreateSerializer(VersionSerializer):
    client_request_id = serializers.UUIDField()
    purpose = serializers.ChoiceField(choices=["advance", "balance"])
    provider = serializers.ChoiceField(choices=PaymentIntent.Provider.choices)


class BankReferenceField(serializers.RegexField):
    def __init__(self, **kwargs):
        super().__init__(r"^[A-Za-z0-9_-]{1,128}$", **kwargs)


class BankConfirmationSerializer(VersionSerializer):
    transaction_id = BankReferenceField()
    amount = MoneyField()
    confirmed_in_bank = serializers.BooleanField()


class RefundExecuteSerializer(VersionSerializer):
    confirmed = serializers.BooleanField()


class PaymentBalanceSerializer(serializers.Serializer):
    currency = serializers.CharField()
    advance_amount = serializers.DecimalField(max_digits=16, decimal_places=2)
    agreed_total = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    retained_amount = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    received = serializers.DecimalField(max_digits=16, decimal_places=2)
    refunded = serializers.DecimalField(max_digits=16, decimal_places=2)
    net_paid = serializers.DecimalField(max_digits=16, decimal_places=2)
    refund_pending = serializers.DecimalField(max_digits=16, decimal_places=2)
    amount_due = serializers.DecimalField(max_digits=16, decimal_places=2)
    overpayment = serializers.DecimalField(max_digits=16, decimal_places=2)
    is_test = serializers.BooleanField()
    legacy_simulation = serializers.BooleanField()
    fiscal_status = serializers.CharField()


class ReceiptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Receipt
        fields = ["id", "intent_id", "amount", "verification", "created_at"]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.number", read_only=True)
    receipt = ReceiptSerializer(read_only=True, allow_null=True)

    class Meta:
        model = PaymentIntent
        fields = [
            "id",
            "order_id",
            "order_number",
            "purpose",
            "provider",
            "environment",
            "amount",
            "currency",
            "state",
            "expires_at",
            "needs_review",
            "last_checked_at",
            "receipt",
            "created_at",
        ]
        read_only_fields = fields


class RefundEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = RefundEntry
        fields = ["id", "amount", "verification", "created_at"]
        read_only_fields = fields


class RefundSerializer(serializers.ModelSerializer):
    entry = RefundEntrySerializer(read_only=True, allow_null=True)

    class Meta:
        model = Refund
        fields = ["id", "receipt_id", "amount", "reason", "state", "entry", "created_at", "updated_at"]
        read_only_fields = fields


class PaymentOverviewSerializer(serializers.Serializer):
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()
    balance = PaymentBalanceSerializer()
    intents = PaymentSerializer(many=True)
    refunds = RefundSerializer(many=True)


class PaymentResultSerializer(serializers.Serializer):
    payment = PaymentSerializer()
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()
    balance = PaymentBalanceSerializer()


class PaymentMethodsSerializer(serializers.Serializer):
    halyk_environment = serializers.CharField(allow_null=True)
    mock = serializers.BooleanField()
    kaspi_manual = serializers.BooleanField()
    halyk = serializers.BooleanField()
    advance_amount = MoneyField()
    currency = serializers.CharField()


class CheckoutSerializer(serializers.Serializer):
    kind = serializers.CharField()
    url = serializers.URLField(required=False)
    amount = serializers.CharField(required=False)
    reference = serializers.CharField(required=False)
    amount_prefilled = serializers.BooleanField(required=False)
    verification = serializers.CharField(required=False)
    script_url = serializers.URLField(required=False)
    payment_object = serializers.DictField(required=False)
    is_test = serializers.BooleanField()


class NoInputSerializer(StrictSerializer):
    pass


class CallbackSerializer(serializers.Serializer):
    # Provider callback has additional fields. We read only these, discard everything else.
    invoiceId = serializers.RegexField(r"^[0-9]{6}$")
    secret_hash = serializers.CharField(max_length=128)


class CallbackResultSerializer(serializers.Serializer):
    status = serializers.CharField()


class CaseCreateSerializer(VersionSerializer):
    client_request_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=1000)


class CaseOfferSerializer(VersionSerializer):
    retained_amount = MoneyField()
    comment = serializers.CharField(max_length=1000)


class CaseDecisionSerializer(VersionSerializer):
    action = serializers.ChoiceField(choices=["accept", "withdraw"])
    confirmed = serializers.BooleanField(default=False)


class CaseSerializer(serializers.ModelSerializer):
    class Meta:
        model = CancellationCase
        fields = [
            "id",
            "order_id",
            "reason",
            "state",
            "retained_amount",
            "review_comment",
            "created_at",
            "decided_at",
        ]
        read_only_fields = fields


class CaseResultSerializer(serializers.Serializer):
    case = CaseSerializer()
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()


class PaymentEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentAudit
        fields = ["id", "action", "reference_id", "created_at"]
        read_only_fields = fields


class PaymentQueueQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    state = serializers.ChoiceField(choices=PaymentIntent.State.choices, required=False)
    provider = serializers.ChoiceField(choices=PaymentIntent.Provider.choices, required=False)
    needs_review = serializers.BooleanField(required=False)
