from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import APIException

from apps.payments.services import simulate_payment, simulate_refund


class Command(BaseCommand):
    help = "LOCAL ONLY: record a mock payment/refund. Never sends real money."

    def add_arguments(self, parser):
        parser.add_argument("id", help="UUID of a mock payment intent, or a refund with --refund")
        parser.add_argument("--refund", action="store_true")
        parser.add_argument("--confirm-test", action="store_true")

    def handle(self, *args, **options):
        if not options["confirm_test"]:
            raise CommandError("Add --confirm-test. No real bank or money is involved.")
        try:
            entry = (simulate_refund if options["refund"] else simulate_payment)(options["id"])
        except APIException as exc:
            raise CommandError(str(exc.detail)) from None
        self.stdout.write(self.style.SUCCESS(f"TEST ONLY entry {entry.pk}; no real money moved."))
