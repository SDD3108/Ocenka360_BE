from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import Job, Permission, PermissionEvent

for model in (Job, Permission, PermissionEvent):
    admin.site.register(model, ReadOnlyAdmin)
