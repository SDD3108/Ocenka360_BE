from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "LOCAL ONLY: complete synthetic workflow through PDF publication and original handover, then roll back."

    def handle(self, *args, **options):
        call_command(
            "demo_orders",
            payments=True,
            communications=True,
            inspection=True,
            reports=True,
            stdout=self.stdout,
        )
