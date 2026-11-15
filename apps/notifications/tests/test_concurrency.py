import uuid
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import AuthSession
from apps.communications import services as communications
from apps.communications.tests import test_concurrency as comm_concurrency
from apps.notifications import services, worker
from apps.notifications.models import DeviceLock, PushDelivery
from apps.notifications.tests.test_notifications import PROJECT, PUSH, TOKEN


@skipUnless(connection.vendor == "postgresql", "Push worker concurrency requires PostgreSQL")
@override_settings(**PUSH)
class PushConcurrencyTests(TransactionTestCase):
    user = comm_concurrency.CommunicationConcurrencyTests.user
    parallel = comm_concurrency.CommunicationConcurrencyTests.parallel

    def setUp(self):
        comm_concurrency.CommunicationConcurrencyTests.setUp(self)
        DeviceLock.objects.get_or_create(pk=1)
        session = AuthSession.objects.create(
            user=self.reviewer,
            refresh_digest="test",
            last_used_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.device = services.register_device(
            self.reviewer, session.pk, uuid.uuid4(), TOKEN, "ios", uuid.UUID(PROJECT)
        )
        communications.comment(self.owner, self.order.pk, uuid.uuid4(), "new")

    def test_two_workers_send_one_delivery_once(self):
        with patch("apps.notifications.provider.send", return_value={"status": "ok", "id": "ticket"}) as send:
            self.parallel([lambda: worker.send_pending(1), lambda: worker.send_pending(1)])
            self.assertEqual(send.call_count, 1)
        self.assertEqual(PushDelivery.objects.get().state, "ticket")
