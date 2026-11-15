import io

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Run existing rollback-only synthetic workflow demos locally; never call real banks/AI/push."

    def handle(self, *args, **options):
        if not settings.ALLOW_ORDER_SIMULATION:
            raise CommandError("Synthetic release demo is local/test only.")
        for name, arguments in (
            ("demo_orders", {}),
            ("demo_orders", {"inspection": True}),
            ("demo_payments", {}),
            ("demo_communications", {}),
            ("demo_reports", {}),
            ("demo_assistance", {}),
        ):
            output = io.StringIO()
            call_command(name, **arguments, stdout=output)
            if "PASS" not in output.getvalue():
                raise CommandError(f"{name} did not report PASS.")
            self.stdout.write(f"{name}: PASS")
        self.stdout.write(
            "PASS: synthetic workflows only; real external integrations still need staging verification."
        )
