import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.legal.models import LegalDocument
from apps.legal.services import accept_documents
from apps.materials.tests.fixtures import seed_test_materials
from apps.orders import services as orders
from apps.orders.models import Asset
from apps.orders.tests import test_concurrency as order_concurrency
from apps.pricing import services
from apps.pricing.models import PromoRedemption


@skipUnless(connection.vendor == "postgresql", "Pricing concurrency requires PostgreSQL")
class PricingConcurrencyTests(TransactionTestCase):
    def setUp(self):
        order_concurrency.OrderConcurrencyTests.setUp(self)
        self.reviewer = self.user("parallel-reviewer@example.com", "appraiser")
        self.admin = self.user("parallel-admin@example.com", "admin")
        self.other = self.user("parallel-other@example.com", "client")
        self.promo = services.create_promo(
            self.admin,
            {
                "code": "LASTONE",
                "kind": "percent",
                "value": Decimal("10.00"),
                "max_uses": 1,
                "starts_at": timezone.now() - timedelta(days=1),
                "ends_at": timezone.now() + timedelta(days=2),
            },
        )

    def user(self, email, role):
        user = User.objects.create_user(email, role=role)
        user.email_verified_at = timezone.now()
        user.save()
        return accept_documents(
            user,
            list(LegalDocument.objects.filter(language="ru", is_active=True).values_list("id", flat=True)),
            True,
        )

    def offer(self, owner):
        asset = Asset.objects.create(owner=owner, title="TEST", kind="apartment", city="Астана")
        order, _ = orders.create_order(
            owner, {**self.data, "client_request_id": uuid.uuid4(), "asset_id": asset.pk}
        )
        seed_test_materials(order)
        order = orders.submit_order(owner, order.pk, order.version, True)
        order = orders.simulate_payment(order.pk)
        order = orders.review_action(self.reviewer, order.pk, order.version, "start_review")
        order = orders.review_action(self.reviewer, order.pk, order.version, "accept")
        order = services.select_promo(owner, order.pk, order.version, self.promo.code)
        quote, order, _ = services.issue_quote(
            self.reviewer,
            order.pk,
            {
                "expected_version": order.version,
                "client_request_id": uuid.uuid4(),
                "lines": [{"kind": "appraisal", "title": "Test", "amount": Decimal("20000.00")}],
                "terms": "Test only",
                "expires_at": timezone.now() + timedelta(hours=24),
            },
        )
        return owner, order, quote

    def parallel(self, tasks):
        barrier = Barrier(2)

        def run(task):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return task()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, tasks))

    def test_last_promo_use_cannot_be_spent_by_two_users(self):
        records = [self.offer(self.owner), self.offer(self.other)]

        def accept(record):
            owner, order, quote = record
            try:
                services.decide_quote(owner, order.pk, quote.pk, order.version, "accept", confirmed=True)
                return "accepted"
            except orders.Conflict:
                return "conflict"

        result = self.parallel([lambda r=r: accept(r) for r in records])
        self.assertCountEqual(result, ["accepted", "conflict"])
        self.assertEqual(PromoRedemption.objects.filter(released_at__isnull=True).count(), 1)

    def test_double_accept_is_idempotent_under_lock(self):
        owner, order, quote = self.offer(self.owner)
        initial_events = order.events.count()

        def accept():
            return services.decide_quote(owner, order.pk, quote.pk, order.version, "accept", confirmed=True)[
                0
            ].state

        self.assertEqual(self.parallel([accept, accept]), ["accepted", "accepted"])
        self.assertEqual(PromoRedemption.objects.count(), 1)
        self.assertEqual(order.events.count(), initial_events + 1)

    def test_cancel_and_accept_cannot_both_change_same_version(self):
        owner, order, quote = self.offer(self.owner)

        def act(action):
            try:
                if action == "accept":
                    services.decide_quote(owner, order.pk, quote.pk, order.version, "accept", confirmed=True)
                else:
                    orders.cancel_order(owner, order.pk, order.version)
                return "ok"
            except orders.Conflict:
                return "conflict"

        self.assertCountEqual(
            self.parallel([lambda: act("accept"), lambda: act("cancel")]), ["ok", "conflict"]
        )
        order.refresh_from_db()
        self.assertIn(order.status, {"cancelled", "price_agreed"})
        self.assertEqual(
            PromoRedemption.objects.filter(released_at__isnull=True).count(),
            int(order.status == "price_agreed"),
        )
