from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import (
    ALLOWED_HOSTS,
    CORS_ALLOWED_ORIGINS,
    CSRF_TRUSTED_ORIGINS,
    DATABASES,
    MIDDLEWARE,
    REST_FRAMEWORK,
    SECRET_KEY,
    env,
)

DEBUG = False
MIDDLEWARE = [*MIDDLEWARE[:2], "whitenoise.middleware.WhiteNoiseMiddleware", *MIDDLEWARE[2:]]
if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("Production requires a unique random DJANGO_SECRET_KEY (50+ chars).")
if not ALLOWED_HOSTS or any(h in {"*", "localhost", "127.0.0.1"} or h.startswith(".") for h in ALLOWED_HOSTS):
    raise ImproperlyConfigured("Set exact production DJANGO_ALLOWED_HOSTS; no wildcards or localhost.")
if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
    raise ImproperlyConfigured("Production requires a PostgreSQL DATABASE_URL.")
for origin in CORS_ALLOWED_ORIGINS + CSRF_TRUSTED_ORIGINS:
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "*" in origin
    ):
        raise ImproperlyConfigured(
            "Production origins must be exact HTTPS origins without credentials/path/query."
        )

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 3600
# Enable only after all subdomains support HTTPS; no premature preload commitment.
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
if env.bool("TRUST_PROXY_HTTPS", default=False):
    # Only behind a proxy that removes/replaces the header, with no direct public app port.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Exactly one trusted NGINX overwrites X-Forwarded-For; app is loopback-only.
    REST_FRAMEWORK = {**REST_FRAMEWORK, "NUM_PROXIES": 1}

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured("Select EMAIL_USE_TLS or EMAIL_USE_SSL, not both.")
