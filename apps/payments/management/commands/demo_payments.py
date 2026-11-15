from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "LOCAL ONLY: two rollback-only payment demos, full refund and funded inspection."

    def handle(self, *args, **options):
        call_command("demo_orders", payments=True, stdout=self.stdout)
        call_command("demo_orders", payments=True, inspection=True, stdout=self.stdout)
