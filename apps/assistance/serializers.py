from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer

from .models import Job, Permission
from .policy import DISCLAIMER


class PermissionInput(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=0)
    policy_version = serializers.CharField(max_length=50)
    enabled = serializers.BooleanField()
    allow_photos = serializers.BooleanField(default=False)
    confirmed = serializers.BooleanField()


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ("order_id", "policy_version", "version", "enabled", "allow_photos", "updated_at")
        read_only_fields = fields


class JobInput(StrictSerializer):
    client_request_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=Job.Kind.choices)
    policy_version = serializers.CharField(max_length=50)
    confirmed = serializers.BooleanField()
    question = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    order_id = serializers.UUIDField(required=False)
    expected_version = serializers.IntegerField(min_value=1, required=False)
    service_id = serializers.UUIDField(required=False)
    field_key = serializers.RegexField(r"^[a-z][a-z0-9_]{0,49}$", required=False)
    photo_ids = serializers.ListField(
        child=serializers.UUIDField(), max_length=4, required=False, default=list
    )
    photos_redacted = serializers.BooleanField(default=False)

    def validate(self, data):
        kind = data["kind"]
        if bool(data.get("order_id")) != ("expected_version" in data):
            raise serializers.ValidationError("Order requests require expected_version.")
        if kind == "service_guide" and (
            data.get("order_id") or data.get("service_id") or data.get("field_key")
        ):
            raise serializers.ValidationError("Service guide uses the published catalog only.")
        if kind == "service_guide" and not data["question"]:
            raise serializers.ValidationError(
                {"question": "Describe the object/purpose without personal data."}
            )
        if kind == "form_help":
            if not data.get("field_key") or bool(data.get("order_id")) == bool(data.get("service_id")):
                raise serializers.ValidationError("Choose exactly one order or service and a field_key.")
        elif data.get("field_key") or data.get("service_id"):
            raise serializers.ValidationError("Service/field are only for form_help.")
        if kind in {"material_check", "photo_review", "description_draft"} and not data.get("order_id"):
            raise serializers.ValidationError("This request requires an order.")
        if kind == "photo_review" and not data["photo_ids"]:
            raise serializers.ValidationError("Choose 1–4 photos.")
        if data["photo_ids"] and kind not in {"photo_review", "description_draft"}:
            raise serializers.ValidationError("This task does not accept photos.")
        if len(set(data["photo_ids"])) != len(data["photo_ids"]):
            raise serializers.ValidationError("Duplicate photos.")
        if data["photo_ids"] and not data["photos_redacted"]:
            raise serializers.ValidationError(
                "Confirm that photos contain no unmasked identifiers or documents."
            )
        return data


class JobSerializer(serializers.ModelSerializer):
    disclaimer = serializers.SerializerMethodField()
    stale = serializers.SerializerMethodField()

    def get_disclaimer(self, obj) -> str:
        return DISCLAIMER[obj.language]

    def get_stale(self, obj) -> bool:
        return bool(obj.order_id and obj.order.version != obj.order_version)

    class Meta:
        model = Job
        fields = (
            "id",
            "order_id",
            "kind",
            "audience",
            "language",
            "state",
            "error_code",
            "result",
            "order_version",
            "stale",
            "policy_version",
            "model_name",
            "prompt_version",
            "input_tokens",
            "output_tokens",
            "created_at",
            "finished_at",
            "expires_at",
            "decision",
            "decision_note",
            "decided_at",
            "disclaimer",
        )
        read_only_fields = fields


class JobQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)


class EmptyInput(StrictSerializer):
    pass


class AIDecisionInput(StrictSerializer):
    decision = serializers.ChoiceField(choices=["accepted", "rejected"])
    confirmed = serializers.BooleanField()
    note = serializers.CharField(max_length=500, required=False, default="", allow_blank=True)


class ToolsSerializer(serializers.Serializer):
    policy_version = serializers.CharField()
    notice = serializers.CharField()
    ai_status = serializers.CharField()
    ai_configured = serializers.BooleanField()
    allowed_kinds = serializers.ListField(child=serializers.CharField())
    market_mode = serializers.CharField()
    market_supported_services = serializers.ListField(child=serializers.CharField())
    damage_calculation_status = serializers.CharField()
    service_fee_calculator = serializers.CharField()
