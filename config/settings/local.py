from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import EMAIL_DELIVERY_MODE, EMAIL_USE_SSL, EMAIL_USE_TLS, REST_FRAMEWORK, SPECTACULAR_SETTINGS

DEBUG = True
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}
SPECTACULAR_SETTINGS = {
    **SPECTACULAR_SETTINGS,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.AllowAny"],
}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


ALLOW_DRAFT_LEGAL_DOCUMENTS = True
if EMAIL_DELIVERY_MODE == "smtp":
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
elif EMAIL_DELIVERY_MODE != "console":
    raise ImproperlyConfigured("EMAIL_DELIVERY_MODE must be console or smtp.")
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured("Choose TLS or SSL, not both.")
