from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "LOCAL ONLY: Parts 4–9 workflow, rolled back; no real money, push or email."

    def handle(self, *args, **options):
        call_command("demo_orders", payments=True, communications=True, stdout=self.stdout)
