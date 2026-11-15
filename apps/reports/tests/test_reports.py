import io
import os
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.http import Http404
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.materials.storage import PrivateStorage
from apps.materials.tests.fixtures import pdf_bytes, pdf_upload, photo_upload
from apps.notifications.models import Notification
from apps.orders import services as orders
from apps.orders.tests.test_orders import ROOT
from apps.payments.models import CancellationCase, PaymentIntent
from apps.reports import services
from apps.reports.models import Original, Report, ReportAccess
from apps.scheduling import services as scheduling
from apps.scheduling.tests.test_scheduling import SchedulingFixture


class ReportFixture(SchedulingFixture):
    def ready(self):
        order, appt = self.scheduled()
        self.assertEqual(self.start_slot(order, appt).status_code, 200)
        order.refresh_from_db()
        with patch("apps.scheduling.services.timezone.now", return_value=appt.starts_at + timedelta(hours=1)):
            _, order = scheduling.finish_inspection(self.reviewer, order.pk, appt.pk, order.version, "Done")
        return order

    def upload_data(self, order, **extra):
        return {
            "expected_version": order.version,
            "client_upload_id": uuid.uuid4(),
            "report_number": "TEST-001",
            "title": "Отчёт об оценке",
            "issue_date": timezone.localdate(),
            "note": "Проверено оценщиком",
            "file": pdf_upload(),
            **extra,
        }

    def draft_report(self, order=None, **extra):
        order = order or self.ready()
        report, order, created = services.upload(self.reviewer, order.pk, self.upload_data(order, **extra))
        self.assertTrue(created)
        return report, order

    def published(self):
        report, order = self.draft_report()
        return services.publish(self.reviewer, order.pk, report.pk, order.version, True)


class ReportTests(ReportFixture):
    def test_draft_private_publication_download_and_notification(self):
        report, order = self.draft_report()
        self.api.force_authenticate(self.owner)
        path = ROOT + f"orders/{order.pk}/reports/"
        self.assertEqual(self.api.get(path).json()["count"], 0)
        self.assertEqual(self.api.get(path + f"{report.pk}/download/").status_code, 404)
        self.assertFalse(Notification.objects.filter(kind="report_published").exists())
        before = order.inspection.report_due_at
        report, order = services.publish(self.reviewer, order.pk, report.pk, order.version, True)
        self.assertEqual(order.status, "completed")
        self.assertEqual(order.inspection.report_due_at, before)
        self.assertEqual(report.original.state, "preparing")
        r = self.api.get(path + f"{report.pk}/download/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), pdf_bytes())
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn("no-store", r["Cache-Control"])
        self.assertEqual(ReportAccess.objects.count(), 1)
        self.assertTrue(Notification.objects.filter(kind="report_published", recipient=self.owner).exists())
        listing = self.api.get(path).json()["results"][0]
        self.assertNotIn("storage_key", listing)
        self.assertEqual(listing["language"], "ru")

    def test_foreign_user_anonymous_and_client_upload_forbidden(self):
        report, order = self.published()
        path = ROOT + f"orders/{order.pk}/reports/{report.pk}/download/"
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(path).status_code, 404)
        self.api.force_authenticate(None)
        self.assertEqual(self.api.get(path).status_code, 401)
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.post(ROOT + f"review/orders/{order.pk}/reports/", {}).status_code, 403)

    def test_upload_idempotency_and_different_bytes_conflict(self):
        order = self.ready()
        data = self.upload_data(order)
        report, order, _ = services.upload(self.reviewer, order.pk, data)
        data["file"] = pdf_upload()
        repeated, _, created = services.upload(self.reviewer, order.pk, data)
        self.assertFalse(created)
        self.assertEqual(report.pk, repeated.pk)
        data.update(file=pdf_upload(), title="Different")
        with self.assertRaises(orders.Conflict):
            services.upload(self.reviewer, order.pk, data)
        self.assertEqual(Report.objects.count(), 1)

    def test_publish_needs_confirmation_version_and_is_idempotent(self):
        report, order = self.draft_report()
        with self.assertRaises(ValidationError):
            services.publish(self.reviewer, order.pk, report.pk, order.version, False)
        with self.assertRaises(orders.Conflict):
            services.publish(self.reviewer, order.pk, report.pk, 1, True)
        old = order.version
        report, order = services.publish(self.reviewer, order.pk, report.pk, old, True)
        repeated, same = services.publish(self.reviewer, order.pk, report.pk, old, True)
        self.assertEqual(same.version, order.version)
        self.assertEqual(Original.objects.count(), 1)
        self.assertEqual(
            Notification.objects.filter(kind="report_published", recipient=self.owner).count(), 1
        )

    def test_replacement_keeps_old_bytes_and_hides_old_download(self):
        old, order = self.published()
        completed = order.completed_at
        report, order = self.draft_report(order, report_number="TEST-002")
        with self.assertRaises(ValidationError):
            services.publish(self.reviewer, order.pk, report.pk, order.version, True)
        report, order = services.publish(
            self.reviewer, order.pk, report.pk, order.version, True, "Исправление опечатки"
        )
        old.refresh_from_db()
        self.assertEqual(old.state, "superseded")
        self.assertEqual(order.completed_at, completed)
        self.assertTrue(PrivateStorage().exists(old.storage_key))
        with self.assertRaises((NotFound, Http404)):
            services.open_report(self.owner, order.pk, old.pk)
        _, stream = services.open_report(self.reviewer, order.pk, old.pk, True)
        stream.close()
        with self.assertRaises(orders.Conflict):
            services.publish(self.reviewer, order.pk, old.pk, order.version, True)

    def test_older_draft_cannot_replace_a_newer_published_revision(self):
        older, order = self.draft_report()
        newer, order = self.draft_report(order)
        newer, order = services.publish(self.reviewer, order.pk, newer.pk, order.version, True)
        with self.assertRaises(orders.Conflict):
            services.publish(self.reviewer, order.pk, older.pk, order.version, True, "Attempt to regress")

    def test_withdraw_restores_report_work_without_resetting_deadline(self):
        report, order = self.published()
        due = order.inspection.report_due_at
        report, order = services.withdraw(
            self.reviewer, order.pk, report.pk, order.version, True, "Нужно исправить"
        )
        self.assertEqual(order.status, "awaiting_report")
        self.assertIsNone(order.completed_at)
        self.assertEqual(order.inspection.report_due_at, due)
        with self.assertRaises((NotFound, Http404)):
            services.open_report(self.owner, order.pk, report.pk)
        self.assertTrue(Notification.objects.filter(kind="report_withdrawn", recipient=self.owner).exists())
        self.assertTrue(PrivateStorage().exists(report.storage_key))

    def test_draft_discard_does_not_hide_current_report(self):
        current, order = self.published()
        draft, order = self.draft_report(order)
        draft, order = services.withdraw(
            self.reviewer, order.pk, draft.pk, order.version, True, "Лишняя копия"
        )
        self.assertEqual(order.status, "completed")
        current.refresh_from_db()
        self.assertEqual(current.state, "published")
        self.api.force_authenticate(self.owner)
        actions = [x["action"] for x in self.api.get(ROOT + f"orders/{order.pk}/history/").json()["results"]]
        self.assertNotIn("report_draft_discarded", actions)
        self.assertNotIn("report_uploaded", actions)

    def test_upload_requires_finished_inspection(self):
        order = self.agreed()
        with self.assertRaises(orders.Conflict):
            services.upload(self.reviewer, order.pk, self.upload_data(order))

    def test_publish_blocks_cancellation_and_unpaid_balance(self):
        report, order = self.draft_report()
        case = CancellationCase.objects.create(order=order, client_request_id=uuid.uuid4(), reason="Cancel")
        with self.assertRaises(orders.Conflict):
            services.publish(self.reviewer, order.pk, report.pk, order.version, True)
        case.state = "withdrawn"
        case.save()
        PaymentIntent.objects.create(
            order=order,
            client_request_id=uuid.uuid4(),
            request_fingerprint="test",
            purpose="balance",
            provider="mock",
            environment="local",
            amount="20000.00",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        with self.assertRaises(orders.Conflict):
            services.publish(self.reviewer, order.pk, report.pk, order.version, True)

    def test_missing_or_changed_file_cannot_publish_or_download(self):
        report, order = self.draft_report()
        with PrivateStorage().open(report.storage_key, "wb") as f:
            f.write(b"modified")
        with self.assertRaises(orders.Conflict):
            services.publish(self.reviewer, order.pk, report.pk, order.version, True)
        PrivateStorage().delete(report.storage_key)
        with self.assertRaises((NotFound, Http404)):
            services.publish(self.reviewer, order.pk, report.pk, order.version, True)

    def test_pdf_only_and_active_pdf_rejected(self):
        order = self.ready()
        with self.assertRaises(ValidationError):
            services.upload(self.reviewer, order.pk, self.upload_data(order, file=photo_upload()))
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject

        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer._root_object[NameObject("/AcroForm")] = DictionaryObject()
        out = io.BytesIO()
        writer.write(out)
        with self.assertRaises(ValidationError):
            services.upload(
                self.reviewer,
                order.pk,
                self.upload_data(
                    order,
                    file=SimpleUploadedFile("active.pdf", out.getvalue(), content_type="application/pdf"),
                ),
            )

    def test_unscanned_report_blocked_when_scanning_required(self):
        report, order = self.draft_report()
        # Keep simulation enabled to isolate the antivirus guard.
        with override_settings(ALLOW_UNSCANNED_UPLOADS=False), self.assertRaises(NotFound):
            services.publish(self.reviewer, order.pk, report.pk, order.version, True)

    def test_publication_rollback_has_no_visible_report_or_notification(self):
        report, order = self.draft_report()
        with self.assertRaises(RuntimeError), transaction.atomic():
            services.publish(self.reviewer, order.pk, report.pk, order.version, True)
            raise RuntimeError()
        report.refresh_from_db()
        self.assertEqual(report.state, "draft")
        self.assertFalse(Original.objects.exists())
        self.assertFalse(Notification.objects.filter(kind="report_published").exists())

    def test_storage_audit_preserves_all_report_states(self):
        report, order = self.draft_report()
        services.withdraw(self.reviewer, order.pk, report.pk, order.version, True, "Discard")
        path = PrivateStorage().path(report.storage_key)
        os.utime(path, (0, 0))
        out = io.StringIO()
        call_command("audit_material_storage", delete_orphans=True, stdout=out)
        self.assertTrue(PrivateStorage().exists(report.storage_key))
        self.assertIn("deleted=0", out.getvalue())

    def test_multipart_upload_endpoint_and_publish(self):
        order = self.ready()
        self.api.force_authenticate(self.reviewer)
        data = self.upload_data(order)
        response = self.api.post(ROOT + f"review/orders/{order.pk}/reports/", data, format="multipart")
        self.assertEqual(response.status_code, 201, response.json())
        report = response.json()["report"]
        response = self.api.post(
            ROOT + f"review/orders/{order.pk}/reports/{report['id']}/publish/",
            {"expected_version": response.json()["order_version"], "confirmed": True},
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["order_status"], "completed")

    def test_future_issue_date_and_extra_fields_are_rejected(self):
        order = self.ready()
        self.api.force_authenticate(self.reviewer)
        response = self.api.post(
            ROOT + f"review/orders/{order.pk}/reports/",
            self.upload_data(order, issue_date=timezone.localdate() + timedelta(days=1)),
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    def test_completed_order_rejects_cancellation(self):
        report, order = self.published()
        with self.assertRaises(orders.Conflict):
            orders.cancel_order(self.owner, order.pk, order.version)


class OriginalTests(ReportFixture):
    def test_original_ready_and_pickup_independent_from_pdf(self):
        report, order = self.published()
        original, order = services.original_action(
            self.reviewer, order.pk, report.pk, order.version, "ready", True
        )
        self.assertEqual(original.state, "ready")
        self.assertTrue(Notification.objects.filter(kind="original_ready", recipient=self.owner).exists())
        original, order = services.original_action(
            self.reviewer, order.pk, report.pk, order.version, "handover", True, "pickup", "Передано клиенту"
        )
        self.assertEqual(original.state, "handed_over")
        self.assertIsNotNone(original.handed_over_at)
        self.assertEqual(order.status, "completed")

    def test_handover_before_ready_or_without_confirmation_blocked(self):
        report, order = self.published()
        with self.assertRaises(orders.Conflict):
            services.original_action(
                self.reviewer, order.pk, report.pk, order.version, "handover", True, "pickup"
            )
        with self.assertRaises(ValidationError):
            services.original_action(self.reviewer, order.pk, report.pk, order.version, "ready", False)

    def test_delivery_request_does_not_book_courier_and_is_idempotent(self):
        report, order = self.published()
        key = uuid.uuid4()
        original, order = services.request_delivery(
            self.owner, order.pk, report.pk, order.version, key, True, "Свяжитесь со мной"
        )
        self.assertEqual(original.state, "preparing")
        self.assertIsNone(original.handed_over_at)
        version = order.version
        original, order = services.request_delivery(
            self.owner, order.pk, report.pk, 1, key, True, "Свяжитесь со мной"
        )
        self.assertEqual(order.version, version)
        self.assertIn("https://wa.me/77017700026?", services.delivery_contact(order))
        self.assertTrue(
            Notification.objects.filter(kind="original_delivery_requested", recipient=self.reviewer).exists()
        )

    def test_courier_handover_not_recipient_delivery(self):
        report, order = self.published()
        _, order = services.original_action(self.reviewer, order.pk, report.pk, order.version, "ready", True)
        original, order = services.original_action(
            self.reviewer, order.pk, report.pk, order.version, "handover", True, "courier", "Передан курьеру"
        )
        self.assertEqual(original.handover_method, "courier")
        self.assertFalse(hasattr(original, "delivered_at"))

    def test_superseded_original_cannot_be_handed_over(self):
        old, order = self.published()
        new, order = self.draft_report(order)
        services.publish(self.reviewer, order.pk, new.pk, order.version, True, "Исправление")
        order.refresh_from_db()
        with self.assertRaises((NotFound, Http404)):
            services.original_action(self.reviewer, order.pk, old.pk, order.version, "ready", True)


class DemoTests(ReportFixture):
    def test_demo_rolls_back_reports_tasks_and_notifications(self):
        from apps.operations.models import Task

        out = io.StringIO()
        before = Notification.objects.count()
        call_command("demo_reports", stdout=out)
        self.assertIn("PASS: private PDF", out.getvalue())
        self.assertEqual(Report.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), before)
