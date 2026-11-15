from django.conf import settings
from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer
from apps.catalog.serializers import ValidateAnswersSerializer

from .models import Asset, Order, OrderEvent, OrderSubmission


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = (
            "id",
            "title",
            "kind",
            "city",
            "address",
            "description",
            "is_archived",
            "version",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class AssetCreateSerializer(StrictSerializer):
    title = serializers.CharField(max_length=160)
    kind = serializers.ChoiceField(choices=Asset.Kind.choices)
    city = serializers.CharField(max_length=100)
    address = serializers.CharField(max_length=300, required=False, default="", allow_blank=True)
    description = serializers.CharField(max_length=2000, required=False, default="", allow_blank=True)


class AssetPatchSerializer(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=160, required=False)
    kind = serializers.ChoiceField(choices=Asset.Kind.choices, required=False)
    city = serializers.CharField(max_length=100, required=False)
    address = serializers.CharField(max_length=300, required=False, allow_blank=True)
    description = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    is_archived = serializers.BooleanField(required=False)

    def validate(self, data):
        if set(data) == {"expected_version"}:
            raise serializers.ValidationError("Provide a field to update.")
        return data


class AnswersMixin:
    def validate_answers(self, value):
        return ValidateAnswersSerializer().validate_answers(value)


class OrderCreateSerializer(AnswersMixin, StrictSerializer):
    client_request_id = serializers.UUIDField(
        help_text="Generate once per new draft; reuse only for identical retries."
    )
    asset_id = serializers.UUIDField()
    service_id = serializers.UUIDField()
    form_id = serializers.UUIDField()
    language = serializers.ChoiceField(choices=settings.LANGUAGES, default="ru")
    answers = serializers.JSONField(required=False, default=dict)


class VersionSerializer(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)


class OrderPatchSerializer(AnswersMixin, VersionSerializer):
    answers = serializers.JSONField(required=False)
    form_id = serializers.UUIDField(required=False)

    def validate(self, data):
        if set(data) == {"expected_version"}:
            raise serializers.ValidationError("Provide answers or form_id.")
        return data


class SubmitSerializer(VersionSerializer):
    confirmed = serializers.BooleanField()


class CancelSerializer(VersionSerializer):
    reason = serializers.CharField(max_length=1000, required=False, default="", allow_blank=True)


class ReviewActionSerializer(VersionSerializer):
    action = serializers.ChoiceField(choices=["start_review", "request_information", "accept", "reject"])
    message = serializers.CharField(max_length=1000, required=False, default="", allow_blank=True)


class OrderSummarySerializer(serializers.ModelSerializer):
    service_slug = serializers.CharField(source="service_snapshot.slug", read_only=True)

    class Meta:
        model = Order
        fields = (
            "id",
            "number",
            "asset_id",
            "service_id",
            "service_slug",
            "form_id",
            "language",
            "status",
            "version",
            "payment_status",
            "is_test",
            "confirmed_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class OrderDetailSerializer(OrderSummarySerializer):
    class Meta(OrderSummarySerializer.Meta):
        fields = (
            *OrderSummarySerializer.Meta.fields,
            "answers",
            "asset_snapshot",
            "service_snapshot",
            "review_unlocked_at",
        )
        read_only_fields = fields


class CustomerContactSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    first_name = serializers.CharField(read_only=True)
    last_name = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True)
    phone_number = serializers.CharField(read_only=True)
    preferred_language = serializers.CharField(read_only=True)


class ReviewOrderDetailSerializer(OrderDetailSerializer):
    customer = CustomerContactSerializer(source="owner", read_only=True)

    class Meta(OrderDetailSerializer.Meta):
        fields = (*OrderDetailSerializer.Meta.fields, "customer")
        read_only_fields = fields


class OrderEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderEvent
        fields = (
            "id",
            "version",
            "actor_role",
            "action",
            "from_status",
            "to_status",
            "message",
            "created_at",
        )
        read_only_fields = fields


class OrderSubmissionSerializer(serializers.ModelSerializer):
    materials_snapshot = serializers.SerializerMethodField()
    price_estimate_snapshot = serializers.SerializerMethodField()

    def get_price_estimate_snapshot(self, obj) -> dict | None:
        estimate = getattr(obj, "price_estimate", None)
        return estimate.snapshot if estimate else None

    def get_materials_snapshot(self, obj) -> dict | None:
        # Part 4 records had no attachments: never invent a manifest for historical submissions.
        manifest = getattr(obj, "material_manifest", None)
        return manifest.snapshot if manifest else None

    class Meta:
        model = OrderSubmission
        fields = (
            "id",
            "version",
            "form_id",
            "form_checksum",
            "materials_snapshot",
            "price_estimate_snapshot",
            "answers",
            "asset_snapshot",
            "service_snapshot",
            "created_at",
        )
        read_only_fields = fields


class OrderQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    status = serializers.ChoiceField(choices=Order.Status.choices, required=False)


class AssetQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    archived = serializers.BooleanField(required=False, default=False)
