from django.core.management.base import BaseCommand, CommandError

from apps.notifications.worker import check_receipts


class Command(BaseCommand):
    help = "Process a bounded batch of Expo push work. No scheduling is installed."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 100:
            raise CommandError("--limit must be between 1 and 100")
        result = check_receipts(options["limit"])
        self.stdout.write(str(result))
