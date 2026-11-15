from django.core.management.base import BaseCommand, CommandError

from apps.assistance.worker import process


class Command(BaseCommand):
    help = "Process a bounded AI batch. Explicitly configured Gemini may incur external API charges. No automatic retries."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=5)

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 20:
            raise CommandError("--limit must be 1–20")
        self.stdout.write(f"processed_ai_jobs={process(options['limit'])}")
