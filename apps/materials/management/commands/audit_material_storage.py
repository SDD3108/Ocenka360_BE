"""Offline maintenance: never delete a file referenced by an active material record."""

import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.materials.models import Material
from apps.materials.storage import PrivateStorage
from apps.reports.models import Report


class Command(BaseCommand):
    help = (
        "Count missing/orphan private files. Dry run unless --delete-orphans; stop uploads before deletion."
    )

    def add_arguments(self, parser):
        parser.add_argument("--delete-orphans", action="store_true")
        parser.add_argument("--min-age-hours", type=int, default=24)

    def handle(self, *args, **options):
        if options["min_age_hours"] < 24:
            raise CommandError("Minimum orphan age is 24 hours; stop uploads before deletion.")
        storage = PrivateStorage()
        root = Path(storage.location)
        active = set(Material.objects.filter(withdrawn_at__isnull=True).values_list("storage_key", flat=True))
        active.update(Report.objects.values_list("storage_key", flat=True))
        missing = sum(not storage.exists(key) for key in active)
        cutoff = time.time() - options["min_age_hours"] * 3600
        orphans = deleted = 0
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_symlink() or not path.is_file():
                    continue
                key = path.relative_to(root).as_posix()
                if key in active or path.stat().st_mtime > cutoff:
                    continue
                # Re-check DB immediately before deletion. Still run offline: no distributed FS/DB atomicity.
                if (
                    Material.objects.filter(storage_key=key, withdrawn_at__isnull=True).exists()
                    or Report.objects.filter(storage_key=key).exists()
                ):
                    continue
                orphans += 1
                if options["delete_orphans"]:
                    storage.delete(key)
                    deleted += 1
        self.stdout.write(f"missing={missing} eligible_orphans={orphans} deleted={deleted}")
        if missing:
            self.stdout.write(
                "Restore missing files from a consistent backup; never recreate customer documents."
            )
