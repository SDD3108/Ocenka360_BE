from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from rest_framework.exceptions import APIException

from apps.catalog.admin import CatalogAdmin
from apps.catalog.models import Service
from apps.orders.admin import ReadOnlyAdmin

from . import services
from .models import (
    PriceQuote,
    PricingAudit,
    PromoCode,
    PromoRedemption,
    PromoSelection,
    ServiceTariff,
    SubmissionEstimate,
    TariffRevision,
)


@admin.register(TariffRevision)
class TariffAdmin(CatalogAdmin):
    list_display = ("service", "version", "mode", "base_amount", "published_at")
    list_filter = ("service", "mode", "published_at")
    actions = ("publish_selected", "clone_selected")

    def get_readonly_fields(self, request, obj=None):
        fields = ("id", "version", "created_by", "published_by", "created_at", "published_at")
        return fields + (("service",) if obj else ())

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.published_at)

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        if not change:
            Service.objects.select_for_update().get(pk=obj.service_id)
            obj.version = (
                TariffRevision.objects.filter(service_id=obj.service_id).aggregate(v=Max("version"))["v"] or 0
            ) + 1
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
        services.audit(request.user, "tariff_draft_saved", obj)

    def one(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Выберите ровно одну версию.", messages.ERROR)
            return None
        return queryset.get()

    @admin.action(description="Опубликовать выбранную версию")
    def publish_selected(self, request, queryset):
        item = self.one(request, queryset)
        if item is None:
            return
        binding = ServiceTariff.objects.filter(service=item.service).first()
        try:
            services.publish_tariff(request.user, item.pk, binding.current_id if binding else None)
        except (APIException, ValidationError) as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, "Тариф опубликован. Предыдущие версии сохранены.")

    @admin.action(description="Создать новую черновую копию")
    def clone_selected(self, request, queryset):
        item = self.one(request, queryset)
        if item is None:
            return
        data = {
            name: getattr(item, name)
            for name in ("mode", "base_amount", "included_area", "per_sqm", "urgent_surcharge")
        }
        try:
            draft = services.create_tariff(request.user, {"service_id": item.service_id, **data})
        except APIException as exc:
            self.message_user(request, str(exc), messages.ERROR)
        else:
            self.message_user(request, f"Создан черновик v{draft.version}. Измените его и опубликуйте.")


@admin.register(PromoCode)
class PromoAdmin(CatalogAdmin):
    list_display = ("code", "kind", "value", "service", "is_active", "ends_at", "version")
    list_filter = ("kind", "is_active", "service")
    search_fields = ("code",)
    actions = ("enable_codes", "disable_codes")
    readonly_fields = ("id", "created_by", "created_at", "version")

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and obj is None

    def save_model(self, request, obj, form, change):
        obj.created_by = request.user
        super().save_model(request, obj, form, change)
        services.audit(request.user, "promo_created", obj)

    def toggle(self, request, queryset, enabled):
        for promo in queryset:
            try:
                services.toggle_promo(request.user, promo.pk, promo.version, enabled)
            except APIException as exc:
                self.message_user(request, str(exc), messages.ERROR)
                return
        self.message_user(request, "Состояние промокодов обновлено.")

    @admin.action(description="Включить выбранные промокоды")
    def enable_codes(self, request, queryset):
        self.toggle(request, queryset, True)

    @admin.action(description="Выключить выбранные промокоды")
    def disable_codes(self, request, queryset):
        self.toggle(request, queryset, False)


@admin.register(PriceQuote)
class PriceQuoteAdmin(ReadOnlyAdmin):
    list_display = ("id", "order", "revision", "state", "total", "expires_at")
    list_filter = ("state",)


admin.site.register(ServiceTariff, ReadOnlyAdmin)
admin.site.register(PromoSelection, ReadOnlyAdmin)
admin.site.register(PromoRedemption, ReadOnlyAdmin)
admin.site.register(PricingAudit, ReadOnlyAdmin)

admin.site.register(SubmissionEstimate, ReadOnlyAdmin)
