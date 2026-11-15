from django.contrib import admin

from apps.orders.admin import ReadOnlyAdmin

from .models import Estimate, EstimateEvent

for model in (Estimate, EstimateEvent):
    admin.site.register(model, ReadOnlyAdmin)
