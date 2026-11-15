from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings

from apps.assistance import services, worker
from apps.assistance.models import Job
from apps.assistance.tests.test_assistance import CONFIG, RESULT, USAGE, clean, payload
from apps.pricing.tests import test_concurrency as pricing_base


@skipUnless(connection.vendor == "postgresql", "AI concurrency requires PostgreSQL")
@override_settings(**CONFIG)
class AIConcurrencyTests(TransactionTestCase):
    user = pricing_base.PricingConcurrencyTests.user
    parallel = pricing_base.PricingConcurrencyTests.parallel

    def setUp(self):
        pricing_base.PricingConcurrencyTests.setUp(self)

    def test_same_request_creates_one_job(self):
        data = clean(payload())
        results = self.parallel([lambda: services.create_job(self.owner, data)[0].pk for _ in range(2)])
        self.assertEqual(results[0], results[1])
        self.assertEqual(Job.objects.count(), 1)

    def test_two_workers_claim_once_and_nullable_order_lock(self):
        job, _ = services.create_job(self.owner, clean(payload()))
        claims = self.parallel([worker.claim, worker.claim])
        self.assertEqual(sum(x is not None for x in claims), 1)
        with patch("apps.assistance.provider.generate", return_value=(RESULT, USAGE)) as call:
            worker.run_one(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(call.call_count, 1)
