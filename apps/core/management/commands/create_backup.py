from django.core.management.base import BaseCommand

from apps.core.backups import create_backup


class Command(BaseCommand):
    help = "Offline database + private materials bundle. Stop all writers first. Contains sensitive unencrypted data."

    def add_arguments(self, parser):
        parser.add_argument("--destination", required=True)
        parser.add_argument("--writers-stopped", action="store_true")

    def handle(self, *args, **options):
        path = create_backup(options["destination"], writers_stopped=options["writers_stopped"])
        self.stdout.write(str(path))
