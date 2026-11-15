import uuid
from unittest.mock import patch

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.communications import services
from apps.communications.models import Comment, MaterialReply, MaterialRequest
from apps.materials import services as materials
from apps.materials.models import Material
from apps.notifications.models import Notification
from apps.orders import services as orders
from apps.orders.tests.test_orders import ROOT, OrderFixture


class CommunicationFixture(OrderFixture):
    def reviewing(self):
        order = self.submitted()
        return orders.review_action(self.reviewer, order.pk, order.version, "start_review")

    def requested(self, text=False):
        order = self.reviewing()
        file = order.materials.first()
        items = [
            {
                "kind": "text" if text else file.kind,
                "slot_key": "" if text else file.slot_key,
                "instruction": "Уточните сведения" if text else "Нужна читаемая копия",
                "where_to_get": "У собственника",
            }
        ]
        req, order, _ = services.request_materials(
            self.reviewer, order.pk, order.version, uuid.uuid4(), "Дополните материалы", items
        )
        return req, order, [] if text else [file.pk]

    def responded(self, text=False):
        req, order, ids = self.requested(text)
        reply, order, _ = services.respond(
            self.owner, order.pk, req.pk, order.version, uuid.uuid4(), "Ответ", ids, True
        )
        return req, reply, order, ids


class CommentTests(CommunicationFixture):
    def test_comment_is_shared_idempotent_and_has_no_author_email(self):
        order = self.reviewing()
        self.api.force_authenticate(self.owner)
        url = ROOT + f"orders/{order.pk}/comments/"
        data = {"client_request_id": str(uuid.uuid4()), "body": "Когда можно приехать?"}
        r = self.api.post(url, data)
        self.assertEqual(r.status_code, 201, r.json())
        self.assertEqual(self.api.post(url, data).status_code, 200)
        self.assertEqual(Comment.objects.count(), 1)
        self.assertEqual(
            Notification.objects.filter(kind="comment_added", recipient=self.reviewer).count(), 1
        )
        self.assertEqual(Notification.objects.filter(kind="comment_added", recipient=self.owner).count(), 0)
        self.api.force_authenticate(self.reviewer)
        response = self.api.get(ROOT + f"review/orders/{order.pk}/comments/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.owner.email, response.content.decode())
        self.assertEqual(response.json()["results"][0]["body"], data["body"])
        self.assertEqual(self.api.patch(url, {"body": "edited"}).status_code, 405)

    def test_foreign_client_cannot_read_or_write(self):
        order = self.reviewing()
        self.api.force_authenticate(self.other)
        url = ROOT + f"orders/{order.pk}/comments/"
        self.assertEqual(self.api.get(url).status_code, 404)
        self.assertEqual(
            self.api.post(url, {"body": "x", "client_request_id": str(uuid.uuid4())}).status_code, 404
        )
        self.assertEqual(self.api.get(ROOT + f"review/orders/{order.pk}/comments/").status_code, 403)

    def test_draft_cannot_comment_and_unknown_fields_rejected(self):
        order = self.draft()
        data = {"body": "x", "client_request_id": str(uuid.uuid4())}
        url = ROOT + f"orders/{order['id']}/comments/"
        self.assertEqual(self.api.post(url, data).status_code, 409)
        self.assertEqual(self.api.post(url, {**data, "author_role": "admin"}).status_code, 400)

    def test_changed_idempotent_body_conflicts(self):
        order = self.reviewing()
        key = uuid.uuid4()
        services.comment(self.owner, order.pk, key, "one")
        with self.assertRaises(orders.Conflict):
            services.comment(self.owner, order.pk, key, "two")

    def test_rollback_includes_event_and_notifications(self):
        order = self.reviewing()
        count = Notification.objects.count()
        with self.assertRaises(RuntimeError), transaction.atomic():
            services.comment(self.owner, order.pk, uuid.uuid4(), "atomic")
            raise RuntimeError("rollback")
        self.assertEqual(Comment.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), count)

    def test_comment_rate_limit(self):
        order = self.reviewing()
        self.api.force_authenticate(self.owner)
        url = ROOT + f"orders/{order.pk}/comments/"
        for _ in range(30):
            self.assertEqual(
                self.api.post(url, {"body": "x", "client_request_id": str(uuid.uuid4())}).status_code, 201
            )
        self.assertEqual(
            self.api.post(url, {"body": "x", "client_request_id": str(uuid.uuid4())}).status_code, 429
        )


class RequestTests(CommunicationFixture):
    def test_full_material_reply_review_accept_flow(self):
        req, reply, order, ids = self.responded()
        self.assertEqual(order.status, "submitted")
        self.assertTrue(Material.objects.get(pk=ids[0]).reply_links.exists())
        order = orders.review_action(self.reviewer, order.pk, order.version, "start_review")
        with self.assertRaises(orders.Conflict):
            orders.review_action(self.reviewer, order.pk, order.version, "accept")
        with self.assertRaises(orders.Conflict):
            services.decide(self.reviewer, order.pk, req.pk, order.version, "accept", "OK")
        _, order = materials.review_material(self.reviewer, order.pk, ids[0], order.version, "accepted", "OK")
        req, order = services.decide(self.reviewer, order.pk, req.pk, order.version, "accept", "Принято")
        self.assertEqual(req.state, "resolved")
        order = orders.review_action(self.reviewer, order.pk, order.version, "accept")
        self.assertEqual(order.status, "accepted")

    def test_text_only_reply_and_return_cycle(self):
        req, reply, order, ids = self.responded(text=True)
        order = orders.review_action(self.reviewer, order.pk, order.version, "start_review")
        req, order = services.decide(
            self.reviewer, order.pk, req.pk, order.version, "return", "Уточните дату"
        )
        self.assertEqual(order.status, "needs_information")
        reply2, order, _ = services.respond(
            self.owner, order.pk, req.pk, order.version, uuid.uuid4(), "Дата уточнена", [], True
        )
        self.assertNotEqual(reply.pk, reply2.pk)
        self.assertEqual(req.replies.count(), 2)

    def test_empty_text_reply_rejected(self):
        req, order, ids = self.requested(text=True)
        with self.assertRaises(ValidationError):
            services.respond(self.owner, order.pk, req.pk, order.version, uuid.uuid4(), "", [], True)

    def test_unanswered_request_blocks_bypass_submit(self):
        req, order, ids = self.requested()
        with self.assertRaises(orders.Conflict):
            orders.submit_order(self.owner, order.pk, order.version, True)

    def test_request_cancel_allows_legacy_submit(self):
        req, order, ids = self.requested()
        req, order = services.decide(self.reviewer, order.pk, req.pk, order.version, "cancel", "Не требуется")
        order = orders.submit_order(self.owner, order.pk, order.version, True)
        self.assertEqual(order.status, "submitted")

    def test_order_cancel_closes_request(self):
        req, order, ids = self.requested()
        order = orders.cancel_order(self.owner, order.pk, order.version)
        req.refresh_from_db()
        self.assertEqual(req.state, "cancelled")
        with self.assertRaises(orders.Conflict):
            services.respond(self.owner, order.pk, req.pk, order.version, uuid.uuid4(), "x", ids, True)

    def test_reply_idempotent_but_changed_files_conflict(self):
        req, reply, order, ids = self.responded()
        repeat, current, created = services.respond(
            self.owner, order.pk, req.pk, 1, reply.client_request_id, "Ответ", ids, True
        )
        self.assertFalse(created)
        self.assertEqual(reply.pk, repeat.pk)
        with self.assertRaises(orders.Conflict):
            services.respond(
                self.owner, order.pk, req.pk, order.version, reply.client_request_id, "changed", ids, True
            )

    def test_reply_failure_rolls_back_pins_state_notifications(self):
        req, order, ids = self.requested()
        before = Notification.objects.count()
        with (
            patch("apps.materials.services.require_materials", side_effect=ValidationError("Missing")),
            self.assertRaises(ValidationError),
        ):
            services.respond(self.owner, order.pk, req.pk, order.version, uuid.uuid4(), "Ответ", ids, True)
        req.refresh_from_db()
        self.assertEqual(req.state, "open")
        self.assertEqual(MaterialReply.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), before)

    def test_foreign_missing_withdrawn_and_wrong_slot_files_rejected(self):
        req, order, ids = self.requested()
        with self.assertRaises(ValidationError):
            services.respond(
                self.owner, order.pk, req.pk, order.version, uuid.uuid4(), "x", [uuid.uuid4()], True
            )
        Material.objects.filter(pk=ids[0]).update(withdrawn_at=timezone.now())
        with self.assertRaises(ValidationError):
            services.respond(self.owner, order.pk, req.pk, order.version, uuid.uuid4(), "x", ids, True)

    def test_invalid_slot_request_and_stale_decision(self):
        order = self.reviewing()
        with self.assertRaises(ValidationError):
            services.request_materials(
                self.reviewer,
                order.pk,
                order.version,
                uuid.uuid4(),
                "x",
                [{"kind": "document", "slot_key": "unknown", "instruction": "x", "where_to_get": ""}],
            )
        req, order, ids = self.requested(text=True)
        with self.assertRaises(orders.Conflict):
            services.decide(self.reviewer, order.pk, req.pk, 1, "cancel", "x")

    def test_request_api_roles_lists_and_strict_inputs(self):
        req, order, ids = self.requested()
        self.api.force_authenticate(self.owner)
        url = ROOT + f"orders/{order.pk}/material-requests/"
        self.assertEqual(self.api.get(url).json()["count"], 1)
        self.assertEqual(self.api.post(url, {}).status_code, 405)
        self.assertEqual(
            self.api.post(ROOT + f"review/orders/{order.pk}/material-requests/", {}).status_code, 403
        )
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(url).status_code, 404)
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(ROOT + "review/material-requests/").json()["count"], 1)

    def test_request_idempotency_and_one_active(self):
        req, order, ids = self.requested()
        existing, _, created = services.request_materials(
            self.reviewer, order.pk, 1, req.client_request_id, req.message, req.items
        )
        self.assertFalse(created)
        self.assertEqual(existing.pk, req.pk)
        with self.assertRaises(orders.Conflict):
            services.request_materials(self.reviewer, order.pk, order.version, uuid.uuid4(), "x", req.items)
        self.assertEqual(MaterialRequest.objects.count(), 1)


class DemoTests(CommunicationFixture):
    def test_demo_is_rollback_only(self):
        import io

        from django.core.management import call_command

        before = Notification.objects.count()
        out = io.StringIO()
        call_command("demo_communications", stdout=out)
        self.assertIn("PASS: comment", out.getvalue())
        self.assertEqual(Notification.objects.count(), before)
        self.assertEqual(Comment.objects.count(), 0)
