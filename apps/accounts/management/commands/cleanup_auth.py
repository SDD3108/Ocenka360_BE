from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import AuthRateBucket, AuthSession, EmailChallenge, EmailCooldown


class Command(BaseCommand):
    help = "Remove expired auth data; does not remove users or consent records. Run daily."

    def handle(self, *args, **options):
        now = timezone.now()
        total = 0
        for query in (
            AuthRateBucket.objects.filter(expires_at__lt=now),
            EmailCooldown.objects.filter(next_allowed_at__lt=now - timedelta(days=1)),
            EmailChallenge.objects.filter(expires_at__lt=now - timedelta(days=1)),
            AuthSession.objects.filter(expires_at__lt=now - timedelta(days=30)),
        ):
            count, _ = query.delete()
            total += count
        self.stdout.write(f"Removed {total} expired auth records. Users and consents unchanged.")
