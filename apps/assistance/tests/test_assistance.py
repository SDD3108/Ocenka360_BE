import io
import json
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import APIException

from apps.assistance import provider, services, worker
from apps.assistance.models import Job, Permission, PermissionEvent
from apps.assistance.policy import POLICY_VERSION
from apps.assistance.serializers import JobInput
from apps.catalog.services import clone_form, publish_form
from apps.materials.tests.fixtures import seed_test_materials
from apps.orders.models import Order, OrderEvent
from apps.orders.tests.test_orders import ROOT, OrderFixture, valid_answers

CONFIG = {
    "GEMINI_ENABLED": True,
    "GEMINI_API_KEY": "synthetic-unit-test-key",
    "GEMINI_MODEL": "gemini-test-only",
    "GEMINI_PAID_PROJECT_CONFIRMED": True,
    "GEMINI_DATA_PROCESSING_APPROVED": True,
}
RESULT = {
    "answer": "Synthetic guidance only",
    "service_slugs": [],
    "observations": [],
    "warnings": ["TEST ONLY"],
}
USAGE = {"promptTokenCount": 20, "candidatesTokenCount": 10}


def payload(**extra):
    return {
        "client_request_id": str(uuid.uuid4()),
        "kind": "service_guide",
        "policy_version": POLICY_VERSION,
        "confirmed": True,
        "question": "Apartment for sale",
        **extra,
    }


def clean(data):
    obj = JobInput(data=data)
    obj.is_valid(raise_exception=True)
    return obj.validated_data


@override_settings(**CONFIG)
class AssistanceTests(OrderFixture):
    def grant(self, order, photos=False):
        return services.set_permission(self.owner, order.pk, 0, POLICY_VERSION, True, photos, True)

    def job(self, order=None, review=False, **extra):
        actor = self.reviewer if review else self.owner
        data = payload(**extra)
        if order:
            data.update(order_id=str(order.pk), expected_version=order.version)
        return services.create_job(actor, clean(data), review)[0]

    def process(self):
        with patch("apps.assistance.provider.generate", return_value=(RESULT, USAGE)) as mocked:
            count = worker.process()
        return count, mocked

    def test_disabled_default_truthful_status_and_no_job(self):
        with override_settings(GEMINI_ENABLED=False):
            self.assertFalse(self.api.get(ROOT + "config/").json()["features"]["ai"])
            tools = self.api.get(ROOT + "assessment-tools/").json()
            self.assertEqual(tools["ai_status"], "disabled")
            self.assertEqual(tools["damage_calculation_status"], "provider_not_configured")
            self.assertEqual(self.api.post(ROOT + "ai/jobs/", payload()).status_code, 503)
            self.assertEqual(Job.objects.count(), 0)

    def test_configuration_requires_paid_project_and_data_processing(self):
        for key in ["GEMINI_PAID_PROJECT_CONFIRMED", "GEMINI_DATA_PROCESSING_APPROVED"]:
            with override_settings(**{key: False}):
                self.assertEqual(self.api.post(ROOT + "ai/jobs/", payload()).status_code, 503)
        with override_settings(GEMINI_API_KEY=""):
            self.assertEqual(
                self.api.get(ROOT + "assessment-tools/").json()["ai_status"], "credentials_missing"
            )

    def test_service_guide_queue_idempotency_and_no_network_in_request(self):
        data = payload()
        with patch("apps.assistance.provider.generate") as network:
            first = self.api.post(ROOT + "ai/jobs/", data)
            second = self.api.post(ROOT + "ai/jobs/", data)
            network.assert_not_called()
        self.assertEqual(first.status_code, 202, first.json())
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.api.post(ROOT + "ai/jobs/", {**data, "question": "Changed"}).status_code, 409)
        count, mock = self.process()
        self.assertEqual(count, 1)
        job = Job.objects.get()
        self.assertEqual(job.state, "succeeded")
        self.assertEqual(job.input_tokens, 20)
        self.assertNotIn("order_id", json.dumps(mock.call_args.args[0]))
        self.assertTrue(mock.call_args.args[0]["allowed_services"])

    def test_consent_adult_verified_strict_input_and_role(self):
        for extra in [
            {"confirmed": False},
            {"policy_version": "old"},
            {"kind": "description_draft", "order_id": str(uuid.uuid4()), "expected_version": 1},
            {"answer": "forged"},
        ]:
            self.assertIn(self.api.post(ROOT + "ai/jobs/", payload(**extra)).status_code, [400, 403])
        self.owner.adult_confirmed_at = None
        self.owner.save()
        self.assertEqual(self.api.post(ROOT + "ai/jobs/", payload()).status_code, 403)
        self.owner.email_verified_at = None
        self.owner.save()
        self.assertEqual(self.api.get(ROOT + "assessment-tools/").status_code, 403)
        self.assertEqual(Job.objects.count(), 0)

    def test_owner_permission_cannot_be_granted_by_reviewer_or_other(self):
        order = Order.objects.get(pk=self.draft()["id"])
        url = ROOT + f"orders/{order.pk}/ai-permission/"
        self.assertEqual(self.api.get(url).json()["version"], 0)
        data = {
            "expected_version": 0,
            "policy_version": POLICY_VERSION,
            "enabled": True,
            "allow_photos": False,
            "confirmed": True,
        }
        for user in [self.other, self.reviewer]:
            self.api.force_authenticate(user)
            self.assertEqual(self.api.post(url, data).status_code, 404)
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.post(url, data).status_code, 200)
        self.assertEqual(self.api.post(url, data).status_code, 409)
        self.assertEqual(PermissionEvent.objects.count(), 1)

    def test_material_check_uses_missing_slots_without_answers_or_pdf(self):
        order = Order.objects.get(pk=self.draft()["id"])
        self.grant(order)
        job = self.job(order, kind="material_check", question="IIN 123456789012 mail me@test.kz")
        raw = json.dumps(job.context)
        for secret in [
            "Test address",
            "Original address",
            self.owner.email,
            "123456789012",
            "me@test.kz",
            str(order.pk),
        ]:
            self.assertNotIn(secret, raw)
        self.assertTrue(job.context["checklist"]["missing"])
        version = order.version
        events = OrderEvent.objects.count()
        self.process()
        order.refresh_from_db()
        self.assertEqual(order.version, version)
        self.assertEqual(OrderEvent.objects.count(), events)
        self.assertEqual(order.status, "draft")

    def test_order_requires_permission_and_expected_version(self):
        order = Order.objects.get(pk=self.draft()["id"])
        with self.assertRaises(APIException):
            self.job(order, kind="material_check")
        self.grant(order)
        response = self.api.post(
            ROOT + "ai/jobs/", payload(kind="material_check", order_id=str(order.pk), expected_version=99)
        )
        self.assertEqual(response.status_code, 409)
        self.api.force_authenticate(self.other)
        self.assertEqual(
            self.api.post(
                ROOT + "ai/jobs/",
                payload(kind="material_check", order_id=str(order.pk), expected_version=order.version),
            ).status_code,
            404,
        )

    def test_form_help_no_answer_contents_and_current_field(self):
        job = self.job(kind="form_help", service_id=str(self.service.pk), field_key="area_sqm")
        self.assertEqual(job.context["field"]["key"], "area_sqm")
        self.assertNotIn("answers", job.context)
        with self.assertRaises(APIException):
            self.job(kind="form_help", service_id=str(self.service.pk), field_key="made_up")
        draft = clone_form(self.service.current_form_id)
        draft.definition["fields"][0]["label"]["ru"] = "Updated field"
        draft.save()
        publish_form(draft.pk)
        _, network = self.process()
        network.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "stale")

    def test_revocation_cancels_queued_and_erases_retained_contents(self):
        order = Order.objects.get(pk=self.draft()["id"])
        perm = self.grant(order)
        job = self.job(order, kind="material_check")
        services.set_permission(self.owner, order.pk, perm.version, POLICY_VERSION, False, False, True)
        _, network = self.process()
        network.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(job.context, {})

    def test_revocation_during_external_call_discards_response(self):
        order = Order.objects.get(pk=self.draft()["id"])
        perm = self.grant(order)
        job = self.job(order, kind="material_check")

        def generate(*args):
            services.set_permission(self.owner, order.pk, perm.version, POLICY_VERSION, False, False, True)
            return RESULT, USAGE

        with patch("apps.assistance.provider.generate", side_effect=generate):
            worker.process()
        job.refresh_from_db()
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(job.result, {})

    def test_stale_order_before_and_during_generation(self):
        order = Order.objects.get(pk=self.draft()["id"])
        self.grant(order)
        job = self.job(order, kind="material_check")
        Order.objects.filter(pk=order.pk).update(version=2)
        _, network = self.process()
        network.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "stale")
        order.refresh_from_db()
        job = self.job(order, kind="material_check")

        def generate(*args):
            Order.objects.filter(pk=order.pk).update(version=3)
            return RESULT, USAGE

        with patch("apps.assistance.provider.generate", side_effect=generate):
            worker.process()
        job.refresh_from_db()
        self.assertEqual(job.state, "stale")
        self.assertFalse(job.result)

    def test_changed_account_before_dispatch_sends_nothing(self):
        job = self.job()
        self.owner.is_active = False
        self.owner.save()
        _, network = self.process()
        network.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "stale")

    def test_timeout_not_retried_and_interrupted_worker_not_replayed(self):
        job = self.job()
        with patch(
            "apps.assistance.provider.generate", side_effect=provider.ProviderError("network_unknown", True)
        ) as mock:
            self.assertEqual(worker.process(), 1)
            self.assertEqual(worker.process(), 0)
            self.assertEqual(mock.call_count, 1)
        job.refresh_from_db()
        self.assertEqual(job.state, "unknown")
        next_job = self.job()
        worker.claim()
        Job.objects.filter(pk=next_job.pk).update(claimed_at=timezone.now() - timedelta(minutes=6))
        self.assertEqual(worker.recover(), 1)
        next_job.refresh_from_db()
        self.assertEqual(next_job.state, "unknown")

    def test_limits_and_failed_requests_do_not_create_jobs(self):
        with override_settings(GEMINI_USER_DAILY_LIMIT=1):
            self.job()
            self.assertEqual(self.api.post(ROOT + "ai/jobs/", payload()).status_code, 429)
        self.assertEqual(Job.objects.count(), 1)

    def test_global_limit_across_users(self):
        with override_settings(GEMINI_GLOBAL_DAILY_LIMIT=1):
            self.job()
            self.api.force_authenticate(self.other)
            self.assertEqual(self.api.post(ROOT + "ai/jobs/", payload()).status_code, 429)

    def test_cancellation_expiry_and_private_job_history(self):
        job = self.job()
        self.process()
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(ROOT + f"ai/jobs/{job.pk}/").status_code, 404)
        self.assertEqual(self.api.get(ROOT + "ai/jobs/").json()["count"], 0)
        self.api.force_authenticate(self.owner)
        Job.objects.filter(pk=job.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        data = self.api.get(ROOT + f"ai/jobs/{job.pk}/").json()
        self.assertEqual(data["state"], "expired")
        self.assertFalse(data["result"])
        self.assertEqual(worker.cleanup(), 1)
        job.refresh_from_db()
        self.assertEqual(job.context, {})
        second = self.job()
        services.cancel_job(self.owner, second.pk)
        self.assertEqual(worker.process(), 0)

    def test_reviewer_draft_acceptance_never_changes_order(self):
        order = self.submitted()
        self.grant(order)
        job = self.job(order, review=True, kind="description_draft")
        self.process()
        version = order.version
        obj = services.decide_job(self.reviewer, job.pk, "accepted", True, "Checked")
        self.assertEqual(obj.decision, "accepted")
        self.assertEqual(services.decide_job(self.reviewer, job.pk, "accepted", True, "Checked").pk, obj.pk)
        with self.assertRaises(APIException):
            services.decide_job(self.reviewer, job.pk, "rejected", True, "No")
        order.refresh_from_db()
        self.assertEqual(order.version, version)
        self.assertEqual(order.reports.count(), 0)
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.get(ROOT + f"ai/jobs/{job.pk}/").status_code, 404)

    def photos(self):
        order = Order.objects.get(
            pk=self.draft(
                answers={
                    **valid_answers(),
                    "inspection_preference": "remote",
                }
            )["id"]
        )
        seed_test_materials(order)
        self.grant(order, True)
        return order, order.materials.filter(kind="photo").first()

    def test_photo_minimal_transport_and_no_documents(self):
        order, photo = self.photos()
        self.job(order, kind="photo_review", photo_ids=[str(photo.pk)], photos_redacted=True)
        count, mock = self.process()
        self.assertEqual(count, 1)
        images = mock.call_args.args[1]
        self.assertEqual(len(images), 1)
        self.assertNotIn(str(photo.pk), json.dumps(mock.call_args.args[0]))
        document = order.materials.filter(kind="document").first()
        response = self.api.post(
            ROOT + "ai/jobs/",
            payload(
                kind="photo_review",
                order_id=str(order.pk),
                expected_version=order.version,
                photo_ids=[str(document.pk)],
                photos_redacted=True,
            ),
        )
        self.assertEqual(response.status_code, 404)

    def test_photo_needs_explicit_permission_and_mask_confirmation(self):
        order, photo = self.photos()
        with self.assertRaises(APIException):
            self.job(order, kind="photo_review", photo_ids=[str(photo.pk)])
        perm = Permission.objects.get(order=order)
        services.set_permission(self.owner, order.pk, perm.version, POLICY_VERSION, True, False, True)
        with self.assertRaises(APIException):
            self.job(order, kind="photo_review", photo_ids=[str(photo.pk)], photos_redacted=True)

    def test_withdrawn_corrupt_vin_and_rejected_photos_never_dispatched(self):
        order, photo = self.photos()
        job = self.job(order, kind="photo_review", photo_ids=[str(photo.pk)], photos_redacted=True)
        photo.withdrawn_at = timezone.now()
        photo.save()
        _, mock = self.process()
        mock.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "stale")
        photo.withdrawn_at = None
        photo.slot_key = "vin"
        photo.save()
        with self.assertRaises(APIException):
            self.job(order, kind="photo_review", photo_ids=[str(photo.pk)], photos_redacted=True)
        photo.slot_key = "rooms"
        photo.review_status = "rejected"
        photo.save()
        with self.assertRaises(APIException):
            self.job(order, kind="photo_review", photo_ids=[str(photo.pk)], photos_redacted=True)
        photo.review_status = "accepted"
        photo.save()
        job = self.job(order, kind="photo_review", photo_ids=[str(photo.pk)], photos_redacted=True)
        from apps.materials.storage import PrivateStorage

        with PrivateStorage().open(photo.storage_key, "wb") as stream:
            stream.write(b"corrupt")
        _, mock = self.process()
        mock.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "stale")

    def test_rollback_does_not_leave_queued_job(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.job()
                raise RuntimeError("rollback")
        self.assertEqual(Job.objects.count(), 0)


@override_settings(**CONFIG)
class ProviderTests(SimpleTestCase):
    def response(self, result=None, **extra):
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(RESULT if result is None else result)}]},
                    **extra,
                }
            ],
            "usageMetadata": USAGE,
        }

    def invoke(self, value, context=None):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(value).encode()
        with patch("apps.assistance.provider.build_opener", return_value=opener):
            result = provider.generate(context or {}, [], "gemini-test-only")
        return result, opener

    def test_fixed_host_secret_header_schema_and_no_tools(self):
        (result, _), opener = self.invoke(self.response())
        self.assertEqual(result, RESULT)
        signed = self.response(
            content={"parts": [{"text": json.dumps(RESULT), "thoughtSignature": "opaque"}]}
        )
        self.assertEqual(self.invoke(signed)[0][0], RESULT)
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-test-only:generateContent",
        )
        self.assertNotIn(CONFIG["GEMINI_API_KEY"], request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), CONFIG["GEMINI_API_KEY"])
        data = json.loads(request.data)
        self.assertNotIn("tools", data)
        self.assertEqual(data["generationConfig"]["responseJsonSchema"], provider.API_SCHEMA)
        self.assertIsNone(
            provider.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")
        )

    def test_invalid_schema_hallucinated_slug_extra_fields_and_links(self):
        for result in [
            {},
            {**RESULT, "price": 100},
            {**RESULT, "service_slugs": ["invented"]},
            {**RESULT, "answer": "https://evil.invalid"},
            {**RESULT, "answer": "x" * 3001},
        ]:
            with self.assertRaises(provider.ProviderError):
                self.invoke(self.response(result))

    def test_blocked_truncated_cited_and_tool_response(self):
        cases = [
            {"promptFeedback": {"blockReason": "SAFETY"}},
            self.response(finishReason="MAX_TOKENS"),
            self.response(citationMetadata={"citationSources": [{}]}),
            self.response(content={"parts": [{"functionCall": {"name": "pay"}}]}),
        ]
        for value in cases:
            with self.assertRaises(provider.ProviderError):
                self.invoke(value)

    def test_bad_json_oversize_timeout_http_and_disabled(self):
        for raw in [b"not json", b"x" * 131073]:
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = raw
            with (
                patch("apps.assistance.provider.build_opener", return_value=opener),
                self.assertRaises(provider.ProviderError),
            ):
                provider.generate({}, [], "gemini-test-only")
        for exc in [URLError("SECRET"), HTTPError("https://provider.invalid", 429, "SECRET", {}, None)]:
            opener = MagicMock()
            opener.open.side_effect = exc
            with (
                patch("apps.assistance.provider.build_opener", return_value=opener),
                self.assertRaises(provider.ProviderError) as captured,
            ):
                provider.generate({}, [], "gemini-test-only")
            self.assertNotIn("SECRET", str(captured.exception))
        with (
            override_settings(GEMINI_ENABLED=False),
            patch("apps.assistance.provider.build_opener") as network,
            self.assertRaises(provider.ProviderError),
        ):
            provider.generate({}, [], "gemini-test-only")
        network.assert_not_called()

    def test_model_path_injection_rejected(self):
        with self.assertRaises(provider.ProviderError):
            provider.generate({}, [], "../evil")


class DemoTests(OrderFixture):
    def test_demo_rolls_back_without_external_network(self):
        from apps.accounts.models import User

        before = (Job.objects.count(), Order.objects.count(), User.objects.count())
        out = io.StringIO()
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")):
            call_command("demo_assistance", stdout=out)
        self.assertIn("PASS", out.getvalue())
        self.assertEqual(before, (Job.objects.count(), Order.objects.count(), User.objects.count()))
        with override_settings(ALLOW_ORDER_SIMULATION=False), self.assertRaises(CommandError):
            call_command("demo_assistance", stdout=out)
