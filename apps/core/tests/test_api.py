import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient
from rest_framework.throttling import AnonRateThrottle

from apps.accounts.models import User
from apps.core.exceptions import api_exception_handler


class SystemApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_liveness_works_without_database(self):
        with patch("apps.core.views.connection.cursor", side_effect=OperationalError("secret database URL")):
            response = self.client.get(reverse("core:live"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readiness_queries_database(self):
        with self.assertNumQueries(1):
            response = self.client.get(reverse("core:ready"))
        self.assertEqual(response.status_code, 200)

    def test_readiness_fails_without_leaking_database_details(self):
        with patch("apps.core.views.connection.cursor", side_effect=OperationalError("secret database URL")):
            response = self.client.get(reverse("core:ready"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_config_is_public_and_truthful(self):
        response = self.client.get(reverse("core:config"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["company"]["email"], "sati-ocenka@mail.ru")
        self.assertIsNone(data["company"]["legal_name"])
        self.assertEqual(data["supported_languages"], ["ru", "kk", "en"])
        self.assertEqual(data["currency"], "KZT")
        self.assertTrue(data["features"]["email_otp"])
        self.assertTrue(data["features"]["catalog"])
        self.assertTrue(data["features"]["orders"])
        self.assertTrue(data["features"]["materials"])
        self.assertTrue(data["features"]["pricing"])
        self.assertTrue(data["features"]["scheduling"])
        self.assertTrue(data["features"]["reports"])
        self.assertTrue(data["features"]["reviewer_operations"])
        self.assertTrue(data["features"]["communications"])
        self.assertTrue(data["features"]["notification_inbox"])
        self.assertFalse(
            any(
                v
                for k, v in data["features"].items()
                if k
                not in {
                    "email_otp",
                    "catalog",
                    "orders",
                    "materials",
                    "pricing",
                    "scheduling",
                    "reports",
                    "reviewer_operations",
                    "communications",
                    "notification_inbox",
                }
            )
        )
        for forbidden in ("SECRET_KEY", "DATABASE_URL", "EMAIL_HOST_PASSWORD"):
            self.assertNotContains(response, forbidden)

    @override_settings(COMPANY_LEGAL_NAME="ИП Пример")
    def test_confirmed_legal_name_can_be_configured(self):
        self.assertEqual(self.client.get(reverse("core:config")).json()["company"]["legal_name"], "ИП Пример")

    def test_request_id_generated_and_not_trusted_from_client(self):
        response = self.client.get(reverse("api-root"), HTTP_X_REQUEST_ID="untrusted-value")
        self.assertIsInstance(uuid.UUID(response["X-Request-ID"]), uuid.UUID)
        self.assertNotEqual(response["X-Request-ID"], "untrusted-value")

    def test_unknown_endpoint_uses_json_error(self):
        response = self.client.get("/api/v1/missing/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_private_files_not_served(self):
        for path in ("/media/id.pdf", "/private_media/id.pdf"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_unhandled_exception_response_does_not_leak(self):
        with self.assertLogs("apps.core.exceptions", level="ERROR") as logs:
            response = api_exception_handler(RuntimeError("SECRET-TOKEN-XYZ"), {})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("SECRET-TOKEN-XYZ", str(response.data))
        self.assertNotIn("SECRET-TOKEN-XYZ", " ".join(logs.output))

    def test_validation_details_keep_field_names(self):
        response = api_exception_handler(ValidationError({"email": ["Invalid email."]}), {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "validation_error")
        self.assertIn("email", response.data["error"]["details"])

    def test_anonymous_rate_limit_returns_429(self):
        with patch.object(AnonRateThrottle, "get_rate", return_value="2/min"):
            self.assertEqual(self.client.get(reverse("core:config")).status_code, 200)
            self.assertEqual(self.client.get(reverse("core:config")).status_code, 200)
            response = self.client.get(reverse("core:config"))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "throttled")
        self.assertIn("Retry-After", response)

    @override_settings(CORS_ALLOWED_ORIGINS=["https://office.example.invalid"])
    def test_cors_only_allows_configured_origins(self):
        path = reverse("core:config")
        allowed = self.client.get(path, HTTP_ORIGIN="https://office.example.invalid")
        denied = self.client.get(path, HTTP_ORIGIN="https://evil.example.invalid")
        self.assertEqual(allowed["Access-Control-Allow-Origin"], "https://office.example.invalid")
        self.assertNotIn("Access-Control-Allow-Origin", denied)

    def test_schema_and_docs_require_staff_outside_local_settings(self):
        for name in ("schema", "swagger-ui"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 401)
        user = User.objects.create_user("client@example.com")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("schema")).status_code, 403)
        admin = User.objects.create_superuser("owner@example.com", "Strong-Test-Password!42")
        self.client.force_login(admin)
        response = self.client.get(reverse("schema"), HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/api/v1/accounts/me/", response.json()["paths"])
        self.assertIn("/api/v1/auth/request-code/", response.json()["paths"])
