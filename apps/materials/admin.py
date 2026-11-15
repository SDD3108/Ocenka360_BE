from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import Material, MaterialManifest, SubmissionMaterial


@admin.register(Material)
class MaterialAdmin(ReadOnlyAdmin):
    list_display = ("id", "order", "kind", "slot_key", "source", "scan_status", "review_status", "created_at")
    list_filter = ("kind", "source", "scan_status", "review_status")
    # No direct file URLs or editable file fields in Django Admin.
    exclude = ("storage_key", "original_sha256")

    def get_readonly_fields(self, request, obj=None):
        return tuple(f for f in super().get_readonly_fields(request, obj) if f not in self.exclude)


admin.site.register(MaterialManifest, ReadOnlyAdmin)
admin.site.register(SubmissionMaterial, ReadOnlyAdmin)
