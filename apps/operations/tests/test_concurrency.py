from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from apps.notifications.models import Notification
from apps.operations import services
from apps.operations.models import Reminder
from apps.reports.tests import test_concurrency as reports_base


@skipUnless(connection.vendor == "postgresql", "Reminder concurrency requires PostgreSQL")
class ReminderConcurrencyTests(TransactionTestCase):
    user = reports_base.ReportConcurrencyTests.user
    parallel = reports_base.ReportConcurrencyTests.parallel
    offer = reports_base.ReportConcurrencyTests.offer

    def setUp(self):
        reports_base.ReportConcurrencyTests.setUp(self)

    def test_two_reminder_workers_create_one_daily_notification(self):
        self.parallel([lambda: services.notify_overdue(), lambda: services.notify_overdue()])
        self.assertEqual(Reminder.objects.count(), 1)
        self.assertEqual(
            Notification.objects.filter(kind="report_overdue", recipient=self.reviewer).count(), 1
        )
