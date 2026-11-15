from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Category, FormRevision, Service
from .services import clone_form, publish_form


class CatalogAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Category)
class CategoryAdmin(CatalogAdmin):
    list_display = ("slug", "__str__", "is_active", "sort_order")
    list_filter = ("is_active",)

    def get_readonly_fields(self, request, obj=None):
        return ("id", "slug") if obj else ("id",)


@admin.register(Service)
class ServiceAdmin(CatalogAdmin):
    list_display = ("slug", "__str__", "category", "is_active", "current_form")
    list_filter = ("category", "is_active")
    search_fields = ("slug", "title__ru")

    def get_readonly_fields(self, request, obj=None):
        base = ("id", "current_form", "updated_at")
        return (*base, "slug") if obj else base


@admin.register(FormRevision)
class FormRevisionAdmin(CatalogAdmin):
    list_display = ("service", "version", "published_at", "created_at")
    list_filter = ("service", "published_at")
    actions = ("publish_selected", "clone_selected")

    def get_readonly_fields(self, request, obj=None):
        base = ("id", "checksum", "published_at", "created_at")
        if obj:
            base += ("service", "version")
            if obj.published_at:
                base += ("definition",)
        return base

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.published_at)

    @admin.action(description="Опубликовать и сделать текущей (одна версия)")
    def publish_selected(self, request, queryset):
        if not self.has_module_permission(request):
            return
        if queryset.count() != 1:
            self.message_user(request, "Выберите ровно одну версию.", messages.ERROR)
            return
        try:
            with transaction.atomic():
                form = publish_form(queryset.get().pk)
                self.log_change(request, form, "Published as current form")
        except ValidationError as exc:
            self.message_user(request, "; ".join(exc.messages), messages.ERROR)
        else:
            self.message_user(request, "Версия опубликована. Предыдущие версии сохранены.")

    @admin.action(description="Создать редактируемую копию (одна версия)")
    def clone_selected(self, request, queryset):
        if not self.has_module_permission(request):
            return
        if queryset.count() != 1:
            self.message_user(request, "Выберите ровно одну версию.", messages.ERROR)
            return
        with transaction.atomic():
            draft = clone_form(queryset.get().pk)
            self.log_addition(request, draft, "Cloned form revision")
        self.message_user(request, f"Создан черновик v{draft.version}. Откройте его и измените definition.")
