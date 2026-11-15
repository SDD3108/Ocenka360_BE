from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer
from apps.orders.models import Order
from apps.orders.serializers import VersionSerializer

from .models import Task, TaskEvent
from .services import next_action


class TaskCreate(StrictSerializer):
    order_id = serializers.UUIDField()
    client_request_id = serializers.UUIDField()
    title = serializers.CharField(max_length=160)
    note = serializers.CharField(max_length=1000, allow_blank=True, required=False, default="")
    due_at = serializers.DateTimeField()


class TaskPatch(VersionSerializer):
    title = serializers.CharField(max_length=160, required=False)
    note = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    due_at = serializers.DateTimeField(required=False)
    state = serializers.ChoiceField(choices=Task.State.choices, required=False)

    def validate(self, data):
        if set(data) == {"expected_version"}:
            raise serializers.ValidationError("Provide a field to update.")
        return data


class TaskQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    order_id = serializers.UUIDField(required=False)
    state = serializers.ChoiceField(choices=Task.State.choices, required=False)
    overdue = serializers.BooleanField(required=False)


class TaskSerializer(serializers.ModelSerializer):
    order_number = serializers.CharField(source="order.number", read_only=True)
    is_overdue = serializers.SerializerMethodField()

    def get_is_overdue(self, obj) -> bool:
        return obj.state == "open" and obj.due_at < timezone.now()

    class Meta:
        model = Task
        fields = (
            "id",
            "order_id",
            "order_number",
            "title",
            "note",
            "due_at",
            "state",
            "version",
            "completed_at",
            "created_at",
            "updated_at",
            "is_overdue",
        )
        read_only_fields = fields


class TaskEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskEvent
        fields = ("version", "snapshot", "created_at")
        read_only_fields = fields


class WorkQuery(StrictSerializer):
    page = serializers.IntegerField(min_value=1, required=False)
    overdue = serializers.BooleanField(required=False, default=False)


class WorkSerializer(serializers.ModelSerializer):
    next_action = serializers.SerializerMethodField()
    report_due_at = serializers.DateTimeField(
        source="inspection.report_due_at", read_only=True, allow_null=True, default=None
    )
    is_overdue = serializers.SerializerMethodField()

    def get_next_action(self, obj) -> str:
        return next_action(obj)

    def get_is_overdue(self, obj) -> bool:
        return bool(
            obj.status in {"in_progress", "awaiting_report"}
            and hasattr(obj, "inspection")
            and obj.inspection.report_due_at < timezone.now()
        )

    class Meta:
        model = Order
        fields = ("id", "number", "status", "version", "next_action", "report_due_at", "is_overdue")
        read_only_fields = fields


class SummaryQuery(StrictSerializer):
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    include_test = serializers.BooleanField(required=False, default=False)

    def validate(self, data):
        today = timezone.localdate()
        data.setdefault("end_date", today)
        data.setdefault("start_date", data["end_date"] - timedelta(days=29))
        if data["end_date"] > today or not 0 <= (data["end_date"] - data["start_date"]).days <= 365:
            raise serializers.ValidationError(
                "Use an inclusive date range of at most 366 days ending no later than today."
            )
        return data


class SourceCount(serializers.Serializer):
    acquisition_source = serializers.CharField()
    count = serializers.IntegerField()


class StatusCount(serializers.Serializer):
    status = serializers.CharField()
    count = serializers.IntegerField()


class SummarySerializer(serializers.Serializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    timezone = serializers.CharField()
    include_test = serializers.BooleanField()
    currency = serializers.CharField()
    new_paid_orders = serializers.IntegerField()
    completed_orders = serializers.IntegerField()
    report_publications = serializers.IntegerField()
    active_orders = serializers.IntegerField()
    overdue_reports = serializers.IntegerField()
    overdue_manual_tasks = serializers.IntegerField()
    originals_ready = serializers.IntegerField()
    pending_refunds = serializers.IntegerField()
    gross_receipts = serializers.DecimalField(max_digits=20, decimal_places=2)
    refunds = serializers.DecimalField(max_digits=20, decimal_places=2)
    net_cash_flow = serializers.DecimalField(max_digits=20, decimal_places=2)
    sources = SourceCount(many=True)
    current_statuses = StatusCount(many=True)
