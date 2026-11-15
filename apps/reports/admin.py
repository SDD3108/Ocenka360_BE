from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import Original, Report, ReportAccess


@admin.register(Report)
class ReportAdmin(ReadOnlyAdmin):
    list_display = ("id", "order", "revision", "state", "published_at")
    list_filter = ("state",)


admin.site.register(Original, ReadOnlyAdmin)
admin.site.register(ReportAccess, ReadOnlyAdmin)
