from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import Appointment, CalendarBlock, Inspection, InspectionMaterial, ScheduleEvent


@admin.register(Appointment)
class AppointmentAdmin(ReadOnlyAdmin):
    list_display = ("id", "order", "mode", "state", "starts_at", "ends_at", "report_hours")
    list_filter = ("state", "mode")
    date_hierarchy = "starts_at"
    search_fields = ("order__number",)


@admin.register(CalendarBlock)
class CalendarBlockAdmin(ReadOnlyAdmin):
    list_display = ("id", "starts_at", "ends_at", "reason", "cancelled_at")
    date_hierarchy = "starts_at"


@admin.register(Inspection)
class InspectionAdmin(ReadOnlyAdmin):
    list_display = ("order", "started_at", "completed_at", "report_due_at", "remote_materials_adequate")
    list_filter = ("remote_materials_adequate",)
    date_hierarchy = "report_due_at"
    search_fields = ("order__number",)


@admin.register(InspectionMaterial)
class InspectionMaterialAdmin(ReadOnlyAdmin):
    list_display = ("inspection", "material")


@admin.register(ScheduleEvent)
class ScheduleEventAdmin(ReadOnlyAdmin):
    list_display = ("order", "actor", "action", "created_at")
    list_filter = ("action",)
