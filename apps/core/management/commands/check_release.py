import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.release import release_checks


class Command(BaseCommand):
    help = "Read-only JSON preflight. --strict exits nonzero on configuration blockers; never certifies external integrations."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true")
        parser.add_argument("--backup-archive")

    def handle(self, *args, **options):
        result = release_checks(options["backup_archive"])
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        if options["strict"] and not result["configuration_checks_passed"]:
            raise CommandError("Release configuration checks are blocked; see JSON above.")
