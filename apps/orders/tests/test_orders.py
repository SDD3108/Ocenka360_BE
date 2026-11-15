import io
import uuid
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.catalog.models import Service
from apps.catalog.services import clone_form, publish_form
from apps.legal.models import LegalDocument
from apps.legal.services import accept_documents
from apps.orders.models import Asset, Order, OrderEvent, OrderSubmission
from apps.orders.services import simulate_payment

ROOT = "/api/v1/"


def valid_answers():
    return {
        "applicant_type": "individual",
        "applicant_role": "owner",
        "purpose": "sale",
        "city": "Астана",
        "address": "Test address",
        "area_sqm": "65.5",
        "inspection_preference": "onsite",
        "urgency": "regular",
    }


class OrderFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_legal_drafts", stdout=io.StringIO())
        call_command("seed_catalog", stdout=io.StringIO())
        cls.owner = cls.make_user("owner@example.com")
        cls.other = cls.make_user("other@example.com")
        cls.reviewer = cls.make_user("reviewer@example.com", role="appraiser")
        cls.service = Service.objects.get(slug="apartment")
        cls.asset = Asset.objects.create(
            owner=cls.owner, title="Home", kind="apartment", city="Астана", address="Original address"
        )

    @classmethod
    def make_user(cls, email, **kwargs):
        user = User.objects.create_user(email=email, **kwargs)
        user.email_verified_at = timezone.now()
        user.save()
        ids = list(LegalDocument.objects.filter(language="ru", is_active=True).values_list("id", flat=True))
        return accept_documents(user, ids, True)

    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def create(self, answers=None, **extra):
        data = {
            "client_request_id": str(uuid.uuid4()),
            "asset_id": str(self.asset.pk),
            "service_id": str(self.service.pk),
            "form_id": str(self.service.current_form_id),
            "answers": valid_answers() if answers is None else answers,
            **extra,
        }
        return self.api.post(ROOT + "orders/", data)

    def draft(self, **kwargs):
        response = self.create(**kwargs)
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def confirm(self, order):
        return self.api.post(
            ROOT + f"orders/{order['id']}/submit/", {"expected_version": order["version"], "confirmed": True}
        )

    def submitted(self):
        order = self.draft()
        response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.json())
        return simulate_payment(order["id"])

    def action(self, order, action, message=""):
        self.api.force_authenticate(self.reviewer)
        return self.api.post(
            ROOT + f"review/orders/{order.pk}/actions/",
            {"expected_version": order.version, "action": action, "message": message},
        )


class AssetTests(OrderFixture):
    def test_create_patch_archive_and_restore(self):
        response = self.api.post(ROOT + "assets/", {"title": "Car", "kind": "vehicle", "city": "Астана"})
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(Asset.objects.get(pk=data["id"]).owner, self.owner)
        response = self.api.patch(
            ROOT + f"assets/{data['id']}/", {"expected_version": 1, "title": "Updated", "is_archived": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], 2)
        self.assertEqual(self.api.get(ROOT + "assets/?archived=true").json()["count"], 1)
        response = self.api.patch(
            ROOT + f"assets/{data['id']}/", {"expected_version": 2, "is_archived": False}
        )
        self.assertEqual(response.status_code, 200)

    def test_foreign_asset_list_detail_patch_are_isolated(self):
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(ROOT + "assets/").json()["count"], 0)
        path = ROOT + f"assets/{self.asset.pk}/"
        self.assertEqual(self.api.get(path).status_code, 404)
        self.assertEqual(self.api.patch(path, {"expected_version": 1, "title": "Stolen"}).status_code, 404)

    def test_client_cannot_assign_owner_or_overwrite_version(self):
        response = self.api.post(
            ROOT + "assets/", {"title": "Test", "kind": "other", "city": "City", "owner": str(self.other.pk)}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.api.patch(
                ROOT + f"assets/{self.asset.pk}/", {"expected_version": 1, "version": 99}
            ).status_code,
            400,
        )

    def test_asset_stale_write_rejected(self):
        path = ROOT + f"assets/{self.asset.pk}/"
        self.assertEqual(self.api.patch(path, {"expected_version": 1, "title": "First"}).status_code, 200)
        response = self.api.patch(path, {"expected_version": 1, "title": "Lost update"})
        self.assertEqual(response.status_code, 409)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.title, "First")

    def test_asset_delete_is_not_exposed(self):
        self.assertEqual(self.api.delete(ROOT + f"assets/{self.asset.pk}/").status_code, 405)

    def test_active_asset_limit_and_archive_escape(self):
        with override_settings(MAX_ACTIVE_ASSETS=1):
            data = {"title": "Next", "kind": "other", "city": "City"}
            self.assertEqual(self.api.post(ROOT + "assets/", data).status_code, 409)
            self.api.patch(ROOT + f"assets/{self.asset.pk}/", {"expected_version": 1, "is_archived": True})
            self.assertEqual(self.api.post(ROOT + "assets/", data).status_code, 201)
            self.assertEqual(
                self.api.patch(
                    ROOT + f"assets/{self.asset.pk}/", {"expected_version": 2, "is_archived": False}
                ).status_code,
                409,
            )

    def test_unverified_or_unconsented_user_cannot_write(self):
        user = User.objects.create_user("unverified@example.com")
        self.api.force_authenticate(user)
        self.assertEqual(self.api.get(ROOT + "assets/").status_code, 403)
        user.email_verified_at = timezone.now()
        user.save()
        self.assertEqual(
            self.api.post(ROOT + "assets/", {"title": "T", "kind": "other", "city": "C"}).status_code, 403
        )


class DraftTests(OrderFixture):
    def test_partial_draft_persists_and_no_payment_or_review_access(self):
        order = self.draft(answers={})
        self.assertEqual(order["status"], "draft")
        self.assertEqual(order["payment_status"], "not_paid")
        self.assertFalse(order["is_test"])
        self.assertEqual(OrderEvent.objects.count(), 1)
        self.assertEqual(OrderSubmission.objects.count(), 0)
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(ROOT + "review/orders/").json()["count"], 0)
        self.assertEqual(self.api.get(ROOT + f"review/orders/{order['id']}/").status_code, 404)

    def test_identical_create_retry_returns_same_order_and_event(self):
        request_id = str(uuid.uuid4())
        first = self.create(client_request_id=request_id)
        second = self.create(client_request_id=request_id)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderEvent.objects.count(), 1)

    def test_idempotency_key_reuse_with_other_payload_conflicts(self):
        request_id = str(uuid.uuid4())
        self.draft(client_request_id=request_id)
        response = self.create(answers={}, client_request_id=request_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")

    def test_idempotency_key_scoped_to_owner(self):
        request_id = str(uuid.uuid4())
        first = self.draft(client_request_id=request_id)
        foreign = Asset.objects.create(owner=self.other, title="Other", kind="other", city="City")
        self.api.force_authenticate(self.other)
        second = self.draft(client_request_id=request_id, asset_id=str(foreign.pk))
        self.assertNotEqual(first["id"], second["id"])

    def test_cannot_use_someone_elses_asset(self):
        self.api.force_authenticate(self.other)
        self.assertEqual(self.create().status_code, 404)
        self.assertEqual(Order.objects.count(), 0)

    def test_archived_asset_cannot_create_order(self):
        self.asset.is_archived = True
        self.asset.save()
        response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "asset_archived")

    def test_invalid_draft_answers_are_rejected_even_when_incomplete(self):
        for answers in ({"area_sqm": True}, {"company_name": "Hidden"}, {"role": "admin"}):
            self.assertEqual(self.create(answers=answers).status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_unknown_writable_fields_rejected(self):
        for data in (
            {"status": "accepted"},
            {"payment_status": "test_confirmed"},
            {"owner_id": str(self.other.pk)},
            {"is_test": True},
        ):
            self.assertEqual(self.create(**data).status_code, 400)

    def test_patch_replaces_answers_and_version_conflict(self):
        order = self.draft()
        path = ROOT + f"orders/{order['id']}/"
        response = self.api.patch(
            path, {"expected_version": 1, "answers": {"applicant_type": "company", "company_name": "Test"}}
        )
        self.assertEqual(response.status_code, 200, response.json())
        self.assertNotIn("address", response.json()["answers"])
        self.assertEqual(self.api.patch(path, {"expected_version": 1, "answers": {}}).status_code, 409)

    def test_draft_list_filters_owner_and_status_without_answers(self):
        self.draft()
        data = self.api.get(ROOT + "orders/?status=draft").json()
        self.assertEqual(data["count"], 1)
        self.assertNotIn("answers", data["results"][0])
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(ROOT + "orders/").json()["count"], 0)

    def test_all_foreign_order_routes_return_404(self):
        order = self.draft()
        self.api.force_authenticate(self.other)
        path = ROOT + f"orders/{order['id']}/"
        for suffix in ("", "history/", "submissions/", "form/"):
            self.assertEqual(self.api.get(path + suffix).status_code, 404)
        self.assertEqual(self.api.patch(path, {"expected_version": 1, "answers": {}}).status_code, 404)
        self.assertEqual(
            self.api.post(path + "submit/", {"expected_version": 1, "confirmed": True}).status_code, 404
        )
        self.assertEqual(self.api.post(path + "cancel/", {"expected_version": 1}).status_code, 404)

    def test_open_order_limit_preserves_identical_retry(self):
        request_id = str(uuid.uuid4())
        with override_settings(MAX_OPEN_ORDERS=1):
            order = self.draft(client_request_id=request_id)
            self.assertEqual(self.create().status_code, 409)
            self.assertEqual(self.create(client_request_id=request_id).status_code, 200)
            self.api.post(ROOT + f"orders/{order['id']}/cancel/", {"expected_version": 1})
            self.assertEqual(self.create().status_code, 201)


class SubmissionTests(OrderFixture):
    def test_complete_confirm_freezes_snapshot_and_waits_for_payment(self):
        order = self.draft()
        response = self.confirm(order)
        self.assertEqual(response.status_code, 200, response.json())
        data = response.json()
        self.assertEqual(data["status"], "awaiting_payment")
        self.assertIsNone(data["review_unlocked_at"])
        snap = OrderSubmission.objects.get()
        self.assertEqual(snap.answers["area_sqm"], "65.5")
        self.api.patch(ROOT + f"assets/{self.asset.pk}/", {"expected_version": 1, "address": "New address"})
        saved = self.api.get(ROOT + f"orders/{order['id']}/").json()
        self.assertEqual(saved["asset_snapshot"]["address"], "Original address")
        self.assertEqual(snap.asset_snapshot["address"], "Original address")

    def test_incomplete_or_unconfirmed_submission_no_partial_changes(self):
        order = self.draft(answers={})
        self.assertEqual(self.confirm(order).status_code, 400)
        saved = Order.objects.get(pk=order["id"])
        self.assertEqual(saved.status, "draft")
        self.assertEqual(saved.version, 1)
        self.assertEqual(saved.events.count(), 1)
        self.assertEqual(OrderSubmission.objects.count(), 0)
        response = self.api.post(
            ROOT + f"orders/{order['id']}/submit/", {"expected_version": 1, "confirmed": False}
        )
        self.assertEqual(response.status_code, 400)

    def test_double_submit_does_not_duplicate_history(self):
        order = self.draft()
        self.assertEqual(self.confirm(order).status_code, 200)
        self.assertEqual(self.confirm(order).status_code, 409)
        self.assertEqual(OrderSubmission.objects.count(), 1)
        self.assertEqual(OrderEvent.objects.count(), 2)

    def test_publication_stales_draft_and_explicit_rebase_works(self):
        order = self.draft()
        form = clone_form(self.service.current_form_id)
        publish_form(form.pk)
        response = self.confirm(order)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "stale_form")
        patched = self.api.patch(
            ROOT + f"orders/{order['id']}/",
            {"expected_version": 1, "form_id": str(form.pk), "answers": valid_answers()},
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(self.confirm(patched.json()).status_code, 200)

    def test_pinned_form_can_be_read_after_catalog_changes(self):
        order = self.draft(language="kk")
        self.confirm(order)
        old_id = order["form_id"]
        new = clone_form(self.service.current_form_id)
        new.definition["fields"][0]["label"]["kk"] = "Updated test label"
        new.save()
        publish_form(new.pk)
        response = self.api.get(ROOT + f"orders/{order['id']}/form/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], old_id)
        self.assertEqual(response.json()["language"], "kk")
        self.assertNotEqual(response.json()["fields"][0]["label"], "Updated test label")
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(ROOT + f"review/orders/{order['id']}/form/").status_code, 404)
        simulate_payment(order["id"])
        self.assertEqual(
            self.api.get(ROOT + f"review/orders/{order['id']}/form/?language=en").json()["id"], old_id
        )

    def test_inactive_service_or_asset_blocks_confirmation(self):
        order = self.draft()
        self.service.is_active = False
        self.service.save()
        self.assertEqual(self.confirm(order).status_code, 409)
        self.service.is_active = True
        self.service.save()
        self.asset.is_archived = True
        self.asset.save()
        self.assertEqual(self.confirm(order).status_code, 409)

    def test_confirmed_answers_not_editable_before_information_request(self):
        order = self.draft()
        response = self.confirm(order)
        self.assertEqual(
            self.api.patch(
                ROOT + f"orders/{order['id']}/",
                {"expected_version": response.json()["version"], "answers": {}},
            ).status_code,
            409,
        )

    def test_transaction_failure_rolls_back_order_and_event(self):
        order = self.draft()
        with patch("apps.orders.services.snapshot_submission", side_effect=RuntimeError("test rollback")):
            response = self.confirm(order)
        self.assertEqual(response.status_code, 500)
        saved = Order.objects.get(pk=order["id"])
        self.assertEqual(saved.version, 1)
        self.assertEqual(saved.status, "draft")
        self.assertEqual(saved.events.count(), 1)

    def test_history_append_only_and_cannot_delete_linked_assets_or_forms(self):
        order = self.draft()
        self.confirm(order)
        submission = OrderSubmission.objects.get()
        submission.answers = {}
        with self.assertRaises(ValidationError):
            submission.save()
        with self.assertRaises(ValidationError):
            submission.delete()
        event = OrderEvent.objects.first()
        event.message = "Rewrite"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ProtectedError):
            self.asset.delete()
        with self.assertRaises(ProtectedError):
            self.service.current_form.delete()


class ReviewTests(OrderFixture):
    def test_client_cannot_access_review_api(self):
        order = self.submitted()
        for suffix in ("", f"{order.pk}/", f"{order.pk}/history/", f"{order.pk}/submissions/"):
            self.assertEqual(self.api.get(ROOT + "review/orders/" + suffix).status_code, 403)
        self.assertEqual(
            self.api.post(
                ROOT + f"review/orders/{order.pk}/actions/",
                {"expected_version": order.version, "action": "accept"},
            ).status_code,
            403,
        )

    def test_unpaid_order_hidden_from_reviewer_and_review_actions(self):
        order = self.draft()
        self.confirm(order)
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(ROOT + "review/orders/").json()["count"], 0)
        self.assertEqual(
            self.api.post(
                ROOT + f"review/orders/{order['id']}/actions/",
                {"expected_version": 2, "action": "start_review"},
            ).status_code,
            404,
        )

    def test_simulation_command_requires_confirmation_and_is_disabled_in_production(self):
        order = self.draft()
        self.confirm(order)
        with self.assertRaises(CommandError):
            call_command("simulate_order_payment", order["id"], stdout=io.StringIO())
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            with self.assertRaises(CommandError):
                call_command("simulate_order_payment", order["id"], confirm_test=True, stdout=io.StringIO())
        call_command("simulate_order_payment", order["id"], confirm_test=True, stdout=io.StringIO())
        saved = Order.objects.get(pk=order["id"])
        self.assertTrue(saved.is_test)
        self.assertEqual(saved.payment_status, "test_confirmed")
        with self.assertRaises(CommandError):
            call_command("simulate_order_payment", order["id"], confirm_test=True, stdout=io.StringIO())

    def test_review_information_resubmit_and_accept_flow(self):
        order = self.submitted()
        self.assertEqual(self.action(order, "start_review").status_code, 200)
        order.refresh_from_db()
        self.assertEqual(
            self.action(order, "request_information", "Уточните площадь по техпаспорту").status_code, 200
        )
        order.refresh_from_db()
        pinned = order.form_id
        new = clone_form(pinned)
        publish_form(new.pk)
        self.api.force_authenticate(self.owner)
        path = ROOT + f"orders/{order.pk}/"
        self.assertEqual(
            self.api.patch(path, {"expected_version": order.version, "form_id": str(new.pk)}).status_code, 409
        )
        patched = self.api.patch(
            path, {"expected_version": order.version, "answers": {**valid_answers(), "area_sqm": "66.5"}}
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(self.confirm(patched.json()).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.form_id, pinned)
        self.assertEqual(order.submissions.count(), 2)
        self.assertEqual(order.submissions.first().answers["area_sqm"], "65.5")
        self.assertEqual(order.submissions.last().answers["area_sqm"], "66.5")
        self.assertEqual(self.action(order, "start_review").status_code, 200)
        order.refresh_from_db()
        response = self.action(order, "accept")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "accepted")
        self.api.force_authenticate(self.owner)
        events = self.api.get(path + "history/").json()["results"]
        self.assertTrue(any(e["message"] == "Уточните площадь по техпаспорту" for e in events))
        self.assertFalse(any("actor" in e for e in events))

    def test_skip_review_and_empty_explanation_rejected(self):
        order = self.submitted()
        self.assertEqual(self.action(order, "accept").status_code, 409)
        self.assertEqual(self.action(order, "reject").status_code, 400)
        self.action(order, "start_review")
        order.refresh_from_db()
        self.assertEqual(self.action(order, "request_information").status_code, 400)

    def test_reject_is_terminal_and_preserves_reason(self):
        order = self.submitted()
        response = self.action(order, "reject", "Не можем выполнить эту задачу")
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(self.action(order, "start_review").status_code, 409)
        self.assertEqual(order.events.last().message, "Не можем выполнить эту задачу")

    def test_test_orders_blocked_and_hidden_outside_local(self):
        order = self.submitted()
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            self.api.force_authenticate(self.reviewer)
            self.assertEqual(self.api.get(ROOT + "review/orders/").json()["count"], 0)
            self.assertEqual(self.api.get(ROOT + f"review/orders/{order.pk}/").status_code, 404)
            self.api.force_authenticate(self.owner)
            self.assertEqual(
                self.api.post(
                    ROOT + f"orders/{order.pk}/cancel/", {"expected_version": order.version}
                ).status_code,
                403,
            )

    def test_client_cancellation_before_work_and_stale_reviewer_rejected(self):
        order = self.submitted()
        stale_version = order.version
        response = self.api.post(
            ROOT + f"orders/{order.pk}/cancel/", {"expected_version": stale_version, "reason": "Передумал"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.action(order, "start_review").status_code, 409)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(order.events.last().message, "Передумал")

    def test_future_work_status_is_not_implicitly_cancellable(self):
        order = self.draft()
        # A future Part adding a state must explicitly define its cancellation policy.
        Order.objects.filter(pk=order["id"]).update(status="in_progress")
        response = self.api.post(ROOT + f"orders/{order['id']}/cancel/", {"expected_version": 1})
        self.assertEqual(response.status_code, 409)

    def test_new_legal_version_blocks_edit_but_not_read_or_cancel(self):
        order = self.draft()
        legal = LegalDocument.objects.get(kind="terms", language="ru", is_active=True)
        legal.is_active = False
        legal.save()
        LegalDocument.objects.create(
            kind="terms", language="ru", version="dev-2", title="Next", content="Next", is_active=True
        )
        path = ROOT + f"orders/{order['id']}/"
        self.assertEqual(self.api.get(path).status_code, 200)
        self.assertEqual(self.api.patch(path, {"expected_version": 1, "answers": {}}).status_code, 403)
        self.assertEqual(self.api.post(path + "cancel/", {"expected_version": 1}).status_code, 200)

    def test_admin_cannot_bypass_workflow_using_django_forms(self):
        admin = self.make_user("admin-orders@example.com", role="admin", is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        order = self.draft()
        for model in (Asset, Order, OrderEvent, OrderSubmission):
            self.assertIn(model, site._registry)
        response = self.client.get("/admin/orders/order/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/admin/orders/order/{order['id']}/change/", {"status": "accepted"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/admin/orders/order/add/").status_code, 403)

    def test_reviewer_contact_is_scoped_to_unlocked_order(self):
        order = self.submitted()
        own = self.api.get(ROOT + f"orders/{order.pk}/").json()
        self.assertNotIn("customer", own)
        self.api.force_authenticate(self.reviewer)
        result = self.api.get(ROOT + f"review/orders/{order.pk}/").json()
        self.assertEqual(result["customer"]["email"], self.owner.email)
        self.assertNotIn("password", result["customer"])
        self.assertEqual(self.api.get(ROOT + f"assets/{self.asset.pk}/").status_code, 404)

    def test_invalid_status_filter_and_delete_rejected(self):
        order = self.draft()
        self.assertEqual(self.api.get(ROOT + "orders/?status=paid").status_code, 400)
        self.assertEqual(self.api.delete(ROOT + f"orders/{order['id']}/").status_code, 405)


class DemoTests(OrderFixture):
    def test_demo_rolls_back_all_data(self):
        before = (User.objects.count(), Asset.objects.count(), Service.objects.count(), Order.objects.count())
        output = io.StringIO()
        call_command("demo_orders", stdout=output)
        self.assertIn("PASS", output.getvalue())
        self.assertEqual(
            before,
            (User.objects.count(), Asset.objects.count(), Service.objects.count(), Order.objects.count()),
        )
        self.assertEqual(OrderEvent.objects.count(), 0)

    @override_settings(ALLOW_ORDER_SIMULATION=False)
    def test_demo_refuses_production(self):
        with self.assertRaises(CommandError):
            call_command("demo_orders", stdout=io.StringIO())
