from copy import deepcopy

from django.db import models, transaction
from django.db.models import F
from django.utils import timezone

from .models import FormRevision, Service


def published_services():
    return Service.objects.filter(
        is_active=True,
        category__is_active=True,
        current_form__published_at__isnull=False,
        current_form__service_id=F("id"),
    ).select_related("category", "current_form")


@transaction.atomic
def publish_form(form_id):
    service_id = FormRevision.objects.values_list("service_id", flat=True).get(pk=form_id)
    service = Service.objects.select_for_update().get(pk=service_id)
    form = FormRevision.objects.select_for_update().get(pk=form_id)
    if form.published_at is None:
        form.published_at = timezone.now()
        form.save()
    service.current_form = form
    service.save(update_fields=["current_form", "updated_at"])
    return form


@transaction.atomic
def clone_form(form_id):
    source = FormRevision.objects.get(pk=form_id)
    Service.objects.select_for_update().get(pk=source.service_id)
    latest = source.service.forms.aggregate(value=models.Max("version"))["value"] or 0
    return FormRevision.objects.create(
        service=source.service, version=latest + 1, definition=deepcopy(source.definition)
    )
