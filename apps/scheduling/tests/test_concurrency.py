import uuid
from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.orders import services as orders
from apps.pricing import services as pricing
from apps.pricing.models import PromoCode
from apps.pricing.tests import test_concurrency as pricing_concurrency
from apps.scheduling import services
from apps.scheduling.models import Appointment, CalendarBlock, CalendarLock


@skipUnless(connection.vendor == "postgresql", "Calendar concurrency requires PostgreSQL")
class CalendarConcurrencyTests(TransactionTestCase):
    user = pricing_concurrency.PricingConcurrencyTests.user
    offer = pricing_concurrency.PricingConcurrencyTests.offer
    parallel = pricing_concurrency.PricingConcurrencyTests.parallel

    def setUp(self):
        pricing_concurrency.PricingConcurrencyTests.setUp(self)
        CalendarLock.objects.get_or_create(pk=1)
        # Fixture allows both independent orders to agree their price before racing for one slot.
        PromoCode.objects.filter(pk=self.promo.pk).update(max_uses=10)
        self.second_reviewer = self.user("calendar-other-appraiser@example.com", "appraiser")
        self.start = timezone.now() + timedelta(days=1)
        self.end = self.start + timedelta(hours=1)

    def requested(self, owner):
        owner, order, quote = self.offer(owner)
        _, order = pricing.decide_quote(owner, order.pk, quote.pk, order.version, "accept", confirmed=True)
        appointment, order, _ = services.propose(
            owner,
            order.pk,
            {
                "expected_version": order.version,
                "client_request_id": uuid.uuid4(),
                "starts_at": self.start,
                "ends_at": self.end,
                "mode": "onsite",
                "note": "TEST",
            },
        )
        return owner, order, appointment

    def confirm(self, actor, record):
        owner, order, appointment = record
        try:
            services.confirm_request(
                actor,
                order.pk,
                appointment.pk,
                {
                    "expected_version": order.version,
                    "starts_at": self.start,
                    "ends_at": self.end,
                    "report_hours": 24,
                    "buffer_before_minutes": 0,
                    "buffer_after_minutes": 0,
                },
            )
            return "ok"
        except orders.Conflict:
            return "conflict"

    def test_different_reviewers_cannot_double_book_shared_calendar(self):
        first, second = self.requested(self.owner), self.requested(self.other)
        result = self.parallel(
            [lambda: self.confirm(self.reviewer, first), lambda: self.confirm(self.second_reviewer, second)]
        )
        self.assertCountEqual(result, ["ok", "conflict"])
        self.assertEqual(Appointment.objects.filter(state="confirmed").count(), 1)

    def test_block_and_confirmation_use_same_calendar_lock(self):
        record = self.requested(self.owner)

        def block():
            try:
                services.create_block(
                    self.second_reviewer,
                    {
                        "client_request_id": uuid.uuid4(),
                        "starts_at": self.start,
                        "ends_at": self.end,
                        "reason": "TEST",
                    },
                )
                return "ok"
            except orders.Conflict:
                return "conflict"

        self.assertCountEqual(
            self.parallel([lambda: self.confirm(self.reviewer, record), block]), ["ok", "conflict"]
        )
        self.assertEqual(
            Appointment.objects.filter(state="confirmed").count()
            + CalendarBlock.objects.filter(cancelled_at__isnull=True).count(),
            1,
        )

    def test_order_cancel_and_confirmation_cannot_both_apply_same_version(self):
        record = self.requested(self.owner)
        owner, order, appointment = record

        def cancel():
            try:
                orders.cancel_order(owner, order.pk, order.version, "TEST")
                return "ok"
            except orders.Conflict:
                return "conflict"

        self.assertCountEqual(
            self.parallel([lambda: self.confirm(self.reviewer, record), cancel]), ["ok", "conflict"]
        )
        order.refresh_from_db()
        appointment.refresh_from_db()
        self.assertEqual(
            appointment.state, "confirmed" if order.status == "inspection_scheduled" else "cancelled"
        )
