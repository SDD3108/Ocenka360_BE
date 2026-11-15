import uuid
from decimal import Decimal
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.comparables import services
from apps.comparables.models import Estimate
from apps.pricing.tests import test_concurrency as pricing_base


@skipUnless(connection.vendor == "postgresql", "Comparable concurrency requires PostgreSQL")
class ComparableConcurrencyTests(TransactionTestCase):
    user = pricing_base.PricingConcurrencyTests.user
    parallel = pricing_base.PricingConcurrencyTests.parallel
    offer = pricing_base.PricingConcurrencyTests.offer

    def setUp(self):
        pricing_base.PricingConcurrencyTests.setUp(self)

    def test_same_request_creates_one_snapshot(self):
        _, order, _ = self.offer(self.owner)
        data = {
            "expected_version": order.version,
            "client_request_id": uuid.uuid4(),
            "as_of": timezone.localdate(),
            "comparability_confirmed": True,
            "note": "TEST",
            "comparables": [
                {
                    "source_url": f"https://example.com/{i}",
                    "source_label": "TEST",
                    "observed_on": timezone.localdate(),
                    "city": order.answers["city"],
                    "area_sqm": Decimal("50"),
                    "asking_price_kzt": Decimal("25000000"),
                    "note": "",
                }
                for i in range(3)
            ],
        }
        results = self.parallel(
            [lambda: services.create(self.reviewer, order.pk, data)[0].pk for _ in range(2)]
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(Estimate.objects.count(), 1)
