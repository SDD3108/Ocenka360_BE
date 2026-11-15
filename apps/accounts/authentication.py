from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import AuthSession


class SessionJWTAuthentication(JWTAuthentication):
    """Signed access token plus live DB session; logout takes effect immediately."""

    def get_user(self, validated_token):
        try:
            user = super().get_user(validated_token)
            valid = (
                validated_token.get("ver") == user.token_version
                and AuthSession.objects.filter(
                    id=validated_token.get("sid"),
                    user=user,
                    revoked_at__isnull=True,
                    expires_at__gt=timezone.now(),
                ).exists()
            )
        except (DjangoValidationError, ValueError, TypeError, KeyError) as exc:
            raise AuthenticationFailed("Invalid session.", code="session_invalid") from exc
        if not valid:
            raise AuthenticationFailed("Session expired or revoked.", code="session_invalid")
        return user


class SessionJWTAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "apps.accounts.authentication.SessionJWTAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
