"""A local, rollback-only workflow demonstration; no email, bank or persistent test data."""

import io
import uuid
from tempfile import TemporaryDirectory

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


class Command(BaseCommand):
    help = "LOCAL ONLY: demonstrate Parts 4–5 materials and transitions and roll back every change. No real payments."

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
            order = services.simulate_payment(order.pk)
            self.show(order)
            order = services.review_action(reviewer, order.pk, order.version, "start_review")
            self.show(order)
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
            order = services.cancel_order(owner, order.pk, order.version, "TEST: отмена до начала работ")
            self.show(order)
            self.stdout.write(f"Snapshots: {order.submissions.count()}, events: {order.events.count()}")
            transaction.set_rollback(True)
        self.stdout.write(self.style.SUCCESS("PASS. All demo data rolled back. No email or money sent."))

    def show(self, order):
        self.stdout.write(f"v{order.version}: {order.status} | is_test={order.is_test}")
