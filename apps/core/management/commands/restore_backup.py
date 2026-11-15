import json

from django.core.management.base import BaseCommand

from apps.core.backups import verify_backup


class Command(BaseCommand):
    help = "Verify and extract into a NEW directory only. Does NOT import PostgreSQL or replace the running application."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--archive", required=True)
        parser.add_argument("--destination", required=True)

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(verify_backup(options["archive"], destination=options["destination"])))
