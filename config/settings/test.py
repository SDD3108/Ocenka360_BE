import os

# A fixed key is confined to tests; no real credentials or production database.
os.environ.setdefault("DJANGO_SECRET_KEY", "tests-only-not-a-deployment-key-0123456789-abcdefghijk")

from .base import *  # noqa: E402, F403
from .base import env  # noqa: E402

DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
DATABASES = {"default": env.db("TEST_DATABASE_URL", default="sqlite:///:memory:")}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
COMPANY_LEGAL_NAME = ""
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "loggers": {
        "django": {"handlers": ["null"], "propagate": False},
        "django.request": {"handlers": ["null"], "propagate": False},
    },
}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

ALLOW_DRAFT_LEGAL_DOCUMENTS = True

ALLOW_ORDER_SIMULATION = True
