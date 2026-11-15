import uuid
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from apps.communications.tests.test_communications import CommunicationFixture
from apps.notifications.models import Notification
from apps.operations import services
from apps.operations.models import Reminder
from apps.orders import services as orders
from apps.orders.models import Order
from apps.orders.tests.test_orders import ROOT
from apps.payments import services as payments
from apps.payments.tests.test_payments import PaymentFixture
from apps.reports import services as reports
from apps.reports.tests.test_reports import ReportFixture
from apps.scheduling.models import Inspection


class TaskTests(CommunicationFixture):
    def task(self, order=None):
        order = order or self.reviewing()
        return services.create_task(
            self.reviewer,
            {
                "order_id": order.pk,
                "client_request_id": uuid.uuid4(),
                "title": "Проверить сведения",
                "note": "PRIVATE INTERNAL",
                "due_at": timezone.now() - timedelta(hours=1),
            },
        )[0]

    def test_task_api_role_version_history_and_reopen(self):
        task = self.task()
        self.api.force_authenticate(self.owner)
        for url in [
            "review/tasks/",
            f"review/tasks/{task.pk}/",
            f"review/tasks/{task.pk}/history/",
            "review/work-queue/",
            "review/dashboard/",
        ]:
            self.assertEqual(self.api.get(ROOT + url).status_code, 403)
        self.api.force_authenticate(self.reviewer)
        response = self.api.patch(ROOT + f"review/tasks/{task.pk}/", {"expected_version": 1, "state": "done"})
        self.assertEqual(response.status_code, 200, response.json())
        self.assertIsNotNone(response.json()["completed_at"])
        self.assertEqual(
            self.api.patch(
                ROOT + f"review/tasks/{task.pk}/", {"expected_version": 1, "state": "open"}
            ).status_code,
            409,
        )
        response = self.api.patch(ROOT + f"review/tasks/{task.pk}/", {"expected_version": 2, "state": "open"})
        self.assertIsNone(response.json()["completed_at"])
        self.assertEqual(self.api.get(ROOT + f"review/tasks/{task.pk}/history/").json()["count"], 3)

    def test_create_idempotency_and_unpaid_order_not_reviewable(self):
        order = self.reviewing()
        data = {
            "order_id": order.pk,
            "client_request_id": uuid.uuid4(),
            "title": "x",
            "note": "",
            "due_at": timezone.now(),
        }
        first, _ = services.create_task(self.reviewer, data)
        second, created = services.create_task(self.reviewer, data)
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        with self.assertRaises(orders.Conflict):
            services.create_task(self.reviewer, {**data, "title": "different"})

    def test_overdue_notification_reviewer_only_once_per_day_and_no_private_body(self):
        task = self.task()
        before = Notification.objects.filter(recipient=self.owner).count()
        version = task.order.version
        self.assertEqual(services.notify_overdue(), 1)
        self.assertEqual(services.notify_overdue(), 0)
        self.assertEqual(Reminder.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.owner).count(), before)
        self.assertEqual(
            Notification.objects.filter(kind="review_task_due", recipient=self.reviewer).count(), 1
        )
        self.api.force_authenticate(self.owner)
        history = self.api.get(ROOT + f"orders/{task.order_id}/history/")
        self.assertNotIn("review_task_due", history.content.decode())
        self.assertNotIn("PRIVATE INTERNAL", history.content.decode())
        task.order.refresh_from_db()
        self.assertEqual(task.order.version, version + 1)

    def test_reminder_limit_does_not_starve_later_tasks(self):
        task = self.task()
        self.task(task.order)
        self.assertEqual(services.notify_overdue(1), 1)
        self.assertEqual(services.notify_overdue(1), 1)
        self.assertEqual(services.notify_overdue(1), 0)

    def test_done_task_not_reminded_and_filter(self):
        task = self.task()
        services.update_task(self.reviewer, task.pk, {"expected_version": 1, "state": "done"})
        self.assertEqual(services.notify_overdue(), 0)
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(ROOT + "review/tasks/?overdue=true").json()["count"], 0)

    def test_unfiltered_tasks_include_overdue_and_future_tasks(self):
        overdue = self.task()
        future = self.task(overdue.order)
        services.update_task(
            self.reviewer, future.pk, {"expected_version": 1, "due_at": timezone.now() + timedelta(days=1)}
        )
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(ROOT + "review/tasks/").json()["count"], 2)
        self.assertEqual(
            [t["id"] for t in self.api.get(ROOT + "review/tasks/?overdue=true").json()["results"]],
            [str(overdue.pk)],
        )
        self.assertEqual(
            [t["id"] for t in self.api.get(ROOT + "review/tasks/?overdue=false").json()["results"]],
            [str(future.pk)],
        )

    def test_tasks_hidden_when_simulation_disabled(self):
        self.task()
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            self.assertEqual(services.task_query().count(), 0)


class QueueTests(ReportFixture):
    def test_report_deadline_queue_publish_original_and_finish(self):
        report, order = self.draft_report()
        now = timezone.now()
        Inspection.objects.filter(order=order).update(
            started_at=now - timedelta(days=2),
            completed_at=now - timedelta(days=1),
            report_due_at=now - timedelta(hours=2),
        )
        self.api.force_authenticate(self.reviewer)
        response = self.api.get(ROOT + "review/work-queue/?overdue=true")
        self.assertEqual(response.json()["count"], 1, response.json())
        self.assertEqual(response.json()["results"][0]["next_action"], "prepare_report")
        self.assertEqual(services.notify_overdue(), 1)
        self.assertFalse(Notification.objects.filter(kind="report_overdue", recipient=self.owner).exists())
        order.refresh_from_db()
        report, order = reports.publish(self.reviewer, order.pk, report.pk, order.version, True)
        self.assertEqual(self.api.get(ROOT + "review/work-queue/?overdue=true").json()["count"], 0)
        response = self.api.get(ROOT + "review/work-queue/").json()
        self.assertEqual(response["results"][0]["next_action"], "prepare_original")
        _, order = reports.original_action(self.reviewer, order.pk, report.pk, order.version, "ready", True)
        self.assertEqual(
            self.api.get(ROOT + "review/work-queue/").json()["results"][0]["next_action"], "handover_original"
        )
        reports.original_action(self.reviewer, order.pk, report.pk, order.version, "handover", True, "pickup")
        self.assertEqual(self.api.get(ROOT + "review/work-queue/").json()["count"], 0)

    def test_completed_statistics_and_withdrawal(self):
        report, order = self.published()
        today = timezone.localdate()
        result = services.summary(today, today, True)
        self.assertEqual(result["completed_orders"], 1)
        self.assertEqual(result["report_publications"], 1)
        reports.withdraw(self.reviewer, order.pk, report.pk, order.version, True, "Correction")
        result = services.summary(today, today, True)
        self.assertEqual(result["completed_orders"], 0)
        self.assertEqual(result["report_publications"], 1)


class AnalyticsTests(PaymentFixture):
    def test_cashflow_refunds_test_exclusion_and_source_cohort(self):
        order, _ = self.paid_advance()
        Order.objects.filter(pk=order.pk).update(acquisition_source="referral")
        today = timezone.localdate()
        self.assertEqual(services.summary(today, today)["new_paid_orders"], 0)
        result = services.summary(today, today, True)
        self.assertEqual(result["new_paid_orders"], 1)
        self.assertEqual(result["gross_receipts"], 2000)
        self.assertEqual(result["sources"], [{"acquisition_source": "referral", "count": 1}])
        order = orders.cancel_order(self.owner, order.pk, order.version)
        from apps.payments.models import Refund

        payments.simulate_refund(Refund.objects.get().pk)
        result = services.summary(today, today, True)
        self.assertEqual(result["net_cash_flow"], 0)
        self.assertEqual(result["refunds"], 2000)

    def test_dashboard_filters_and_defaults(self):
        self.paid_advance()
        self.api.force_authenticate(self.reviewer)
        data = self.api.get(ROOT + "review/dashboard/").json()
        self.assertFalse(data["include_test"])
        self.assertEqual(data["gross_receipts"], "0.00")
        self.assertEqual(data["timezone"], "Asia/Almaty")
        self.assertEqual(
            self.api.get(ROOT + "review/dashboard/?start_date=2020-01-01&end_date=2026-01-01").status_code,
            400,
        )
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            self.assertEqual(self.api.get(ROOT + "review/dashboard/?include_test=true").status_code, 400)

    def test_source_defaults_and_is_fixed_after_submission(self):
        self.api.force_authenticate(self.owner)
        data = self.draft(acquisition_source="2gis")
        self.assertEqual(data["acquisition_source"], "2gis")
        response = self.confirm(data)
        self.assertEqual(response.status_code, 200)
        order = orders.simulate_payment(data["id"])
        order = orders.review_action(self.reviewer, order.pk, order.version, "start_review")
        order = orders.review_action(self.reviewer, order.pk, order.version, "request_information", "Explain")
        with self.assertRaises(orders.Conflict):
            orders.update_order(
                self.owner, order.pk, {"expected_version": order.version, "acquisition_source": "call"}
            )
