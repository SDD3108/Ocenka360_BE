from datetime import UTC, datetime, timedelta

from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import Throttled

from .models import AuthRateBucket, EmailCooldown


def digest(purpose, value):
    return salted_hmac("ocenka360:" + purpose, str(value), algorithm="sha256").hexdigest()


def reserve_quota(scope, identity, limit, seconds):
    now = timezone.now()
    window = int(now.timestamp()) // seconds
    end = datetime.fromtimestamp((window + 1) * seconds, tz=UTC)
    key = digest("quota", f"{scope}:{identity}:{window}")
    AuthRateBucket.objects.get_or_create(key=key, defaults={"expires_at": end})
    # Atomic conditional increment: no process-local cache race.
    if not AuthRateBucket.objects.filter(key=key, count__lt=limit).update(count=F("count") + 1):
        raise Throttled(wait=max(1, int((end - now).total_seconds())))


def reserve_email_cooldown(email, seconds):
    now = timezone.now()
    key = digest("email-cooldown", email)
    EmailCooldown.objects.get_or_create(email_digest=key, defaults={"next_allowed_at": now})
    if not EmailCooldown.objects.filter(email_digest=key, next_allowed_at__lte=now).update(
        next_allowed_at=now + timedelta(seconds=seconds)
    ):
        next_at = EmailCooldown.objects.get(pk=key).next_allowed_at
        raise Throttled(wait=max(1, int((next_at - now).total_seconds())))
