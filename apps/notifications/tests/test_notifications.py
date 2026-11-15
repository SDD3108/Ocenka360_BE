import io
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.db import transaction
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import AuthSession, User
from apps.communications import services as comms
from apps.communications.tests.test_communications import CommunicationFixture
from apps.notifications import provider, services, worker
from apps.notifications.messages import body
from apps.notifications.models import Notification, PushDelivery
from apps.orders import services as orders
from apps.orders.tests.test_orders import ROOT

PROJECT = "11111111-1111-4111-8111-111111111111"
PUSH = dict(EXPO_PUSH_ENABLED=True, EXPO_PROJECT_ID=PROJECT, EXPO_ACCESS_TOKEN="unit-test-not-real")
TOKEN = "ExpoPushToken[unit_test_token_1234567890]"


@override_settings(**PUSH)
class NotificationTests(CommunicationFixture):
    def device(self, user=None, **extra):
        user = user or self.reviewer
        session = AuthSession.objects.create(
            user=user,
            refresh_digest="synthetic",
            last_used_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        return services.register_device(
            user, session.pk, uuid.uuid4(), extra.get("token", TOKEN), "android", uuid.UUID(PROJECT)
        )

    def pending(self):
        order = self.reviewing()
        device = self.device()
        comms.comment(self.owner, order.pk, uuid.uuid4(), "SECRET address document IIN")
        return PushDelivery.objects.get(device=device), device, order

    def test_inbox_scope_read_count_and_role_demotion(self):
        row, device, order = self.pending()
        self.api.force_authenticate(self.reviewer)
        r = self.api.get(ROOT + "notifications/?unread=true")
        self.assertEqual(r.status_code, 200, r.json())
        self.assertNotIn("SECRET", r.content.decode())
        nid = row.notification_id
        count = self.api.get(ROOT + "notifications/unread-count/").json()["unread"]
        response = self.api.post(ROOT + f"notifications/{nid}/read/", {})
        self.assertEqual(response.status_code, 200)
        stamp = response.json()["read_at"]
        self.assertEqual(self.api.post(ROOT + f"notifications/{nid}/read/", {}).json()["read_at"], stamp)
        self.assertEqual(self.api.get(ROOT + "notifications/unread-count/").json()["unread"], count - 1)
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(ROOT + f"notifications/{nid}/").status_code, 404)
        self.assertEqual(self.api.post(ROOT + f"notifications/{nid}/read/", {}).status_code, 404)
        User.objects.filter(pk=self.reviewer.pk).update(role="client")
        self.reviewer.refresh_from_db()
        self.assertFalse(services.visible(self.reviewer).exists())

    def test_event_idempotency_and_no_old_backfill(self):
        order = self.reviewing()
        before = Notification.objects.count()
        services.notify_event(order.events.get(action="start_review"))
        self.assertEqual(Notification.objects.count(), before)
        self.assertFalse(Notification.objects.filter(event__action="created").exists())

    def test_device_api_requires_jwt_session_and_never_returns_token(self):
        self.api.force_authenticate(self.reviewer)
        payload = {
            "installation_id": str(uuid.uuid4()),
            "token": TOKEN,
            "platform": "ios",
            "project_id": PROJECT,
        }
        self.assertEqual(self.api.post(ROOT + "notifications/devices/", payload).status_code, 403)
        device = self.device()
        self.api.force_authenticate(self.reviewer, token={"sid": str(device.session_id)})
        payload["installation_id"] = str(device.installation_id)
        r = self.api.post(ROOT + "notifications/devices/", payload)
        self.assertEqual(r.status_code, 200, r.json())
        self.assertNotIn(TOKEN, r.content.decode())
        self.assertNotIn(TOKEN, self.api.get(ROOT + "notifications/devices/").content.decode())
        self.assertEqual(
            self.api.post(
                ROOT + "notifications/devices/", {**payload, "project_id": str(uuid.uuid4())}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.api.post(ROOT + "notifications/devices/", {**payload, "token": "not-token"}).status_code, 400
        )

    def test_disabled_push_preserves_inbox_and_queue(self):
        row, device, order = self.pending()
        with override_settings(EXPO_PUSH_ENABLED=False), patch("apps.notifications.provider.send") as send:
            self.assertTrue(worker.send_pending()["disabled"])
            send.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.state, "queued")
        self.assertTrue(Notification.objects.filter(pk=row.notification_id).exists())

    def test_success_ticket_then_receipt_is_not_a_read_receipt(self):
        row, device, order = self.pending()
        with patch(
            "apps.notifications.provider.send", return_value={"status": "ok", "id": "ticket-1"}
        ) as send:
            self.assertEqual(worker.send_pending()["processed"], 1)
        payload = send.call_args.args[0]
        self.assertNotIn("SECRET", str(payload))
        self.assertEqual(set(payload["data"]), {"notification_id"})
        row.refresh_from_db()
        self.assertEqual(row.state, "ticket")
        PushDelivery.objects.filter(pk=row.pk).update(receipt_due_at=timezone.now())
        with patch("apps.notifications.provider.receipts", return_value={"ticket-1": {"status": "ok"}}):
            worker.check_receipts()
        row.refresh_from_db()
        self.assertEqual(row.state, "accepted")
        self.assertIsNone(row.notification.read_at)

    def test_ticket_error_deactivates_token(self):
        row, device, order = self.pending()
        with patch(
            "apps.notifications.provider.send",
            return_value={"status": "error", "details": {"error": "DeviceNotRegistered"}},
        ):
            worker.send_pending()
        device.refresh_from_db()
        self.assertFalse(device.active)
        row.refresh_from_db()
        self.assertEqual(row.error_code, "DeviceNotRegistered")

    def test_timeout_is_unknown_and_never_auto_resent(self):
        row, device, order = self.pending()
        with patch(
            "apps.notifications.provider.send",
            side_effect=provider.PushError("network_unknown", uncertain=True),
        ) as send:
            worker.send_pending()
            worker.send_pending()
            self.assertEqual(send.call_count, 1)
        row.refresh_from_db()
        self.assertEqual(row.state, "unknown")

    def test_transient_429_uses_backoff_and_attempt_cap(self):
        row, device, order = self.pending()
        with patch(
            "apps.notifications.provider.send", side_effect=provider.PushError("http_429", retryable=True)
        ) as send:
            worker.send_pending()
            worker.send_pending()
            self.assertEqual(send.call_count, 1)
            for _ in range(4):
                PushDelivery.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now())
                worker.send_pending()
        row.refresh_from_db()
        self.assertEqual(row.attempts, 5)
        self.assertEqual(row.state, "failed")

    def test_session_revocation_read_and_role_changes_skip(self):
        row, device, order = self.pending()
        AuthSession.objects.filter(pk=device.session_id).update(revoked_at=timezone.now())
        with patch("apps.notifications.provider.send") as send:
            worker.send_pending()
            send.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.state, "skipped")

    def test_read_before_dispatch_skips(self):
        row, device, order = self.pending()
        Notification.objects.filter(pk=row.notification_id).update(read_at=timezone.now())
        with patch("apps.notifications.provider.send") as send:
            worker.send_pending()
            send.assert_not_called()

    def test_expired_queue_and_interrupted_worker_are_terminal(self):
        row, device, order = self.pending()
        PushDelivery.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(days=2))
        with patch("apps.notifications.provider.send") as send:
            worker.send_pending()
            send.assert_not_called()
        PushDelivery.objects.filter(pk=row.pk).update(
            state="sending", claimed_at=timezone.now() - timedelta(minutes=10)
        )
        worker.send_pending()
        row.refresh_from_db()
        self.assertEqual(row.state, "unknown")

    def test_rotation_skips_old_jobs_and_stale_receipt_cannot_disable_new_token(self):
        row, device, order = self.pending()
        services.register_device(
            self.reviewer,
            device.session_id,
            device.installation_id,
            "ExpoPushToken[new_token_1234567890]",
            "android",
            uuid.UUID(PROJECT),
        )
        with patch("apps.notifications.provider.send") as send:
            worker.send_pending()
            send.assert_not_called()
        PushDelivery.objects.filter(pk=row.pk).update(
            state="ticket", ticket_id="old", receipt_due_at=timezone.now(), claimed_at=timezone.now()
        )
        with patch(
            "apps.notifications.provider.receipts",
            return_value={"old": {"status": "error", "details": {"error": "DeviceNotRegistered"}}},
        ):
            worker.check_receipts()
        device.refresh_from_db()
        self.assertTrue(device.active)

    def test_missing_receipt_expires_without_resending(self):
        row, device, order = self.pending()
        PushDelivery.objects.filter(pk=row.pk).update(
            state="ticket",
            ticket_id="missing",
            receipt_due_at=timezone.now(),
            claimed_at=timezone.now() - timedelta(hours=24),
        )
        with patch("apps.notifications.provider.receipts", return_value={}):
            worker.check_receipts()
        row.refresh_from_db()
        self.assertEqual(row.state, "unknown")

    def test_other_account_cannot_steal_active_device(self):
        device = self.device()
        other = AuthSession.objects.create(
            user=self.other,
            refresh_digest="test",
            last_used_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        with self.assertRaises(orders.Conflict):
            services.register_device(
                self.other, other.pk, device.installation_id, device.token, "android", uuid.UUID(PROJECT)
            )
        AuthSession.objects.filter(pk=device.session_id).update(revoked_at=timezone.now())
        rebound = services.register_device(
            self.other, other.pk, device.installation_id, device.token, "android", uuid.UUID(PROJECT)
        )
        self.assertEqual(rebound.user_id, self.other.pk)
        self.assertGreater(rebound.revision, device.revision)

    def test_foreign_device_delete_and_test_order_hidden(self):
        row, device, order = self.pending()
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.delete(ROOT + f"notifications/devices/{device.pk}/").status_code, 404)
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            self.assertFalse(services.visible(self.reviewer).exists())

    def test_transaction_rolls_back_outbox(self):
        order = self.reviewing()
        device = self.device()
        with self.assertRaises(RuntimeError), transaction.atomic():
            comms.comment(self.owner, order.pk, uuid.uuid4(), "test")
            raise RuntimeError()
        self.assertEqual(PushDelivery.objects.filter(device=device).count(), 0)

    def test_management_commands_disabled_without_network(self):
        with (
            override_settings(EXPO_PUSH_ENABLED=False),
            patch("apps.notifications.provider.request_json") as send,
        ):
            for name in ["dispatch_notifications", "check_push_receipts"]:
                out = io.StringIO()
                call_command(name, stdout=out)
                self.assertIn("disabled", out.getvalue())
            send.assert_not_called()


@override_settings(**PUSH)
class TransportTests(SimpleTestCase):
    def test_translations_have_three_distinct_languages(self):
        self.assertEqual(len({body("materials_requested", x) for x in ["ru", "kk", "en"]}), 3)

    def test_send_contract_fixed_host_auth_and_timeout(self):
        with patch("apps.notifications.provider.build_opener") as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"data":[{"status":"ok","id":"ticket"}]}'
            self.assertEqual(provider.send({"to": TOKEN})["id"], "ticket")
            args, kwargs = opener.return_value.open.call_args
            self.assertEqual(args[0].full_url, "https://exp.host/--/api/v2/push/send")
            self.assertEqual(args[0].headers["Authorization"], "Bearer unit-test-not-real")
            self.assertEqual(kwargs["timeout"], 10)

    def test_malformed_provider_response_does_not_become_success(self):
        with (
            patch("apps.notifications.provider.request_json", return_value={"data": {}}),
            self.assertRaises(provider.PushError),
        ):
            provider.send({"to": TOKEN})
