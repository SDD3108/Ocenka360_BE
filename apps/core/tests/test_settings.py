import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase


class ProductionSettingsTests(SimpleTestCase):
    def load_settings(self, **changes):
        environment = {
            **os.environ,
            "DJANGO_SECRET_KEY": "production-settings-test-only-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            "DJANGO_ALLOWED_HOSTS": "api.example.invalid",
            "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
            "CORS_ALLOWED_ORIGINS": "https://office.example.invalid",
            "CSRF_TRUSTED_ORIGINS": "https://api.example.invalid",
            "EMAIL_USE_TLS": "True",
            "EMAIL_USE_SSL": "False",
            **changes,
        }
        return subprocess.run(
            [sys.executable, "-c", "import config.settings.production"],
            cwd=settings.BASE_DIR,
            env=environment,
            text=True,
            capture_output=True,
        )

    def test_valid_configuration_loads(self):
        result = self.load_settings()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsafe_configuration_fails_closed(self):
        for changes in (
            {"DJANGO_SECRET_KEY": "short"},
            {"DJANGO_ALLOWED_HOSTS": "*"},
            {"DATABASE_URL": "sqlite:///:memory:"},
            {"CORS_ALLOWED_ORIGINS": "http://office.example.invalid"},
            {"EMAIL_USE_SSL": "True", "EMAIL_USE_TLS": "True"},
        ):
            with self.subTest(changes=changes):
                result = self.load_settings(**changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ImproperlyConfigured", result.stderr)
