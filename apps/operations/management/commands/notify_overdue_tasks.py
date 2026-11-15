from django.core.management.base import BaseCommand, CommandError

from apps.operations.services import notify_overdue


class Command(BaseCommand):
    help = (
        "Queue reviewer-only overdue task/report reminders, at most once per local calendar day. No network."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 500:
            raise CommandError("--limit must be between 1 and 500")
        self.stdout.write(f"queued_reminders={notify_overdue(options['limit'])}")
