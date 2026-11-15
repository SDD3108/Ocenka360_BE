import hashlib
import io
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, TextStringObject

from apps.materials.models import Material, MaterialManifest
from apps.materials.services import checklist
from apps.materials.storage import PrivateStorage
from apps.orders.models import Order
from apps.orders.services import review_action, simulate_payment
from apps.orders.tests.test_orders import ROOT, OrderFixture, valid_answers

from .fixtures import pdf_bytes, pdf_upload, photo_upload


class MaterialTests(OrderFixture):
    def setUp(self):
        super().setUp()
        self.order = Order.objects.get(pk=self.draft()["id"])
        self.path = ROOT + f"orders/{self.order.pk}/materials/"
        self.review_path = ROOT + f"review/orders/{self.order.pk}/materials/"

    def upload(self, file=None, **extra):
        self.order.refresh_from_db()
        return self.api.post(
            self.path,
            {
                "expected_version": self.order.version,
                "client_upload_id": str(uuid.uuid4()),
                "kind": "document",
                "slot_key": "additional",
                "file": file if file is not None else pdf_upload(),
                **extra,
            },
            format="multipart",
        )

    def fill_required(self):
        for missing in checklist(self.order)["missing"]:
            response = self.upload(
                file=photo_upload() if missing["kind"] == "photo" else pdf_upload(), **missing
            )
            self.assertEqual(response.status_code, 201, response.json())
        self.order.refresh_from_db()

    def submit(self):
        self.order.refresh_from_db()
        return self.api.post(
            ROOT + f"orders/{self.order.pk}/submit/",
            {
                "expected_version": self.order.version,
                "confirmed": True,
            },
        )

    def under_review(self):
        self.fill_required()
        self.assertEqual(self.submit().status_code, 200)
        order = simulate_payment(self.order.pk)
        self.order = review_action(self.reviewer, order.pk, order.version, "start_review")
        self.api.force_authenticate(self.reviewer)

    def test_pdf_upload_private_download_and_no_path_leak(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()
        self.assertEqual(data["order_version"], 2)
        self.assertEqual(data["material"]["scan_status"], "local_unscanned")
        self.assertNotIn("storage_key", data["material"])
        self.assertNotIn("uploaded_by", data["material"])
        file = Material.objects.get()
        downloaded = self.api.get(self.path + f"{file.pk}/download/")
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(b"".join(downloaded.streaming_content), pdf_bytes())
        self.assertIn("attachment;", downloaded["Content-Disposition"])
        self.assertIn("no-store", downloaded["Cache-Control"])
        self.assertEqual(downloaded["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.api.get("/media/" + file.storage_key).status_code, 404)
        with self.assertRaises(NotImplementedError):
            PrivateStorage().url(file.storage_key)

    def test_owner_and_reviewer_boundaries(self):
        material = self.upload().json()["material"]
        self.api.force_authenticate(self.other)
        for suffix in ("", "requirements/", f"{material['id']}/download/"):
            self.assertEqual(self.api.get(self.path + suffix).status_code, 404)
        with patch("apps.materials.services.validated_file") as validator:
            self.assertEqual(self.upload().status_code, 404)
            validator.assert_not_called()
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.get(self.review_path).status_code, 403)
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(self.review_path).status_code, 404)
        self.api.force_authenticate(None)
        self.assertEqual(self.api.get(self.path).status_code, 401)

    def test_idempotent_upload_retry_ignores_stale_version(self):
        key = str(uuid.uuid4())
        first = self.upload(client_upload_id=key)
        again = self.upload(client_upload_id=key, expected_version=1)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json(), again.json())
        self.assertEqual(Material.objects.count(), 1)
        self.assertEqual(self.order.events.count(), 2)
        other = self.upload(client_upload_id=key, file=photo_upload())
        self.assertEqual(other.status_code, 409)

    def test_stale_upload_rejected_before_parser(self):
        self.upload()
        with patch("apps.materials.services.validated_file") as validator:
            self.assertEqual(self.upload(expected_version=1).status_code, 409)
            validator.assert_not_called()

    def test_no_ownership_review_or_path_injection(self):
        for extra in (
            {"owner": str(self.other.pk)},
            {"source": "appraiser"},
            {"review_status": "accepted"},
            {"storage_key": "../../file"},
            {"slot_key": "../../passport"},
            {"slot_key": "unknown"},
        ):
            self.assertEqual(self.upload(**extra).status_code, 400)
        self.assertEqual(Material.objects.count(), 0)

    def test_bad_extension_bad_bytes_and_pdf_as_photo(self):
        for upload in (
            SimpleUploadedFile("x.svg", b"<svg/>"),
            SimpleUploadedFile("x.pdf", b"not pdf"),
            SimpleUploadedFile("x.jpg", pdf_bytes()),
            SimpleUploadedFile("x.pdf", b""),
        ):
            self.assertEqual(self.upload(file=upload).status_code, 400)
        self.assertEqual(self.upload(kind="photo").status_code, 400)
        self.assertEqual(Material.objects.count(), 0)

    def test_images_strip_metadata_and_orientation_is_applied(self):
        image = Image.new("RGB", (20, 10), "blue")
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "Sensitive location"
        stream = io.BytesIO()
        image.save(stream, format="JPEG", exif=exif)
        response = self.upload(file=SimpleUploadedFile("location.jpg", stream.getvalue()), kind="photo")
        self.assertEqual(response.status_code, 201, response.json())
        material = Material.objects.get()
        self.assertEqual((material.width, material.height), (10, 20))
        with PrivateStorage().open(material.storage_key) as stored, Image.open(stored) as clean:
            self.assertFalse(clean.getexif())
        self.assertNotEqual(material.sha256, hashlib.sha256(stream.getvalue()).hexdigest())

    def test_animated_png_and_active_or_encrypted_pdf_rejected(self):
        stream = io.BytesIO()
        Image.new("RGB", (5, 5), "red").save(
            stream,
            format="PNG",
            save_all=True,
            append_images=[Image.new("RGB", (5, 5), "blue")],
            duration=100,
            loop=0,
        )
        self.assertEqual(self.upload(file=SimpleUploadedFile("x.png", stream.getvalue())).status_code, 400)
        for variant in ("javascript", "password", "attachment", "pages"):
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            if variant == "javascript":
                writer.root_object[NameObject("/OpenAction")] = DictionaryObject(
                    {
                        NameObject("/S"): NameObject("/JavaScript"),
                        NameObject("/JS"): TextStringObject("app.alert('test');"),
                    }
                )
            elif variant == "password":
                writer.encrypt("test")
            elif variant == "attachment":
                writer.add_attachment("test.txt", b"test")
            else:
                for _ in range(100):
                    writer.add_blank_page(width=100, height=100)
            out = io.BytesIO()
            writer.write(out)
            self.assertEqual(self.upload(file=SimpleUploadedFile("x.pdf", out.getvalue())).status_code, 400)

    def test_streaming_file_limit_and_multiple_files(self):
        with override_settings(MATERIAL_MAX_FILE_BYTES=50):
            self.assertEqual(self.upload().status_code, 413)
        response = self.upload(file=[pdf_upload(), pdf_upload()])
        self.assertEqual(response.status_code, 413)
        self.assertFalse(Material.objects.exists())

    def test_duplicate_metadata_rejected(self):
        response = self.upload(kind=["document", "photo"])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Material.objects.exists())

    def test_order_quota_and_rate_limit(self):
        with override_settings(MATERIAL_MAX_ORDER_FILES=1):
            self.assertEqual(self.upload().status_code, 201)
            self.assertEqual(self.upload().status_code, 409)
        with override_settings(MATERIAL_MAX_ORDER_BYTES=1):
            self.assertEqual(self.upload().status_code, 409)
        from rest_framework.exceptions import Throttled

        from apps.accounts.security import reserve_quota

        with patch("apps.materials.services.reserve_quota", side_effect=Throttled(wait=5)):
            self.assertEqual(self.upload().status_code, 429)
        self.assertTrue(callable(reserve_quota))

    def test_required_documents_block_submit_atomically(self):
        response = self.submit()
        self.assertEqual(response.status_code, 400)
        self.assertIn("materials", response.json()["error"]["details"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "draft")
        self.assertEqual(self.order.version, 1)
        self.assertEqual(self.order.submissions.count(), 0)
        self.fill_required()
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.upload().status_code, 409)

    def test_remote_photo_guide_is_localized_and_cannot_be_bypassed_by_asset_kind(self):
        Order.objects.filter(pk=self.order.pk).update(
            answers={**valid_answers(), "inspection_preference": "remote"}, asset_snapshot={"kind": "other"}
        )
        self.order.refresh_from_db()
        for lang in ("ru", "kk", "en"):
            data = self.api.get(self.path + f"requirements/?language={lang}").json()
            self.assertEqual(data["language"], lang)
            self.assertTrue(any(x["kind"] == "photo" for x in data["missing"]))
            self.assertEqual(data["guide_version"], 1)
        self.fill_required()
        self.assertEqual(self.submit().status_code, 200)

    def test_manifest_is_pinned_and_historical_review_status_does_not_change(self):
        self.under_review()
        manifest = MaterialManifest.objects.get()
        original = manifest.snapshot
        material = Material.objects.first()
        response = self.api.post(
            self.review_path + f"{material.pk}/review/",
            {
                "expected_version": self.order.version,
                "decision": "rejected",
                "comment": "Загрузите все страницы",
            },
        )
        self.assertEqual(response.status_code, 200, response.json())
        manifest.refresh_from_db()
        self.assertEqual(manifest.snapshot, original)
        self.assertEqual(Material.objects.get(pk=material.pk).review_status, "rejected")
        self.assertTrue(material.submission_links.exists())

    def test_request_replacement_resubmit_and_old_file_retained(self):
        self.under_review()
        material = Material.objects.filter(slot_key="owner_identity").first() or Material.objects.first()
        response = self.api.post(
            self.review_path + f"{material.pk}/review/",
            {
                "expected_version": self.order.version,
                "decision": "rejected",
                "comment": "Нужна читаемая копия",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.order = review_action(
            self.reviewer, self.order.pk, self.order.version, "request_information", "Замените документ"
        )
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.submit().status_code, 400)
        withdrawal = self.api.post(
            self.path + f"{material.pk}/withdraw/", {"expected_version": self.order.version}
        )
        self.assertEqual(withdrawal.status_code, 409)
        self.assertEqual(withdrawal.json()["error"]["code"], "material_pinned")
        self.assertEqual(self.upload(slot_key=material.slot_key).status_code, 201)
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(MaterialManifest.objects.count(), 2)
        self.assertTrue(PrivateStorage().exists(material.storage_key))

    def test_rejection_requires_reason_and_client_cannot_review(self):
        self.under_review()
        material = Material.objects.first()
        route = self.review_path + f"{material.pk}/review/"
        self.assertEqual(
            self.api.post(
                route, {"expected_version": self.order.version, "decision": "rejected"}
            ).status_code,
            400,
        )
        self.api.force_authenticate(self.owner)
        self.assertEqual(
            self.api.post(
                route, {"expected_version": self.order.version, "decision": "accepted"}
            ).status_code,
            403,
        )

    def test_appraiser_uploads_inspection_photo_with_server_source(self):
        self.under_review()
        response = self.api.post(
            self.review_path,
            {
                "expected_version": self.order.version,
                "client_upload_id": str(uuid.uuid4()),
                "kind": "photo",
                "slot_key": "additional",
                "file": photo_upload(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["material"]["source"], "appraiser")
        self.order.refresh_from_db()
        document = self.api.post(
            self.review_path,
            {
                "expected_version": self.order.version,
                "client_upload_id": str(uuid.uuid4()),
                "kind": "document",
                "slot_key": "additional",
                "file": pdf_upload(),
            },
            format="multipart",
        )
        self.assertEqual(document.status_code, 400)
        self.api.force_authenticate(self.owner)
        downloaded = self.api.get(self.path + response.json()["material"]["id"] + "/download/")
        self.assertEqual(downloaded.status_code, 200)
        downloaded.close()

    def test_withdraw_unconfirmed_file_and_idempotency_cannot_resurrect_it(self):
        key = str(uuid.uuid4())
        data = self.upload(client_upload_id=key).json()
        material = Material.objects.get()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(
                self.path + f"{material.pk}/withdraw/", {"expected_version": data["order_version"]}
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PrivateStorage().exists(material.storage_key))
        self.assertEqual(self.api.get(self.path + f"{material.pk}/download/").status_code, 404)
        self.assertEqual(self.upload(client_upload_id=key).status_code, 409)

    def test_database_failure_removes_new_file_and_rolls_back_event(self):
        with patch("apps.materials.services.orders.changed", side_effect=RuntimeError("test")):
            self.assertEqual(self.upload().status_code, 500)
        self.assertFalse(Material.objects.exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.version, 1)
        self.assertFalse(any(p.is_file() for p in Path(PrivateStorage().location).rglob("*")))

    @override_settings(ALLOW_UNSCANNED_UPLOADS=False, MATERIAL_CLAMSCAN_PATH="")
    def test_missing_production_scanner_fails_closed(self):
        response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "scanner_unavailable")
        self.assertFalse(Material.objects.exists())

    @override_settings(ALLOW_UNSCANNED_UPLOADS=False, MATERIAL_CLAMSCAN_PATH="/test/clamscan")
    def test_scanner_infected_unavailable_and_timeout(self):
        for outcome, expected in (
            (subprocess.CompletedProcess([], 1), 400),
            (subprocess.CompletedProcess([], 2), 503),
            (subprocess.TimeoutExpired("clamscan", 30), 503),
        ):
            kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with patch("apps.materials.validation.subprocess.run", **kwargs):
                self.assertEqual(self.upload().status_code, expected)
        self.assertFalse(Material.objects.exists())

    @override_settings(ALLOW_UNSCANNED_UPLOADS=False, MATERIAL_CLAMSCAN_PATH="/test/clamscan")
    def test_clean_scanner_result_and_actual_parser(self):
        original = subprocess.run

        def run(args, **kwargs):
            if args[0] == "/test/clamscan":
                return subprocess.CompletedProcess(args, 0)
            return original(args, **kwargs)

        with patch("apps.materials.validation.subprocess.run", side_effect=run):
            response = self.upload()
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(response.json()["material"]["scan_status"], "clean")

    def test_local_unscanned_not_downloadable_or_complete_in_production(self):
        self.fill_required()
        material = Material.objects.first()
        with override_settings(ALLOW_UNSCANNED_UPLOADS=False):
            self.assertFalse(checklist(self.order)["complete"])
            self.assertEqual(self.api.get(self.path + f"{material.pk}/download/").status_code, 404)

    def test_missing_disk_file_no_false_completeness(self):
        self.fill_required()
        material = Material.objects.first()
        PrivateStorage().delete(material.storage_key)
        self.assertFalse(checklist(self.order)["complete"])
        self.assertEqual(self.api.get(self.path + f"{material.pk}/download/").status_code, 404)

    def test_admin_is_read_only(self):
        self.assertIn(Material, site._registry)
        admin = self.make_user("files-admin@example.com", role="admin", is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        material = self.upload().json()["material"]
        self.assertEqual(
            self.client.post(
                f"/admin/materials/material/{material['id']}/change/", {"scan_status": "clean"}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get("/admin/materials/material/add/").status_code, 403)

    def test_worker_timeout_fails_without_storing_file(self):
        with patch(
            "apps.materials.validation.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 20)
        ):
            self.assertEqual(self.upload().status_code, 400)
        self.assertFalse(Material.objects.exists())

    def test_new_consent_blocks_upload_but_not_download(self):
        data = self.upload().json()
        from apps.legal.models import LegalDocument

        LegalDocument.objects.filter(kind="terms", language="ru", is_active=True).update(is_active=False)
        LegalDocument.objects.create(
            kind="terms",
            language="ru",
            version="next-materials",
            title="Next",
            content="Next",
            is_active=True,
        )
        self.assertEqual(self.upload().status_code, 403)
        response = self.api.get(self.path + data["material"]["id"] + "/download/")
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_rejected_document_can_be_explicitly_accepted(self):
        self.under_review()
        material = Material.objects.first()
        route = self.review_path + f"{material.pk}/review/"
        result = self.api.post(
            route, {"expected_version": self.order.version, "decision": "rejected", "comment": "Check"}
        )
        self.assertEqual(result.status_code, 200)
        result = self.api.post(
            route,
            {
                "expected_version": result.json()["order_version"],
                "decision": "accepted",
                "comment": "Checked",
            },
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["material"]["review_status"], "accepted")

    def test_legacy_submission_has_no_fabricated_manifest(self):
        from apps.orders.models import OrderSubmission
        from apps.orders.serializers import OrderSubmissionSerializer

        submission = OrderSubmission.objects.create(
            order=self.order,
            version=1,
            form=self.order.form,
            form_checksum=self.order.form.checksum,
            answers=self.order.answers,
            asset_snapshot=self.order.asset_snapshot,
            service_snapshot=self.order.service_snapshot,
        )
        self.assertIsNone(OrderSubmissionSerializer(submission).data["materials_snapshot"])

    def test_storage_audit_dry_run_and_age_guard(self):
        import os
        import time

        from django.core.files.base import ContentFile
        from django.core.management import CommandError, call_command

        self.upload()
        material = Material.objects.get()
        storage = PrivateStorage()
        old = storage.save("orphan-old", ContentFile(b"test"))
        recent = storage.save("orphan-recent", ContentFile(b"test"))
        for key in (old, material.storage_key):
            os.utime(storage.path(key), (time.time() - 90000, time.time() - 90000))
        output = io.StringIO()
        call_command("audit_material_storage", stdout=output)
        self.assertIn("eligible_orphans=1 deleted=0", output.getvalue())
        self.assertTrue(storage.exists(old))
        with self.assertRaises(CommandError):
            call_command("audit_material_storage", min_age_hours=1, stdout=io.StringIO())
        call_command("audit_material_storage", delete_orphans=True, stdout=io.StringIO())
        self.assertFalse(storage.exists(old))
        self.assertTrue(storage.exists(recent))
        self.assertTrue(storage.exists(material.storage_key))
