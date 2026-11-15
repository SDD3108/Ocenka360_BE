import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import skipUnless

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.catalog.models import Service
from apps.legal.models import LegalDocument
from apps.legal.services import accept_documents
from apps.orders import services
from apps.orders.models import Asset, Order

from .test_orders import valid_answers


@skipUnless(connection.vendor == "postgresql", "Row locking tests require PostgreSQL")
class OrderConcurrencyTests(TransactionTestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="o360-parallel-tests-")
        self.addCleanup(folder.cleanup)
        override = override_settings(MATERIAL_STORAGE_ROOT=folder.name)
        override.enable()
        self.addCleanup(override.disable)
        call_command("seed_legal_drafts", stdout=io.StringIO())
        call_command("seed_catalog", stdout=io.StringIO())
        self.owner = User.objects.create_user("parallel-order@example.com")
        self.owner.email_verified_at = timezone.now()
        self.owner.save()
        self.owner = accept_documents(
            self.owner,
            list(LegalDocument.objects.filter(language="ru", is_active=True).values_list("id", flat=True)),
            True,
        )
        self.asset = Asset.objects.create(owner=self.owner, title="TEST", kind="apartment", city="Астана")
        service = Service.objects.get(slug="apartment")
        self.data = {
            "client_request_id": uuid.uuid4(),
            "asset_id": self.asset.pk,
            "service_id": service.pk,
            "form_id": service.current_form_id,
            "language": "ru",
            "answers": valid_answers(),
        }

    def run_parallel(self, func):
        barrier = Barrier(2)

        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return func()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(lambda _: run(), range(2)))

    def test_duplicate_create_has_single_order(self):
        results = self.run_parallel(lambda: services.create_order(self.owner, self.data)[1])
        self.assertCountEqual(results, [True, False])
        self.assertEqual(Order.objects.count(), 1)

    def test_double_submit_has_single_snapshot(self):
        order, _ = services.create_order(self.owner, self.data)
        from apps.materials.tests.fixtures import seed_test_materials

        seed_test_materials(order)

        def submit():
            try:
                services.submit_order(self.owner, order.pk, 1, True)
                return "ok"
            except services.Conflict:
                return "conflict"

        self.assertCountEqual(self.run_parallel(submit), ["ok", "conflict"])
        self.assertEqual(order.submissions.count(), 1)
        self.assertEqual(order.events.count(), 2)

    def test_parallel_asset_edit_rejects_stale_write(self):
        def edit():
            try:
                services.update_asset(self.owner, self.asset.pk, {"expected_version": 1, "title": "Edited"})
                return "ok"
            except services.Conflict:
                return "conflict"

        self.assertCountEqual(self.run_parallel(edit), ["ok", "conflict"])
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.version, 2)

    def test_identical_parallel_material_uploads_create_one_record(self):
        from apps.materials.services import upload
        from apps.materials.tests.fixtures import pdf_upload

        order, _ = services.create_order(self.owner, self.data)
        upload_id = uuid.uuid4()

        def send():
            return upload(
                self.owner,
                order.pk,
                {
                    "expected_version": 1,
                    "client_upload_id": upload_id,
                    "kind": "document",
                    "slot_key": "additional",
                    "file": pdf_upload(),
                },
            )[2]

        self.assertCountEqual(self.run_parallel(send), [True, False])
        self.assertEqual(order.materials.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.version, 2)
        self.assertEqual(order.events.count(), 2)

    def test_parallel_different_uploads_reject_stale_version(self):
        from apps.materials.services import upload
        from apps.materials.tests.fixtures import pdf_upload

        order, _ = services.create_order(self.owner, self.data)

        def send():
            try:
                upload(
                    self.owner,
                    order.pk,
                    {
                        "expected_version": 1,
                        "client_upload_id": uuid.uuid4(),
                        "kind": "document",
                        "slot_key": "additional",
                        "file": pdf_upload(),
                    },
                )
                return "ok"
            except services.Conflict:
                return "conflict"

        self.assertCountEqual(self.run_parallel(send), ["ok", "conflict"])
        self.assertEqual(order.materials.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.version, 2)
