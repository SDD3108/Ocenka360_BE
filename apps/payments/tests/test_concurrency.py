import uuid
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings

from apps.materials.tests.fixtures import seed_test_materials
from apps.orders import services as orders
from apps.payments import services
from apps.payments.models import InvoiceSequence, Receipt, Refund
from apps.payments.tests.test_payments import HALYK
from apps.pricing.tests import test_concurrency as pricing_concurrency


@skipUnless(connection.vendor == "postgresql", "Payment concurrency requires PostgreSQL")
class PaymentConcurrencyTests(TransactionTestCase):
    user = pricing_concurrency.PricingConcurrencyTests.user
    parallel = pricing_concurrency.PricingConcurrencyTests.parallel

    def setUp(self):
        pricing_concurrency.PricingConcurrencyTests.setUp(self)
        InvoiceSequence.objects.get_or_create(pk=1)
        self.second_reviewer = self.user("other-finance@example.com", "appraiser")

    def new_intent(self, provider="mock"):
        order, _ = orders.create_order(self.owner, {**self.data, "client_request_id": uuid.uuid4()})
        seed_test_materials(order)
        order = orders.submit_order(self.owner, order.pk, order.version, True)
        intent, order, _ = services.create_intent(
            self.owner,
            order.pk,
            {
                "expected_version": order.version,
                "client_request_id": uuid.uuid4(),
                "purpose": "advance",
                "provider": provider,
            },
        )
        return intent, order

    def test_duplicate_confirmations_create_one_receipt_and_event(self):
        intent, order = self.new_intent()
        results = self.parallel(
            [lambda: services.simulate_payment(intent.pk).pk, lambda: services.simulate_payment(intent.pk).pk]
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(order.events.filter(action="payment_recorded").count(), 1)

    def test_payment_and_cancel_race_never_loses_money_or_reopens_cancelled_order(self):
        intent, order = self.new_intent()

        def cancel():
            try:
                orders.cancel_order(self.owner, order.pk, order.version)
                return "cancelled"
            except orders.Conflict:
                return "stale"

        self.parallel([lambda: services.simulate_payment(intent.pk), cancel])
        order.refresh_from_db()
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertIn(order.status, {"submitted", "cancelled"})
        self.assertEqual(Refund.objects.count(), int(order.status == "cancelled"))
        self.assertEqual(services.balance(order)["received"], 2000)

    @override_settings(**HALYK)
    def test_two_reviewers_cannot_dispatch_refund_twice(self):
        intent, order = self.new_intent(provider="halyk")
        with patch(
            "apps.payments.providers.Halyk.status",
            return_value={"state": "paid", "transaction_id": "TX-TEST", "amount": intent.amount},
        ):
            services.refresh_intent(intent.pk)
        order.refresh_from_db()
        order = orders.cancel_order(self.owner, order.pk, order.version)
        refund = Refund.objects.get()

        def execute(actor):
            try:
                services.execute_refund(actor, order.pk, refund.pk, order.version, True)
                return "ok"
            except orders.Conflict:
                return "conflict"

        with patch("apps.payments.providers.Halyk.refund_full", return_value={}) as transport:
            result = self.parallel([lambda: execute(self.reviewer), lambda: execute(self.second_reviewer)])
            self.assertCountEqual(result, ["ok", "conflict"])
            self.assertEqual(transport.call_count, 1)
        refund.refresh_from_db()
        self.assertEqual(refund.state, "submitted")
