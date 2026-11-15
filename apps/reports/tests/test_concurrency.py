import uuid
from datetime import timedelta
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.materials.tests.fixtures import pdf_upload
from apps.notifications.models import Notification
from apps.orders import services as orders
from apps.pricing import services as pricing
from apps.pricing.tests import test_concurrency as base
from apps.reports import services
from apps.reports.models import Original, Report
from apps.scheduling.models import Appointment, Inspection


@skipUnless(connection.vendor == "postgresql", "Report concurrency requires PostgreSQL")
class ReportConcurrencyTests(TransactionTestCase):
    user = base.PricingConcurrencyTests.user
    parallel = base.PricingConcurrencyTests.parallel
    offer = base.PricingConcurrencyTests.offer

    def setUp(self):
        base.PricingConcurrencyTests.setUp(self)
        self.second = self.user("report-second@example.com", "appraiser")
        owner, order, quote = self.offer(self.owner)
        _, order = pricing.decide_quote(owner, order.pk, quote.pk, order.version, "accept", confirmed=True)
        now = timezone.now()
        appt = Appointment.objects.create(
            order=order,
            created_by=self.reviewer,
            client_request_id=uuid.uuid4(),
            request_fingerprint="fixture",
            mode="onsite",
            state="completed",
            starts_at=now - timedelta(hours=2),
            ends_at=now - timedelta(hours=1),
            busy_from=now - timedelta(hours=2),
            busy_until=now - timedelta(hours=1),
            report_hours=1,
        )
        Inspection.objects.create(
            order=order,
            appointment=appt,
            started_by=self.reviewer,
            completed_by=self.reviewer,
            started_at=appt.starts_at,
            completed_at=appt.ends_at,
            report_due_at=now - timedelta(minutes=30),
        )
        order.status = "awaiting_report"
        order.save()
        self.order = order
        self.report, self.order, _ = services.upload(
            self.reviewer,
            order.pk,
            {
                "expected_version": order.version,
                "client_upload_id": uuid.uuid4(),
                "report_number": "TEST",
                "title": "TEST",
                "note": "",
                "issue_date": timezone.localdate(),
                "file": pdf_upload(),
            },
        )

    def test_duplicate_publication_is_one_report_original_and_notification(self):
        results = self.parallel(
            [
                lambda actor=a: (
                    services.publish(actor, self.order.pk, self.report.pk, self.order.version, True)[0].pk
                )
                for a in [self.reviewer, self.second]
            ]
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(Original.objects.count(), 1)
        self.assertEqual(
            Notification.objects.filter(kind="report_published", recipient=self.owner).count(), 1
        )

    def test_two_drafts_cannot_publish_over_each_other_from_stale_version(self):
        second, order, _ = services.upload(
            self.second,
            self.order.pk,
            {
                "expected_version": self.order.version,
                "client_upload_id": uuid.uuid4(),
                "report_number": "TEST2",
                "title": "TEST2",
                "note": "",
                "issue_date": timezone.localdate(),
                "file": pdf_upload(),
            },
        )

        def publish(actor, report):
            try:
                services.publish(actor, order.pk, report.pk, order.version, True, "Fix")
                return "published"
            except orders.Conflict:
                return "stale"

        results = self.parallel(
            [lambda: publish(self.reviewer, self.report), lambda: publish(self.second, second)]
        )
        self.assertCountEqual(results, ["published", "stale"])
        self.assertEqual(Report.objects.filter(state="published").count(), 1)
