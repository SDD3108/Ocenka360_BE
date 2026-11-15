import re
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer
from apps.orders.serializers import VersionSerializer

from .models import MAX_MONEY, PriceQuote, PromoCode, TariffRevision


class MoneyField(serializers.DecimalField):
    def __init__(self, **kwargs):
        super().__init__(
            max_digits=11, decimal_places=2, min_value=Decimal("0.00"), max_value=MAX_MONEY, **kwargs
        )

    def to_internal_value(self, data):
        if not isinstance(data, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?", data):
            raise serializers.ValidationError('Send a decimal string, for example "20000.00".')
        return super().to_internal_value(data)


class TariffCreateSerializer(StrictSerializer):
    service_id = serializers.UUIDField()
    mode = serializers.ChoiceField(choices=TariffRevision.Mode.choices)
    base_amount = MoneyField(required=False, allow_null=True, default=None)
    included_area = MoneyField(required=False, allow_null=True, default=None)
    per_sqm = MoneyField(required=False, allow_null=True, default=None)
    urgent_surcharge = MoneyField(required=False, allow_null=True, default=None)

    def validate_included_area(self, value):
        if value is not None and value > Decimal("99999999.99"):
            raise serializers.ValidationError("Included area is too large.")
        return value


class TariffSerializer(serializers.ModelSerializer):
    is_current = serializers.SerializerMethodField()

    def get_is_current(self, obj) -> bool:
        return obj.servicetariff_set.exists()

    class Meta:
        model = TariffRevision
        fields = (
            "id",
            "service_id",
            "version",
            "mode",
            "base_amount",
            "included_area",
            "per_sqm",
            "urgent_surcharge",
            "published_at",
            "is_current",
            "created_at",
        )
        read_only_fields = fields


class PublishSerializer(StrictSerializer):
    expected_current_id = serializers.UUIDField(allow_null=True)


class EstimateInputSerializer(StrictSerializer):
    city = serializers.CharField(max_length=100)
    urgency = serializers.ChoiceField(choices=["regular", "urgent"], default="regular")
    area_sqm = MoneyField(required=False, allow_null=True, default=None)
    language = serializers.ChoiceField(choices=settings.LANGUAGES, default="ru")

    def validate_area_sqm(self, value):
        if value is not None and not Decimal("0.01") <= value <= Decimal("1000000.00"):
            raise serializers.ValidationError("Area must be between 0.01 and 1000000 square meters.")
        return value


class BreakdownSerializer(serializers.Serializer):
    code = serializers.CharField()
    amount = MoneyField()


class EstimateSerializer(serializers.Serializer):
    currency = serializers.CharField()
    service_id = serializers.UUIDField()
    tariff_id = serializers.UUIDField(allow_null=True)
    tariff_version = serializers.IntegerField(allow_null=True)
    status = serializers.CharField()
    reason = serializers.CharField()
    subtotal = MoneyField(allow_null=True)
    breakdown = BreakdownSerializer(many=True)
    notice = serializers.CharField()
    is_final = serializers.BooleanField()


class OrderEstimateSerializer(EstimateSerializer):
    promo_code = serializers.CharField(allow_null=True)
    promo_status = serializers.CharField()
    discount = MoneyField()
    total = MoneyField(allow_null=True)


class CodeMixin:
    def validate_code(self, code):
        code = code.strip().upper()
        if code and not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,31}", code):
            raise serializers.ValidationError("Use 3–32 Latin letters, numbers, underscore or hyphen.")
        return code


class PromoCreateSerializer(CodeMixin, StrictSerializer):
    code = serializers.CharField(max_length=32)
    kind = serializers.ChoiceField(choices=PromoCode.Kind.choices)
    value = MoneyField()
    minimum_subtotal = MoneyField(default="0.00")
    maximum_discount = MoneyField(allow_null=True, default=None)
    service_id = serializers.UUIDField(allow_null=True, default=None)
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    max_uses = serializers.IntegerField(min_value=1, max_value=1000000, allow_null=True, default=None)
    per_user_limit = serializers.IntegerField(min_value=1, max_value=1000, default=1)
    is_active = serializers.BooleanField(default=True)

    def validate(self, data):
        if data["ends_at"] <= timezone.now():
            raise serializers.ValidationError({"ends_at": "Must be in the future."})
        return data


class PromoToggleSerializer(VersionSerializer):
    is_active = serializers.BooleanField()


class PromoSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromoCode
        fields = (
            "id",
            "code",
            "kind",
            "value",
            "minimum_subtotal",
            "maximum_discount",
            "service_id",
            "starts_at",
            "ends_at",
            "max_uses",
            "per_user_limit",
            "is_active",
            "version",
            "created_at",
        )
        read_only_fields = fields


class PromoSelectionSerializer(CodeMixin, VersionSerializer):
    code = serializers.CharField(max_length=32, allow_blank=True)


class QuoteLineSerializer(StrictSerializer):
    kind = serializers.ChoiceField(choices=["appraisal", "travel", "other"])
    title = serializers.CharField(max_length=200)
    amount = MoneyField()

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Line amount must be positive.")
        return value


class QuoteCreateSerializer(VersionSerializer):
    client_request_id = serializers.UUIDField()
    lines = QuoteLineSerializer(many=True, min_length=1, max_length=20)
    terms = serializers.CharField(max_length=2000)
    expires_at = serializers.DateTimeField()


class QuoteSerializer(serializers.ModelSerializer):
    currency = serializers.SerializerMethodField()
    effective_state = serializers.SerializerMethodField()
    lines = QuoteLineSerializer(many=True, read_only=True)
    promotion_notice = serializers.SerializerMethodField()

    def get_promotion_notice(self, obj) -> str:
        if not obj.promo_id:
            return ""
        return {
            "ru": "Скидка подтверждается при согласовании цены и зависит от срока и лимита промокода.",
            "kk": "Жеңілдік баға келісілгенде расталады; промокодтың мерзімі мен лимитіне байланысты.",
            "en": "The discount is confirmed when you accept the price, subject to the promotion expiry and usage limits.",
        }[obj.order.language]

    def get_currency(self, obj) -> str:
        return "KZT"

    def get_effective_state(self, obj) -> str:
        if obj.state == PriceQuote.State.OFFERED and obj.expires_at <= timezone.now():
            return "expired"
        return obj.state

    class Meta:
        model = PriceQuote
        fields = (
            "id",
            "revision",
            "client_request_id",
            "issued_order_version",
            "currency",
            "lines",
            "subtotal",
            "appraisal_subtotal",
            "discount",
            "total",
            "promo_snapshot",
            "promotion_notice",
            "terms",
            "expires_at",
            "state",
            "effective_state",
            "decided_at",
            "decision_comment",
            "created_at",
        )
        read_only_fields = fields


class QuoteDetailSerializer(QuoteSerializer):
    class Meta(QuoteSerializer.Meta):
        fields = (*QuoteSerializer.Meta.fields, "context_snapshot")
        read_only_fields = fields


class QuoteResultSerializer(serializers.Serializer):
    quote = QuoteDetailSerializer(read_only=True)
    order_version = serializers.IntegerField(read_only=True)
    order_status = serializers.CharField(read_only=True)


class PriceOverviewSerializer(serializers.Serializer):
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()
    selected_promo_code = serializers.CharField(allow_null=True)
    quotes = QuoteSerializer(many=True)


class AcceptQuoteSerializer(VersionSerializer):
    confirmed = serializers.BooleanField()


class DeclineQuoteSerializer(VersionSerializer):
    comment = serializers.CharField(max_length=1000, allow_blank=True, default="")


class PricingQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    service_id = serializers.UUIDField(required=False)
