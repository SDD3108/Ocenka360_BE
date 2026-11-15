from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import Reminder, Task, TaskEvent

for model in (Task, TaskEvent, Reminder):
    admin.site.register(model, ReadOnlyAdmin)
