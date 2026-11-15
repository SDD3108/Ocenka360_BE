import logging
import secrets
from datetime import timedelta
from smtplib import SMTPException

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from rest_framework.exceptions import APIException, AuthenticationFailed
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.legal.services import required_documents

from .models import AuthSession, EmailChallenge, User
from .security import digest, reserve_email_cooldown, reserve_quota

logger = logging.getLogger(__name__)


class MailUnavailable(APIException):
    status_code = 503
    default_code = "email_unavailable"
    default_detail = "Email delivery is temporarily unavailable. Please try again later."


class InvalidCode(APIException):
    status_code = 400
    default_code = "invalid_code"
    default_detail = "Code is invalid, expired or already used. Request a new code if needed."


def request_code(email, language, ip):
    required_documents(language)
    reserve_quota("otp-ip", ip, settings.OTP_IP_HOURLY_LIMIT, 3600)
    reserve_quota("otp-email", email, settings.OTP_EMAIL_HOURLY_LIMIT, 3600)
    reserve_quota("otp-global", "all", settings.OTP_GLOBAL_DAILY_LIMIT, 86400)
    reserve_email_cooldown(email, settings.OTP_RESEND_SECONDS)
    now = timezone.now()
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge = EmailChallenge(
        email=email, language=language, expires_at=now + timedelta(seconds=settings.OTP_TTL_SECONDS)
    )
    challenge.code_digest = digest("otp", f"{challenge.id}:{code}")
    challenge.save()
    # Disabled accounts get the same public response, without mail or a usable challenge.
    disabled = User.objects.filter(email=email, is_active=False).exists()
    if not disabled:
        minutes = settings.OTP_TTL_SECONDS // 60
        messages = {
            "ru": (
                "Оценка 360 — код входа",
                f"Ваш код входа: {code}\nДействует {minutes} мин. Никому не сообщайте код. Если вы не запрашивали вход, проигнорируйте письмо.",
            ),
            "kk": (
                "Ocenka 360 — кіру коды",
                f"Кіру кодыңыз: {code}\nКод {minutes} минут жарамды. Кодты ешкімге айтпаңыз. Сұрау жібермесеңіз, хатты елемеңіз.",
            ),
            "en": (
                "Ocenka 360 — sign-in code",
                f"Your sign-in code: {code}\nValid for {minutes} minutes. Never share this code. Ignore this email if you did not request it.",
            ),
        }
        subject, body = messages[language]
        try:
            count = send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
            if count != 1:
                raise OSError("No message sent")
        except (SMTPException, OSError):
            # Never log the exception: SMTP messages may contain addresses/credentials.
            logger.warning("OTP mail delivery failed challenge=%s", challenge.id)
            raise MailUnavailable() from None
        with transaction.atomic():
            EmailChallenge.objects.filter(
                email=email, consumed_at__isnull=True, created_at__lt=challenge.created_at
            ).update(invalidated_at=now)
            EmailChallenge.objects.filter(pk=challenge.pk).update(delivered=True)
    return challenge


def token_pair(user, session):
    refresh = RefreshToken.for_user(user)
    refresh["sid"] = str(session.id)
    refresh["ver"] = user.token_version
    # Absolute lifetime of this login, not an indefinitely sliding refresh lifetime.
    refresh["exp"] = int(session.expires_at.timestamp())
    access = refresh.access_token
    access["exp"] = min(access["exp"], refresh["exp"])
    return refresh, {
        "access": str(access),
        "refresh": str(refresh),
        "token_type": "Bearer",
        "access_expires_in": max(0, access["exp"] - int(timezone.now().timestamp())),
        "session_expires_at": session.expires_at,
        "session_id": str(session.id),
    }


def verify_code(challenge_id, code, ip, device_name):
    reserve_quota("verify-ip", ip, settings.OTP_VERIFY_IP_LIMIT, 600)
    now = timezone.now()
    # Attempts persist even when the subsequent validation/transaction fails.
    eligible = EmailChallenge.objects.filter(
        pk=challenge_id,
        delivered=True,
        consumed_at__isnull=True,
        invalidated_at__isnull=True,
        expires_at__gt=now,
        attempts__lt=settings.OTP_MAX_ATTEMPTS,
    )
    if not eligible.update(attempts=F("attempts") + 1):
        raise InvalidCode()
    challenge = EmailChallenge.objects.get(pk=challenge_id)
    if not constant_time_compare(challenge.code_digest, digest("otp", f"{challenge.id}:{code}")):
        raise InvalidCode()
    with transaction.atomic():
        # CAS consumption prevents two successful uses, including when row locks are unavailable.
        if not EmailChallenge.objects.filter(
            pk=challenge.pk,
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).update(consumed_at=now):
            raise InvalidCode()
        user, created = User.objects.get_or_create(
            email=challenge.email,
            defaults={"password": make_password(None), "preferred_language": challenge.language},
        )
        user = User.objects.select_for_update().get(pk=user.pk)
        if not user.is_active:
            raise InvalidCode()
        user.email_verified_at = user.email_verified_at or now
        user.last_login = now
        user.save(update_fields=["email_verified_at", "last_login", "updated_at"])
        session = AuthSession(
            user=user,
            last_used_at=now,
            device_name=device_name,
            expires_at=now + settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"],
        )
        refresh, pair = token_pair(user, session)
        session.refresh_digest = digest("refresh-jti", refresh["jti"])
        session.save()
    return user, pair, created


def rotate_refresh(raw_token):
    try:
        token = RefreshToken(raw_token)
        user_id = token["user_id"]
        session_id = token["sid"]
        # Validate claim shape before passing it into UUID database fields.
        import uuid

        uuid.UUID(str(user_id))
        uuid.UUID(str(session_id))
    except (TokenError, KeyError, ValueError, TypeError) as exc:
        raise AuthenticationFailed("Invalid refresh token.", code="invalid_refresh") from exc
    pair = None
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id, is_active=True).first()
        if user and token.get("ver") == user.token_version:
            session = (
                AuthSession.objects.select_for_update()
                .filter(
                    pk=session_id,
                    user=user,
                    revoked_at__isnull=True,
                    expires_at__gt=timezone.now(),
                )
                .first()
            )
            if session:
                old_digest = digest("refresh-jti", token["jti"])
                if constant_time_compare(session.refresh_digest, old_digest):
                    refresh, candidate = token_pair(user, session)
                    changed = AuthSession.objects.filter(
                        pk=session.pk, refresh_digest=old_digest, revoked_at__isnull=True
                    ).update(
                        refresh_digest=digest("refresh-jti", refresh["jti"]), last_used_at=timezone.now()
                    )
                    if changed:
                        pair = candidate
                else:
                    # Persist revocation on reuse; raise only AFTER transaction commit.
                    AuthSession.objects.filter(pk=session.pk).update(revoked_at=timezone.now())
    if pair is None:
        raise AuthenticationFailed(
            "Refresh expired, revoked or reused. Sign in again.", code="invalid_refresh"
        )
    return pair


@transaction.atomic
def logout_all(user):
    user = User.objects.select_for_update().get(pk=user.pk)
    user.token_version += 1
    user.save(update_fields=["token_version", "updated_at"])
    user.app_sessions.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
