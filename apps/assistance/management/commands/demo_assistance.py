"""Local synthetic fixtures, a stubbed HTTP response and a rollback; never contacts Google."""

import io
import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.assistance import services, worker
from apps.assistance.models import Job
from apps.assistance.policy import POLICY_VERSION
from apps.assistance.serializers import JobInput
from apps.catalog.models import Service
from apps.comparables import services as market
from apps.comparables.serializers import CreateInput
from apps.legal.models import LegalDocument
from apps.legal.services import accept_documents
from apps.orders import services as orders
from apps.orders.models import Asset


class Command(BaseCommand):
    help = "LOCAL ONLY: synthetic AI/market demonstration with stubbed Gemini HTTP, then rollback. No real API key needed."

    def handle(self, *args, **options):
        if not settings.ALLOW_ORDER_SIMULATION or not settings.ALLOW_DRAFT_LEGAL_DOCUMENTS:
            raise CommandError("Local simulation only.")
        with transaction.atomic():
            call_command("seed_legal_drafts", stdout=io.StringIO())
            call_command("seed_catalog", stdout=io.StringIO())
            docs = list(
                LegalDocument.objects.filter(language="ru", is_active=True).values_list("pk", flat=True)
            )
            users = []
            for role in ["client", "appraiser"]:
                user = User.objects.create_user(f"demo-{uuid.uuid4().hex}@example.invalid", role=role)
                user.email_verified_at = timezone.now()
                user.save()
                users.append(accept_documents(user, docs, True))
            owner, reviewer = users
            service = Service.objects.get(slug="apartment")
            asset = Asset.objects.create(
                owner=owner, title="SYNTHETIC AI DEMO", kind="apartment", city="Астана"
            )
            order, _ = orders.create_order(
                owner,
                {
                    "client_request_id": uuid.uuid4(),
                    "asset_id": asset.pk,
                    "service_id": service.pk,
                    "form_id": service.current_form_id,
                    "language": "ru",
                    "answers": {
                        "applicant_type": "individual",
                        "applicant_role": "owner",
                        "purpose": "sale",
                        "city": "Астана",
                        "address": "TEST ONLY",
                        "area_sqm": "60",
                        "inspection_preference": "onsite",
                        "urgency": "regular",
                    },
                },
            )
            # Isolated local fixture: no real ledger entries, bank confirmations or new customer data.
            order.is_test = True
            order.status = "under_review"
            order.review_unlocked_at = timezone.now()
            order.save()
            consent = services.set_permission(owner, order.pk, 0, POLICY_VERSION, True, False, True)
            request = JobInput(
                data={
                    "client_request_id": str(uuid.uuid4()),
                    "kind": "material_check",
                    "order_id": str(order.pk),
                    "expected_version": order.version,
                    "policy_version": POLICY_VERSION,
                    "confirmed": True,
                }
            )
            request.is_valid(raise_exception=True)
            result = {
                "answer": "SYNTHETIC TEST ONLY: appraiser checks the materials.",
                "service_slugs": [],
                "observations": [],
                "warnings": ["TEST ONLY"],
            }
            response = {
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(result)}]}}
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            }
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            with (
                override_settings(
                    GEMINI_ENABLED=True,
                    GEMINI_API_KEY="synthetic-not-a-key",
                    GEMINI_MODEL="gemini-test-only",
                    GEMINI_PAID_PROJECT_CONFIRMED=True,
                    GEMINI_DATA_PROCESSING_APPROVED=True,
                ),
                patch("apps.assistance.provider.build_opener", return_value=opener),
            ):
                job, _ = services.create_job(owner, request.validated_data)
                worker.run_one(worker.claim())
                job.refresh_from_db()
                if job.state != "succeeded" or opener.open.call_count != 1:
                    raise CommandError("AI demonstration failed.")
            data = CreateInput(
                data={
                    "expected_version": order.version,
                    "client_request_id": str(uuid.uuid4()),
                    "as_of": timezone.localdate().isoformat(),
                    "comparability_confirmed": True,
                    "comparables": [
                        {
                            "source_url": f"https://listings.example.com/demo/{i}",
                            "source_label": "SYNTHETIC TEST ONLY",
                            "observed_on": timezone.localdate().isoformat(),
                            "city": "Астана",
                            "area_sqm": "50",
                            "asking_price_kzt": str(value),
                        }
                        for i, value in enumerate([20000000, 25000000, 30000000])
                    ],
                }
            )
            data.is_valid(raise_exception=True)
            estimate, _ = market.create(reviewer, order.pk, data.validated_data)
            if Decimal(estimate.result["indication"]) != Decimal("30000000.00"):
                raise CommandError("Market arithmetic failed.")
            market.action(reviewer, order.pk, estimate.pk, 1, True, "", True)
            services.set_permission(owner, order.pk, consent.version, POLICY_VERSION, False, False, True)
            job = Job.objects.get(pk=job.pk)
            if job.result or job.context:
                raise CommandError("Revocation failed.")
            transaction.set_rollback(True)
        self.stdout.write(
            self.style.SUCCESS(
                "PASS: stubbed Gemini, private consent, revocation and manual comparable estimate. All demo data rolled back; no external requests."
            )
        )
