import json

from django.core.management.base import BaseCommand

from apps.core.backups import verify_backup


class Command(BaseCommand):
    help = "Verify bundle structure and SHA-256 without modifying the application or database."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--archive", required=True)

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(verify_backup(options["archive"])))
