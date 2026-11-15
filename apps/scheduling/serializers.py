import re
from datetime import datetime, timedelta

from django.utils import timezone
from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer

from .models import Appointment, CalendarBlock, Inspection, ScheduleEvent


class OffsetDateTimeField(serializers.DateTimeField):
    def to_internal_value(self, value):
        if isinstance(value, datetime):
            if timezone.is_naive(value):
                raise serializers.ValidationError("UTC offset required, e.g. +05:00 or Z.")
        elif not isinstance(value, str) or not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", value):
            raise serializers.ValidationError("UTC offset required, e.g. +05:00 or Z.")
        return super().to_internal_value(value)


class VersionSerializer(StrictSerializer):
    expected_version = serializers.IntegerField(min_value=1)


class RequestSerializer(VersionSerializer):
    client_request_id = serializers.UUIDField()
    mode = serializers.ChoiceField(choices=Appointment.Mode.choices)
    starts_at = OffsetDateTimeField()
    ends_at = OffsetDateTimeField()
    note = serializers.CharField(max_length=1000, allow_blank=True, default="")


class PromiseSerializer(VersionSerializer):
    starts_at = OffsetDateTimeField()
    ends_at = OffsetDateTimeField()
    report_hours = serializers.IntegerField(min_value=1, max_value=48)
    buffer_before_minutes = serializers.IntegerField(min_value=0, max_value=1440, default=0)
    buffer_after_minutes = serializers.IntegerField(min_value=0, max_value=1440, default=0)


class OfferSerializer(RequestSerializer, PromiseSerializer):
    pass


class AcceptSerializer(VersionSerializer):
    confirmed = serializers.BooleanField()


class ScheduleCancelSerializer(VersionSerializer):
    reason = serializers.CharField(max_length=1000, allow_blank=False)


class StartSerializer(VersionSerializer):
    materials_adequate = serializers.BooleanField(default=False)


class FinishSerializer(VersionSerializer):
    note = serializers.CharField(max_length=1000, allow_blank=False)


class AppointmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Appointment
        fields = [
            "id",
            "order_id",
            "mode",
            "state",
            "starts_at",
            "ends_at",
            "busy_from",
            "busy_until",
            "report_hours",
            "note",
            "confirmed_at",
            "created_at",
        ]
        read_only_fields = fields


class InspectionSerializer(serializers.ModelSerializer):
    overdue = serializers.SerializerMethodField()

    def get_overdue(self, obj) -> bool:
        return obj.order.status in {"in_progress", "awaiting_report"} and obj.report_due_at < timezone.now()

    class Meta:
        model = Inspection
        fields = [
            "id",
            "order_id",
            "appointment_id",
            "started_at",
            "completed_at",
            "report_due_at",
            "overdue",
            "remote_materials_adequate",
            "materials_snapshot",
            "completion_note",
        ]
        read_only_fields = fields


class OverviewSerializer(serializers.Serializer):
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()
    timezone = serializers.CharField()
    appointments = AppointmentSerializer(many=True)
    inspection = InspectionSerializer(allow_null=True)


class ResultSerializer(serializers.Serializer):
    appointment = AppointmentSerializer()
    order_version = serializers.IntegerField()
    order_status = serializers.CharField()


class EventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScheduleEvent
        fields = ["id", "action", "snapshot", "created_at"]
        read_only_fields = fields


class RangeQuery(StrictSerializer):
    starts_at = OffsetDateTimeField()
    ends_at = OffsetDateTimeField()

    def validate(self, attrs):
        if not timedelta(0) < attrs["ends_at"] - attrs["starts_at"] <= timedelta(days=31):
            raise serializers.ValidationError("Choose a positive range of at most 31 days.")
        return attrs


class BlockCreateSerializer(RangeQuery):
    client_request_id = serializers.UUIDField()
    reason = serializers.CharField(max_length=300, allow_blank=False)


class BlockCancelSerializer(StrictSerializer):
    reason = serializers.CharField(max_length=1000, allow_blank=False)


class BlockSerializer(serializers.ModelSerializer):
    class Meta:
        model = CalendarBlock
        fields = ["id", "starts_at", "ends_at", "reason", "cancelled_at", "created_at"]
        read_only_fields = fields


class CalendarSerializer(serializers.Serializer):
    timezone = serializers.CharField()
    appointments = AppointmentSerializer(many=True)
    blocks = BlockSerializer(many=True)
    report_deadlines = InspectionSerializer(many=True)
    truncated = serializers.BooleanField()
