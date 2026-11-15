import uuid
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase

from apps.communications import services
from apps.communications.models import Comment, MaterialRequest
from apps.materials.tests.fixtures import seed_test_materials
from apps.notifications.models import Notification
from apps.orders import services as orders
from apps.pricing.tests import test_concurrency as base


@skipUnless(connection.vendor == "postgresql", "Communication concurrency requires PostgreSQL")
class CommunicationConcurrencyTests(TransactionTestCase):
    user = base.PricingConcurrencyTests.user
    parallel = base.PricingConcurrencyTests.parallel

    def setUp(self):
        base.PricingConcurrencyTests.setUp(self)
        self.second = self.user("second-reviewer@example.com", "appraiser")
        self.order, _ = orders.create_order(self.owner, {**self.data, "client_request_id": uuid.uuid4()})
        seed_test_materials(self.order)
        self.order = orders.submit_order(self.owner, self.order.pk, self.order.version, True)
        self.order = orders.simulate_payment(self.order.pk)
        self.order = orders.review_action(self.reviewer, self.order.pk, self.order.version, "start_review")

    def test_same_comment_only_one_row_and_notification_per_recipient(self):
        key = uuid.uuid4()
        results = self.parallel(
            [lambda: services.comment(self.owner, self.order.pk, key, "same")[0].pk for _ in range(2)]
        )
        self.assertEqual(results[0], results[1])
        self.assertEqual(Comment.objects.count(), 1)
        self.assertEqual(
            Notification.objects.filter(recipient=self.reviewer, kind="comment_added").count(), 1
        )

    def test_competing_reviewers_create_one_active_request(self):
        def create(actor):
            try:
                services.request_materials(
                    actor,
                    self.order.pk,
                    self.order.version,
                    uuid.uuid4(),
                    "Explain",
                    [{"kind": "text", "slot_key": "", "instruction": "Explain", "where_to_get": ""}],
                )
                return "created"
            except orders.Conflict:
                return "conflict"

        result = self.parallel([lambda: create(self.reviewer), lambda: create(self.second)])
        self.assertCountEqual(result, ["created", "conflict"])
        self.assertEqual(MaterialRequest.objects.count(), 1)

    def test_client_and_reviewer_cross_notifications_do_not_deadlock(self):
        from threading import Barrier

        barrier = Barrier(2)
        original = orders.actor_for_write

        def locked_actor(*args, **kwargs):
            actor = original(*args, **kwargs)
            barrier.wait(timeout=10)
            return actor

        from unittest.mock import patch

        with patch("apps.orders.services.actor_for_write", side_effect=locked_actor):
            self.parallel(
                [
                    lambda: services.comment(self.owner, self.order.pk, uuid.uuid4(), "client"),
                    lambda: services.comment(
                        self.reviewer, self.order.pk, uuid.uuid4(), "reviewer", review=True
                    ),
                ]
            )
        self.assertEqual(Comment.objects.count(), 2)
