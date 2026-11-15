from django.contrib import admin

from .models import Asset, Order, OrderEvent, OrderSubmission


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(f.name for f in self.model._meta.fields)


@admin.register(Asset)
class AssetAdmin(ReadOnlyAdmin):
    list_display = ("id", "owner", "kind", "is_archived", "version", "created_at")
    list_filter = ("kind", "is_archived")


@admin.register(Order)
class OrderAdmin(ReadOnlyAdmin):
    list_display = ("number", "owner", "status", "is_test", "payment_status", "version", "created_at")
    list_filter = ("status", "is_test")
    search_fields = ("number",)


@admin.register(OrderEvent)
class OrderEventAdmin(ReadOnlyAdmin):
    list_display = ("order", "version", "action", "actor_role", "created_at")


@admin.register(OrderSubmission)
class OrderSubmissionAdmin(ReadOnlyAdmin):
    list_display = ("order", "version", "form", "created_at")
