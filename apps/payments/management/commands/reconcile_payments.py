from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import APIException

from apps.payments.models import PaymentIntent
from apps.payments.services import refresh_intent


class Command(BaseCommand):
    help = "Read Halyk bank statuses and reconcile ledger. Does not initiate payments or refunds."

    def add_arguments(self, parser):
        parser.add_argument("--intent", help="Specific Halyk intent UUID; includes old payments")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 100:
            raise CommandError("limit must be 1..100")
        query = PaymentIntent.objects.filter(provider="halyk")
        if options["intent"]:
            query = query.filter(pk=options["intent"])
        else:
            query = query.filter(
                Q(created_at__gte=timezone.now() - timedelta(days=7))
                | Q(sync_requested_at__isnull=False)
                | Q(needs_review=True)
                | Q(receipt__refunds__state__in=["processing", "submitted", "unknown"])
            ).distinct()
        ids = list(
            query.order_by(F("last_checked_at").asc(nulls_first=True), "created_at", "id").values_list(
                "id", flat=True
            )[: options["limit"]]
        )
        ok = failed = 0
        for intent_id in ids:
            try:
                refresh_intent(intent_id)
                ok += 1
            except APIException:
                failed += 1
                self.stderr.write(f"Reconciliation pending for {intent_id}; no status assumed.")
        self.stdout.write(f"Checked: {ok}; pending/errors: {failed}. No transfers initiated.")
