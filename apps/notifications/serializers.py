from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer

from .messages import body
from .models import Notification, PushDevice


class DeviceInput(StrictSerializer):
    installation_id = serializers.UUIDField()
    token = serializers.RegexField(
        r"^(?:ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_-]{10,200}\]$", max_length=256
    )
    platform = serializers.ChoiceField(choices=["ios", "android"])
    project_id = serializers.UUIDField()


class DeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = PushDevice
        fields = ("id", "installation_id", "platform", "active", "updated_at")
        read_only_fields = fields


class NotificationSerializer(serializers.ModelSerializer):
    body = serializers.SerializerMethodField()
    order_id = serializers.UUIDField(source="event.order_id", read_only=True)
    order_version = serializers.IntegerField(source="event.version", read_only=True)

    def get_body(self, obj) -> str:
        return body(obj.kind, self.context["request"].user.preferred_language)

    class Meta:
        model = Notification
        fields = ("id", "kind", "body", "audience", "order_id", "order_version", "read_at", "created_at")
        read_only_fields = fields


class NotificationQuery(StrictSerializer):
    unread = serializers.BooleanField(required=False)
    page = serializers.IntegerField(min_value=1, required=False)


class EmptyInput(StrictSerializer):
    pass


class CountSerializer(serializers.Serializer):
    unread = serializers.IntegerField()
