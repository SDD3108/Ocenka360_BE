from django.core.management.base import BaseCommand

from apps.assistance.worker import cleanup, recover


class Command(BaseCommand):
    help = "Remove expired AI input/output locally; retain minimal audit metadata. Does not delete Google-side data."

    def handle(self, *args, **options):
        self.stdout.write(f"expired={cleanup()} interrupted={recover()}")
