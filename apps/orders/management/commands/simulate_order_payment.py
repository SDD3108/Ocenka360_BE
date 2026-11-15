import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.http import Http404
from rest_framework.exceptions import APIException

from apps.orders.services import simulate_payment


class Command(BaseCommand):
    help = "LOCAL TEST ONLY: unlock a test order for review. Never charges or confirms actual money."

    def add_arguments(self, parser):
        parser.add_argument("order_id", type=uuid.UUID)
        parser.add_argument("--confirm-test", action="store_true")

    def handle(self, *args, **options):
        if not settings.ALLOW_ORDER_SIMULATION:
            raise CommandError("Order simulation is disabled in this environment.")
        if not options["confirm_test"]:
            raise CommandError("Pass --confirm-test to mark this order as a simulation with no real payment.")
        try:
            order = simulate_payment(options["order_id"])
        except (APIException, Http404) as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(
            self.style.WARNING(
                f"TEST ONLY {order.number}: submitted. No money charged/received. is_test=True."
            )
        )
