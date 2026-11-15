from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import Notification, PushDelivery, PushDevice


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ("id", "recipient", "kind", "audience", "read_at", "created_at")
    list_filter = ("kind", "audience")


@admin.register(PushDelivery)
class DeliveryAdmin(ReadOnlyAdmin):
    list_display = ("id", "notification", "state", "attempts", "error_code", "updated_at")
    list_filter = ("state", "error_code")


@admin.register(PushDevice)
class DeviceAdmin(ReadOnlyAdmin):
    exclude = ("token",)
    list_display = ("id", "user", "platform", "active", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        return tuple(f.name for f in self.model._meta.fields if f.name != "token")
