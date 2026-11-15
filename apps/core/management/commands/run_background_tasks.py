import json

from django.core.management.base import BaseCommand, CommandError

from apps.core.jobs import run_batch


class Command(BaseCommand):
    help = "One bounded batch: auth/AI cleanup, reminders, configured push/AI. No payment/refund dispatch."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=5)

    def handle(self, *args, **options):
        result = run_batch(options["limit"])
        self.stdout.write(json.dumps(result))
        if not result["ok"]:
            raise CommandError(
                "Background batch has failures; inspect job status, do not blindly replay uncertain sends."
            )
