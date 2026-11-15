"""A local, rollback-only workflow demonstration; no email, bank or persistent test data."""

import io
import uuid
from datetime import timedelta
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone
from pypdf import PdfWriter

from apps.accounts.models import User
from apps.catalog.models import Category, FormRevision, Service
from apps.catalog.seed_data import definition_for, tr
from apps.catalog.services import publish_form
from apps.legal.models import LegalDocument
from apps.legal.services import accept_documents
from apps.materials.services import checklist, upload
from apps.orders import services
from apps.payments import services as payments
from apps.payments.models import Refund
from apps.pricing import services as pricing
from apps.pricing.calculator import estimate
from apps.scheduling import services as scheduling


class Command(BaseCommand):
    help = "LOCAL ONLY: demonstrate Parts 4–7 materials, prices, calendar and transitions and roll back every change. No real payments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--communications",
            action="store_true",
            help="Exercise comments, material requests and the notification inbox.",
        )
        parser.add_argument(
            "--payments",
            action="store_true",
            help="Exercise the mock payment ledger, without banks or money.",
        )
        parser.add_argument(
            "--inspection",
            action="store_true",
            help="Also simulate the clock for an inspection and its report deadline; all changes roll back.",
        )

    def handle(self, *args, **options):
        if not settings.ALLOW_ORDER_SIMULATION:
            raise CommandError("Demo is disabled outside local/test.")
        with (
            TemporaryDirectory(prefix="o360-demo-") as folder,
            override_settings(MATERIAL_STORAGE_ROOT=folder),
            transaction.atomic(),
        ):
            call_command("seed_legal_drafts", stdout=self.stdout)
            suffix = uuid.uuid4().hex
            users = []
            for role in ("client", "appraiser"):
                user = User.objects.create_user(f"demo-{role}-{suffix}@example.invalid", role=role)
                user.email_verified_at = timezone.now()
                user.save()
                ids = list(
                    LegalDocument.objects.filter(language="ru", is_active=True).values_list("id", flat=True)
                )
                users.append(accept_documents(user, ids, True))
            owner, reviewer = users
            category = Category.objects.create(slug="demo-" + suffix, title=tr("Тест", "Тест", "Test"))
            service = Service.objects.create(
                category=category,
                slug="demo-" + suffix,
                title=tr("Тест", "Тест", "Test"),
                summary=tr("Тест", "Тест", "Test"),
            )
            form = FormRevision.objects.create(
                service=service, version=1, definition=definition_for("apartment")
            )
            publish_form(form.pk)
            asset = services.create_asset(owner, {"title": "TEST", "kind": "apartment", "city": "Астана"})
            answers = {
                "applicant_type": "individual",
                "applicant_role": "owner",
                "purpose": "sale",
                "city": "Астана",
                "address": "TEST",
                "area_sqm": "65.5",
                "inspection_preference": "onsite",
                "urgency": "regular",
            }
            order, _ = services.create_order(
                owner,
                {
                    "client_request_id": uuid.uuid4(),
                    "asset_id": asset.pk,
                    "service_id": service.pk,
                    "form_id": form.pk,
                    "language": "ru",
                    "answers": answers,
                },
            )
            self.show(order)
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            stream = io.BytesIO()
            writer.write(stream)
            for slot in checklist(order)["missing"]:
                _, order, _ = upload(
                    owner,
                    order.pk,
                    {
                        "expected_version": order.version,
                        "client_upload_id": uuid.uuid4(),
                        "kind": slot["kind"],
                        "slot_key": slot["slot_key"],
                        "file": SimpleUploadedFile(
                            "demo.pdf", stream.getvalue(), content_type="application/pdf"
                        ),
                    },
                )
            self.stdout.write(f"Synthetic materials uploaded: {order.materials.count()}")
            order = services.submit_order(owner, order.pk, order.version, True)
            self.show(order)
            if options["payments"]:
                intent, order, _ = payments.create_intent(
                    owner,
                    order.pk,
                    {
                        "expected_version": order.version,
                        "client_request_id": uuid.uuid4(),
                        "purpose": "advance",
                        "provider": "mock",
                    },
                )
                payments.simulate_payment(intent.pk)
                order.refresh_from_db()
                self.stdout.write("TEST ONLY advance recorded: 2000 KZT by default")
            else:
                order = services.simulate_payment(order.pk)
            self.show(order)
            order = services.review_action(reviewer, order.pk, order.version, "start_review")
            self.show(order)
            if options["communications"]:
                from apps.communications import services as communication
                from apps.materials.services import review_material
                from apps.notifications.models import Notification

                _, order, _ = communication.comment(owner, order.pk, uuid.uuid4(), "TEST: вопрос")
                material = order.materials.filter(kind="document").first()
                req, order, _ = communication.request_materials(
                    reviewer,
                    order.pk,
                    order.version,
                    uuid.uuid4(),
                    "TEST: уточнение",
                    [
                        {
                            "kind": material.kind,
                            "slot_key": material.slot_key,
                            "instruction": "TEST: приложите копию",
                            "where_to_get": "TEST",
                        }
                    ],
                )
                _, order, _ = communication.respond(
                    owner, order.pk, req.pk, order.version, uuid.uuid4(), "TEST: ответ", [material.pk], True
                )
                order = services.review_action(reviewer, order.pk, order.version, "start_review")
                _, order = review_material(
                    reviewer, order.pk, material.pk, order.version, "accepted", "TEST: проверено"
                )
                req, order = communication.decide(
                    reviewer, order.pk, req.pk, order.version, "accept", "TEST: принято"
                )
                if (
                    req.state != "resolved"
                    or not Notification.objects.filter(
                        recipient=owner, kind="material_request_accept"
                    ).exists()
                ):
                    raise CommandError("Communication scenario failed.")
                self.stdout.write(
                    "PASS: comment, structured request, pinned reply, reviewer acceptance and notification inbox; no push sent"
                )
            order = services.review_action(
                reviewer, order.pk, order.version, "request_information", "TEST: уточните площадь"
            )
            self.show(order)
            order = services.update_order(
                owner,
                order.pk,
                {"expected_version": order.version, "answers": {**answers, "area_sqm": "66.5"}},
            )
            order = services.submit_order(owner, order.pk, order.version, True)
            self.show(order)
            order = services.review_action(reviewer, order.pk, order.version, "start_review")
            order = services.review_action(reviewer, order.pk, order.version, "accept")
            self.show(order)
            tariff = pricing.create_tariff(
                reviewer, {"service_id": service.pk, "mode": "flat", "base_amount": Decimal("20000.00")}
            )
            pricing.publish_tariff(reviewer, tariff.pk, None)
            self.stdout.write(f"DEMO ONLY estimate: {estimate(service, 'Астана', 'regular')['subtotal']} KZT")
            quote, order, _ = pricing.issue_quote(
                reviewer,
                order.pk,
                {
                    "expected_version": order.version,
                    "client_request_id": uuid.uuid4(),
                    "lines": [{"kind": "appraisal", "title": "TEST ONLY", "amount": Decimal("20000.00")}],
                    "terms": "DEMO ONLY: no real quote or payment",
                    "expires_at": timezone.now() + timedelta(hours=24),
                },
            )
            quote, order = pricing.decide_quote(
                owner, order.pk, quote.pk, order.version, "accept", confirmed=True
            )
            self.show(order)
            self.stdout.write(f"DEMO ONLY agreed: {quote.total} KZT; no payment was made.")
            if options["payments"]:
                intent, order, _ = payments.create_intent(
                    owner,
                    order.pk,
                    {
                        "expected_version": order.version,
                        "client_request_id": uuid.uuid4(),
                        "purpose": "balance",
                        "provider": "mock",
                    },
                )
                payments.simulate_payment(intent.pk)
                order.refresh_from_db()
                self.stdout.write(f"TEST ONLY net paid: {payments.balance(order)['net_paid']}")
            start = timezone.now() + timedelta(days=1)
            appointment, order, _ = scheduling.propose(
                owner,
                order.pk,
                {
                    "expected_version": order.version,
                    "client_request_id": uuid.uuid4(),
                    "starts_at": start,
                    "ends_at": start + timedelta(hours=1),
                    "mode": "onsite",
                    "note": "TEST ONLY",
                },
            )
            appointment, order = scheduling.confirm_request(
                reviewer,
                order.pk,
                appointment.pk,
                {
                    "expected_version": order.version,
                    "starts_at": start,
                    "ends_at": start + timedelta(hours=1),
                    "report_hours": 24,
                    "buffer_before_minutes": 0,
                    "buffer_after_minutes": 30,
                },
            )
            self.show(order)
            if options["inspection"]:
                with patch(
                    "apps.scheduling.services.timezone.now", return_value=start + timedelta(minutes=5)
                ):
                    appointment, order = scheduling.start_inspection(
                        reviewer, order.pk, appointment.pk, order.version
                    )
                self.show(order)
                due = order.inspection.report_due_at
                with patch(
                    "apps.scheduling.services.timezone.now", return_value=start + timedelta(minutes=35)
                ):
                    appointment, order = scheduling.finish_inspection(
                        reviewer, order.pk, appointment.pk, order.version, "TEST ONLY completed"
                    )
                self.stdout.write(f"DEMO ONLY report due: {due.isoformat()} (from actual simulated start)")
            else:
                order = services.cancel_order(owner, order.pk, order.version, "TEST: отмена до начала работ")
            self.show(order)
            if options["payments"] and not options["inspection"]:
                for refund in Refund.objects.filter(receipt__intent__order=order):
                    payments.simulate_refund(refund.pk)
                order.refresh_from_db()
                self.stdout.write(f"TEST ONLY refunded: {payments.balance(order)['refunded']}")
            self.stdout.write(f"Snapshots: {order.submissions.count()}, events: {order.events.count()}")
            transaction.set_rollback(True)
        self.stdout.write(self.style.SUCCESS("PASS. All demo data rolled back. No email or money sent."))

    def show(self, order):
        self.stdout.write(f"v{order.version}: {order.status} | is_test={order.is_test}")
