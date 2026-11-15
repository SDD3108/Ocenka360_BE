from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer
from apps.orders.serializers import VersionSerializer

from .models import Material


class UploadSerializer(VersionSerializer):
    client_upload_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=Material.Kind.choices)
    slot_key = serializers.RegexField(r"^[a-z][a-z0-9_]{0,79}$")
    file = serializers.FileField(max_length=255, allow_empty_file=False)


class MaterialSerializer(serializers.ModelSerializer):
    class Meta:
        model = Material
        fields = (
            "id",
            "client_upload_id",
            "kind",
            "slot_key",
            "source",
            "mime_type",
            "byte_size",
            "sha256",
            "width",
            "height",
            "page_count",
            "scan_status",
            "review_status",
            "review_comment",
            "reviewed_at",
            "withdrawn_at",
            "created_at",
        )
        read_only_fields = fields


class MaterialResultSerializer(serializers.Serializer):
    material = MaterialSerializer(read_only=True)
    order_version = serializers.IntegerField(read_only=True)


class MaterialListSerializer(serializers.Serializer):
    materials = MaterialSerializer(many=True, read_only=True)
    order_version = serializers.IntegerField(read_only=True)


class MaterialReviewSerializer(VersionSerializer):
    decision = serializers.ChoiceField(choices=["accepted", "rejected"])
    comment = serializers.CharField(max_length=1000, allow_blank=True, default="")


class RequirementSlotSerializer(serializers.Serializer):
    key = serializers.CharField()
    title = serializers.CharField()
    help = serializers.CharField(required=False)
    required = serializers.BooleanField()
    provided = serializers.BooleanField()
    when = serializers.JSONField(required=False)


class MissingMaterialSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=Material.Kind.choices)
    slot_key = serializers.CharField()


class RequirementsSerializer(serializers.Serializer):
    guide_version = serializers.IntegerField()
    language = serializers.CharField()
    documents = RequirementSlotSerializer(many=True)
    photos = RequirementSlotSerializer(many=True)
    notice = serializers.CharField()
    remote_requested = serializers.BooleanField()
    missing = MissingMaterialSerializer(many=True)
    complete = serializers.BooleanField()
    order_version = serializers.IntegerField()
    allowed_types = serializers.ListField(child=serializers.CharField())
    max_file_bytes = serializers.IntegerField()


class VersionResultSerializer(StrictSerializer):
    order_version = serializers.IntegerField(read_only=True)
