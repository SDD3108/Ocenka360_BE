import io
import re
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts.models import AuthRateBucket, AuthSession, EmailChallenge, EmailCooldown, User
from apps.legal.models import ConsentAcceptance


class AuthFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_legal_drafts", stdout=io.StringIO())

    def setUp(self):
        cache.clear()
        self.api = APIClient()

    def issue(self, email="new@example.com", language="ru"):
        response = self.api.post("/api/v1/auth/request-code/", {"email": email, "language": language})
        self.assertEqual(response.status_code, 202, response.data)
        code = re.search(r"\b[0-9]{6}\b", mail.outbox[-1].body).group()
        return response.data["challenge_id"], code

    def login(self, email="new@example.com", language="ru"):
        challenge, code = self.issue(email, language)
        response = self.api.post(
            "/api/v1/auth/verify-code/",
            {
                "challenge_id": str(challenge),
                "code": code,
                "device_name": "Test phone",
            },
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def authorize(self, tokens):
        self.api.credentials(HTTP_AUTHORIZATION="Bearer " + tokens["access"])

    def test_registration_creates_unprivileged_verified_user_only_after_correct_code(self):
        challenge_id, code = self.issue(" NEW@Example.com ")
        self.assertFalse(User.objects.exists())
        challenge = EmailChallenge.objects.get(pk=challenge_id)
        self.assertNotEqual(challenge.code_digest, code)
        self.assertNotIn("code", challenge.__dict__)
        response = self.api.post("/api/v1/auth/verify-code/", {"challenge_id": challenge_id, "code": code})
        self.assertEqual(response.status_code, 200)
        user = User.objects.get()
        self.assertEqual(user.email, "new@example.com")
        self.assertEqual(user.role, "client")
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertIsNotNone(user.email_verified_at)
        self.assertIsNone(user.adult_confirmed_at)
        self.assertTrue(response.data["is_new_user"])
        self.assertTrue(response.data["user"]["requires_consent"])
        self.assertEqual(ConsentAcceptance.objects.count(), 0)
        self.authorize(response.data)
        self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 200)

    def test_existing_appraiser_role_and_password_preserved(self):
        user = User.objects.create_user("new@example.com", "Original-password!73", role="appraiser")
        tokens = self.login()
        user.refresh_from_db()
        self.assertEqual(tokens["user"]["role"], "appraiser")
        self.assertFalse(tokens["is_new_user"])
        self.assertTrue(user.check_password("Original-password!73"))

    def test_request_never_returns_the_code_or_account_existence(self):
        response = self.api.post("/api/v1/auth/request-code/", {"email": "new@example.com"})
        self.assertEqual(set(response.data), {"challenge_id", "expires_in", "resend_after", "message"})
        self.assertNotIn("is_new_user", response.data)

    def test_disabled_user_gets_generic_request_and_no_email(self):
        User.objects.create_user("disabled@example.com", is_active=False)
        response = self.api.post("/api/v1/auth/request-code/", {"email": "disabled@example.com"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(mail.outbox), 0)
        response = self.api.post(
            "/api/v1/auth/verify-code/", {"challenge_id": response.data["challenge_id"], "code": "000000"}
        )
        self.assertEqual(response.status_code, 400)

    def test_five_wrong_attempts_lock_even_the_correct_code(self):
        challenge, code = self.issue()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            result = self.api.post("/api/v1/auth/verify-code/", {"challenge_id": challenge, "code": wrong})
            self.assertEqual(result.status_code, 400)
        result = self.api.post("/api/v1/auth/verify-code/", {"challenge_id": challenge, "code": code})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(EmailChallenge.objects.get(pk=challenge).attempts, 5)
        self.assertFalse(User.objects.exists())

    def test_expired_code_rejected(self):
        challenge, code = self.issue()
        EmailChallenge.objects.filter(pk=challenge).update(expires_at=timezone.now() - timedelta(seconds=1))
        result = self.api.post("/api/v1/auth/verify-code/", {"challenge_id": challenge, "code": code})
        self.assertEqual(result.status_code, 400)

    def test_successful_code_cannot_be_reused(self):
        challenge, code = self.issue()
        body = {"challenge_id": challenge, "code": code}
        self.assertEqual(self.api.post("/api/v1/auth/verify-code/", body).status_code, 200)
        self.assertEqual(self.api.post("/api/v1/auth/verify-code/", body).status_code, 400)
        self.assertEqual(AuthSession.objects.count(), 1)

    def test_code_is_bound_to_its_challenge(self):
        with patch("apps.accounts.services.secrets.randbelow", side_effect=[123456, 654321]):
            first, code = self.issue("one@example.com")
            second, _ = self.issue("two@example.com")
        result = self.api.post("/api/v1/auth/verify-code/", {"challenge_id": second, "code": code})
        self.assertEqual(result.status_code, 400)
        self.assertIsNone(EmailChallenge.objects.get(pk=first).consumed_at)

    def test_resend_has_cooldown_and_invalidates_old_code(self):
        first, old_code = self.issue()
        rejected = self.api.post("/api/v1/auth/request-code/", {"email": "NEW@example.com"})
        self.assertEqual(rejected.status_code, 429)
        self.assertIn("Retry-After", rejected)
        EmailCooldown.objects.update(next_allowed_at=timezone.now() - timedelta(seconds=1))
        second, new_code = self.issue()
        self.assertEqual(
            self.api.post("/api/v1/auth/verify-code/", {"challenge_id": first, "code": old_code}).status_code,
            400,
        )
        self.assertEqual(
            self.api.post(
                "/api/v1/auth/verify-code/", {"challenge_id": second, "code": new_code}
            ).status_code,
            200,
        )

    def test_email_failure_is_not_reported_as_delivered_and_keeps_previous_code_valid(self):
        challenge, code = self.issue()
        EmailCooldown.objects.update(next_allowed_at=timezone.now() - timedelta(seconds=1))
        with patch("apps.accounts.services.send_mail", side_effect=OSError("private SMTP credentials")):
            response = self.api.post("/api/v1/auth/request-code/", {"email": "new@example.com"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "email_unavailable")
        self.assertNotIn("private SMTP credentials", str(response.data))
        self.assertEqual(
            self.api.post("/api/v1/auth/verify-code/", {"challenge_id": challenge, "code": code}).status_code,
            200,
        )

    @override_settings(OTP_GLOBAL_DAILY_LIMIT=1)
    def test_database_global_quota_survives_cache_clear(self):
        self.issue()
        cache.clear()
        response = self.api.post(
            "/api/v1/auth/request-code/", {"email": "other@example.com"}, REMOTE_ADDR="192.0.2.2"
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(mail.outbox), 1)
        for bucket in AuthRateBucket.objects.all():
            self.assertNotIn("@", bucket.key)

    @override_settings(OTP_IP_HOURLY_LIMIT=1)
    def test_forwarded_header_cannot_bypass_ip_quota(self):
        self.issue()
        result = self.api.post(
            "/api/v1/auth/request-code/", {"email": "other@example.com"}, HTTP_X_FORWARDED_FOR="192.0.2.50"
        )
        self.assertEqual(result.status_code, 429)

    def test_privilege_injection_rejected_before_any_email(self):
        result = self.api.post("/api/v1/auth/request-code/", {"email": "x@example.com", "role": "admin"})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_profile_updates_only_allowed_fields(self):
        tokens = self.login()
        self.authorize(tokens)
        response = self.api.patch(
            "/api/v1/accounts/me/",
            {
                "first_name": "Дамир",
                "last_name": "Сатимов",
                "phone_number": "+77001234567",
                "preferred_language": "kk",
            },
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["first_name"], "Дамир")
        self.assertFalse(response.data["phone_verified"])
        for fields in (
            {"role": "admin"},
            {"is_staff": True},
            {"email": "changed@example.com"},
            {"adult_confirmed_at": "2026-01-01"},
            {"token_version": 100},
        ):
            self.assertEqual(self.api.patch("/api/v1/accounts/me/", fields).status_code, 400)
        self.assertEqual(self.api.patch("/api/v1/accounts/me/", {"phone_number": "123"}).status_code, 400)
        self.assertEqual(User.objects.get().role, "client")

    def test_refresh_rotates_and_reuse_revokes_the_session(self):
        tokens = self.login()
        response = self.api.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]})
        self.assertEqual(response.status_code, 200, response.data)
        rotated = response.data
        self.assertNotEqual(rotated["refresh"], tokens["refresh"])
        self.assertEqual(str(rotated["session_expires_at"]), str(tokens["session_expires_at"]))
        self.authorize(rotated)
        self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 200)
        result = self.api.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]})
        self.assertEqual(result.status_code, 401)
        self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 401)
        self.assertEqual(
            self.api.post("/api/v1/auth/refresh/", {"refresh": rotated["refresh"]}).status_code, 401
        )

    def test_logout_immediately_revokes_access_and_refresh(self):
        tokens = self.login()
        self.authorize(tokens)
        self.assertEqual(self.api.post("/api/v1/auth/logout/").status_code, 204)
        self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 401)
        self.assertEqual(
            self.api.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}).status_code, 401
        )

    def test_logout_all_and_other_users_are_isolated(self):
        first = self.login()
        EmailCooldown.objects.update(next_allowed_at=timezone.now() - timedelta(seconds=1))
        second = self.login()
        other = self.login("other@example.com")
        self.authorize(first)
        listed = self.api.get("/api/v1/auth/sessions/").data
        self.assertEqual(len(listed), 2)
        self.assertNotIn("refresh_digest", listed[0])
        self.assertEqual(self.api.delete(f"/api/v1/auth/sessions/{other['session_id']}/").status_code, 404)
        self.assertEqual(self.api.post("/api/v1/auth/logout-all/").status_code, 204)
        for tokens in (first, second):
            self.authorize(tokens)
            self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 401)
            self.assertEqual(
                self.api.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}).status_code, 401
            )
        self.authorize(other)
        self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 200)

    def test_revoke_one_device_keeps_another_active(self):
        first = self.login()
        EmailCooldown.objects.update(next_allowed_at=timezone.now() - timedelta(seconds=1))
        second = self.login()
        self.authorize(first)
        self.assertEqual(self.api.delete(f"/api/v1/auth/sessions/{second['session_id']}/").status_code, 204)
        self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 200)
        self.authorize(second)
        self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 401)

    def test_inactive_account_rejects_outstanding_access_and_refresh(self):
        tokens = self.login()
        User.objects.update(is_active=False)
        self.authorize(tokens)
        self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 401)
        self.assertEqual(
            self.api.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]}).status_code, 401
        )

    def test_jwt_signature_type_expiry_and_session_claims_checked(self):
        tokens = self.login()
        cases = [tokens["refresh"], tokens["access"] + "tampered"]
        expired = AccessToken(tokens["access"])
        expired.set_exp(from_time=timezone.now() - timedelta(hours=1), lifetime=timedelta(minutes=1))
        cases.append(str(expired))
        malformed = AccessToken(tokens["access"])
        malformed["sid"] = "not-a-uuid"
        cases.append(str(malformed))
        for token in cases:
            self.api.credentials(HTTP_AUTHORIZATION="Bearer " + token)
            self.assertEqual(self.api.get("/api/v1/accounts/me/").status_code, 401)
        refresh = RefreshToken(tokens["refresh"])
        refresh.set_exp(from_time=timezone.now() - timedelta(days=40), lifetime=timedelta(days=1))
        self.assertEqual(self.api.post("/api/v1/auth/refresh/", {"refresh": str(refresh)}).status_code, 401)
        self.assertEqual(
            self.api.post("/api/v1/auth/refresh/", {"refresh": tokens["access"]}).status_code, 401
        )

    def test_email_language_and_multiple_locales(self):
        for language, email, expected in (
            ("kk", "kk@example.com", "Кіру кодыңыз"),
            ("en", "en@example.com", "Your sign-in code"),
        ):
            self.issue(email, language)
            self.assertIn(expected, mail.outbox[-1].body)

    @override_settings(ALLOW_DRAFT_LEGAL_DOCUMENTS=False, COMPANY_LEGAL_NAME="ИП Тест")
    def test_drafts_cannot_enable_production_registration(self):
        result = self.api.post("/api/v1/auth/request-code/", {"email": "new@example.com"})
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.data["error"]["code"], "legal_not_ready")
        self.assertEqual(len(mail.outbox), 0)
