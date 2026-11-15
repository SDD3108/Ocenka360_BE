from django.utils import timezone
from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer
from apps.orders.serializers import VersionSerializer

from .models import Original, Report


class UploadInput(VersionSerializer):
    client_upload_id = serializers.UUIDField()
    report_number = serializers.CharField(max_length=80)
    title = serializers.CharField(max_length=160)
    issue_date = serializers.DateField()
    note = serializers.CharField(max_length=1000, allow_blank=True, required=False, default="")
    file = serializers.FileField()

    def validate_issue_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("Report issue date cannot be in the future.")
        return value


class PublishInput(VersionSerializer):
    confirmed = serializers.BooleanField()
    reason = serializers.CharField(max_length=1000, allow_blank=True, required=False, default="")


class WithdrawInput(PublishInput):
    reason = serializers.CharField(max_length=1000)


class OriginalInput(VersionSerializer):
    action = serializers.ChoiceField(choices=["ready", "handover"])
    confirmed = serializers.BooleanField()
    method = serializers.ChoiceField(choices=["pickup", "courier"], required=False, default="")
    note = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")

    def validate(self, data):
        if data["action"] == "handover" and not data["method"]:
            raise serializers.ValidationError({"method": "Choose pickup or courier."})
        if data["action"] == "ready" and (data["method"] or data["note"]):
            raise serializers.ValidationError("Ready accepts no method/note; use handover for those details.")
        return data


class DeliveryInput(VersionSerializer):
    client_request_id = serializers.UUIDField()
    confirmed = serializers.BooleanField()
    note = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")


class OriginalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Original
        fields = (
            "report_id",
            "state",
            "ready_at",
            "handed_over_at",
            "handover_method",
            "handover_note",
            "delivery_requested_at",
            "delivery_note",
        )
        read_only_fields = fields


class ReportSerializer(serializers.ModelSerializer):
    original = OriginalSerializer(read_only=True, allow_null=True)
    language = serializers.CharField(default="ru", read_only=True)

    class Meta:
        model = Report
        fields = (
            "id",
            "order_id",
            "revision",
            "report_number",
            "title",
            "issue_date",
            "language",
            "note",
            "state",
            "sha256",
            "byte_size",
            "page_count",
            "scan_status",
            "published_at",
            "withdrawn_at",
            "change_reason",
            "created_at",
            "original",
        )
        read_only_fields = fields


class ReportResult(serializers.Serializer):
    report = ReportSerializer()
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()


class OriginalResult(serializers.Serializer):
    original = OriginalSerializer()
    order_version = serializers.IntegerField()
    delivery_contact_url = serializers.URLField()


class ReportQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
