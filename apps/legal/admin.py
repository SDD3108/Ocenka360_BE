from django.contrib import admin

from .models import ConsentAcceptance, LegalDocument


class SuperuserOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LegalDocument)
class LegalDocumentAdmin(SuperuserOnlyAdmin):
    list_display = ("kind", "version", "language", "is_draft", "is_active")
    list_filter = ("kind", "language", "is_draft", "is_active")

    def get_readonly_fields(self, request, obj=None):
        fields = ("checksum", "created_at")
        if obj:
            fields += ("kind", "version", "language", "title", "content", "is_draft")
        return fields


@admin.register(ConsentAcceptance)
class ConsentAcceptanceAdmin(SuperuserOnlyAdmin):
    list_display = ("user", "document", "accepted_at")
    readonly_fields = ("user", "document", "accepted_at", "document_checksum", "adult_confirmed")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
