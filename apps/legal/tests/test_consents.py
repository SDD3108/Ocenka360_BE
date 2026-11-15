import io
from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import AuthRateBucket, EmailChallenge, User
from apps.legal.models import ConsentAcceptance, LegalDocument


class ConsentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_legal_drafts", stdout=io.StringIO())

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("verified@example.com", email_verified_at=timezone.now())
        self.api = APIClient()
        self.api.force_login(self.user)

    def documents(self, language="ru"):
        result = self.api.get("/api/v1/legal/documents/", {"language": language})
        self.assertEqual(result.status_code, 200)
        return result.data

    def accept(self, ids=None, adult=True):
        return self.api.post(
            "/api/v1/legal/consents/",
            {
                "documents": ids if ids is not None else [str(doc["id"]) for doc in self.documents()],
                "adult_confirmed": adult,
            },
        )

    def test_documents_are_public_localized_and_marked_draft(self):
        self.api.logout()
        for language in ("ru", "kk", "en"):
            documents = self.documents(language)
            self.assertEqual(len(documents), 3)
            self.assertTrue(all(doc["language"] == language and doc["is_draft"] for doc in documents))
            self.assertTrue(all(len(doc["checksum"]) == 64 for doc in documents))
        self.assertEqual(self.api.get("/api/v1/legal/documents/?language=xx").status_code, 400)

    def test_explicit_acceptance_is_versioned_and_idempotent(self):
        self.assertTrue(self.api.get("/api/v1/accounts/me/").data["requires_consent"])
        response = self.accept()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["requires_consent"])
        self.assertEqual(ConsentAcceptance.objects.count(), 3)
        for record in ConsentAcceptance.objects.select_related("document"):
            self.assertEqual(record.document_checksum, record.document.checksum)
        self.assertEqual(self.accept().status_code, 200)
        self.assertEqual(ConsentAcceptance.objects.count(), 3)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.adult_confirmed_at)

    def test_no_acceptance_without_adult_confirmation_or_complete_versions(self):
        self.assertEqual(self.accept(adult=False).status_code, 400)
        self.assertEqual(self.accept(ids=[]).status_code, 400)
        docs = [str(doc["id"]) for doc in self.documents()]
        self.assertEqual(self.accept(ids=[docs[0]] * 3).status_code, 400)
        self.assertEqual(ConsentAcceptance.objects.count(), 0)

    def test_unverified_admin_cannot_record_email_ownership_by_accepting_terms(self):
        User.objects.filter(pk=self.user.pk).update(email_verified_at=None)
        self.assertEqual(self.accept().status_code, 400)
        self.assertEqual(ConsentAcceptance.objects.count(), 0)

    def test_document_new_version_requires_new_consent_and_preserves_history(self):
        old_ids = [str(doc["id"]) for doc in self.documents()]
        self.assertEqual(self.accept(old_ids).status_code, 200)
        old = LegalDocument.objects.get(kind="terms", language="ru", is_active=True)
        old.is_active = False
        old.save()
        LegalDocument.objects.create(
            kind="terms",
            language="ru",
            version="dev-2",
            title="Updated",
            content="Updated test terms",
            is_active=True,
        )
        self.assertTrue(self.api.get("/api/v1/accounts/me/").data["requires_consent"])
        self.assertEqual(self.accept(old_ids).status_code, 400)
        self.assertEqual(self.accept().status_code, 200)
        self.assertEqual(self.api.get("/api/v1/legal/consents/").status_code, 200)
        self.assertEqual(ConsentAcceptance.objects.filter(user=self.user).count(), 4)
        self.assertTrue(ConsentAcceptance.objects.filter(user=self.user, document=old).exists())

    def test_existing_version_content_cannot_be_edited(self):
        document = LegalDocument.objects.first()
        document.content = "Silently changed agreement"
        with self.assertRaises(ValidationError):
            document.save()

    def test_user_only_sees_own_consent_history(self):
        self.accept()
        other = User.objects.create_user("other@example.com", email_verified_at=timezone.now())
        self.api.force_login(other)
        self.assertEqual(self.api.get("/api/v1/legal/consents/").data, [])
        self.api.logout()
        self.assertEqual(self.api.get("/api/v1/legal/consents/").status_code, 401)

    @override_settings(ALLOW_DRAFT_LEGAL_DOCUMENTS=False)
    def test_seed_refused_outside_development(self):
        with self.assertRaises(CommandError):
            call_command("seed_legal_drafts", stdout=io.StringIO())

    @override_settings(ALLOW_DRAFT_LEGAL_DOCUMENTS=False, COMPANY_LEGAL_NAME="ИП Тест")
    def test_only_published_non_draft_documents_are_available_in_production(self):
        LegalDocument.objects.filter(language="ru").update(is_active=False)
        for kind in LegalDocument.Kind.values:
            LegalDocument.objects.create(
                kind=kind,
                language="ru",
                version="1",
                title=kind,
                content="Approved test text",
                is_active=True,
                is_draft=False,
            )
        self.assertTrue(all(not doc["is_draft"] for doc in self.documents()))

    def test_cleanup_removes_expired_auth_data_but_preserves_users_and_consents(self):
        self.accept()
        EmailChallenge.objects.create(
            email="old@example.com",
            language="ru",
            code_digest="digest",
            expires_at=timezone.now() - timedelta(days=3),
        )
        AuthRateBucket.objects.create(key="old", expires_at=timezone.now() - timedelta(days=1))
        AuthRateBucket.objects.create(key="active", expires_at=timezone.now() + timedelta(hours=1))
        call_command("cleanup_auth", stdout=io.StringIO())
        self.assertEqual(EmailChallenge.objects.count(), 0)
        self.assertEqual(list(AuthRateBucket.objects.values_list("key", flat=True)), ["active"])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(ConsentAcceptance.objects.count(), 3)
