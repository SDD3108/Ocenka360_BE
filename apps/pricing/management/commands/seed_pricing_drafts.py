from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import Service
from apps.pricing.models import TariffRevision


class Command(BaseCommand):
    help = "Create manual-price drafts for services without tariffs; does not invent or publish prices."

    @transaction.atomic
    def handle(self, *args, **options):
        created = 0
        for service in Service.objects.select_for_update().order_by("pk"):
            if not service.tariffs.exists():
                TariffRevision.objects.create(service=service, version=1, mode="manual")
                created += 1
        self.stdout.write(f"Created {created} manual drafts. Existing tariffs unchanged; nothing published.")
