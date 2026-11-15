"""Private, read-only preflight. Never claims external delivery, legal approval or bank certification."""

import json
import os
import shutil
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.core.backups import verify_backup
from apps.legal.models import LegalDocument


def heartbeat_ok(path):
    try:
        if path.stat().st_size > 16_384:
            return False
        data = json.loads(path.read_text())
        age = (datetime.now(UTC) - datetime.fromisoformat(data["finished_at"])).total_seconds()
        return (
            data["ok"] is True
            and bool(data["jobs"])
            and all(v == "ok" for v in data["jobs"].values())
            and 0 <= age <= 900
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def release_checks(backup_archive=None):
    checks = []

    def add(name, passed, detail):
        checks.append({"check": name, "status": "ok" if passed else "blocked", "detail": detail})

    add(
        "production_settings",
        settings.SETTINGS_MODULE == "config.settings.production"
        and not settings.DEBUG
        and not settings.ALLOW_UNSCANNED_UPLOADS
        and not settings.ALLOW_ORDER_SIMULATION
        and not settings.ALLOW_DRAFT_LEGAL_DOCUMENTS,
        "Production settings; no simulation, draft legal or scanner bypass.",
    )
    add(
        "postgresql",
        connection.vendor == "postgresql",
        "Production requires PostgreSQL; SQLite is for local work.",
    )
    hosts = settings.ALLOWED_HOSTS
    add(
        "public_hostname",
        bool(hosts)
        and all(h not in {"localhost", "127.0.0.1", "*"} and not h.endswith(".invalid") for h in hosts),
        "Configure the actual hostname and TLS independently.",
    )
    add(
        "company",
        bool(settings.COMPANY_LEGAL_NAME.strip()),
        "Exact legal company name must be supplied by the owner.",
    )
    sender = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    add(
        "smtp_configuration",
        settings.EMAIL_BACKEND == "django.core.mail.backends.smtp.EmailBackend"
        and bool(settings.EMAIL_HOST and settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD)
        and bool(settings.EMAIL_USE_TLS or settings.EMAIL_USE_SSL)
        and "@" in sender
        and not sender.endswith(".invalid"),
        "Configuration only; actual OTP delivery must be tested separately.",
    )
    scanner = settings.MATERIAL_CLAMSCAN_PATH
    add(
        "scanner_executable",
        bool(scanner and shutil.which(scanner)),
        "Executable available; signature freshness and real scan still need a staging test.",
    )
    root = Path(settings.MATERIAL_STORAGE_ROOT)
    add(
        "private_storage",
        root.is_dir() and not root.is_symlink() and os.access(root, os.R_OK | os.W_OK | os.X_OK),
        "Private persistent storage must exist and be writable by the service account.",
    )
    add(
        "worker_heartbeat",
        heartbeat_ok(Path(settings.RUNTIME_ROOT) / "jobs.json"),
        "A complete successful background batch is required within 15 minutes.",
    )
    try:
        executor = MigrationExecutor(connection)
        add(
            "migrations",
            not executor.migration_plan(executor.loader.graph.leaf_nodes()),
            "All migrations applied to the selected database.",
        )
        pairs = set(
            LegalDocument.objects.filter(is_active=True, is_draft=False).values_list("kind", "language")
        )
        required = {
            (kind, language) for kind in LegalDocument.Kind.values for language, _ in settings.LANGUAGES
        }
        add(
            "published_legal",
            required <= pairs,
            "Final active terms/privacy/data consent in RU, KK and EN; content still requires owner/legal review.",
        )
    except Exception:
        add("database_queries", False, "Database or schema unavailable; connection details hidden.")
    backup_ok = False
    if backup_archive:
        try:
            verified = verify_backup(backup_archive)
            age = datetime.now(UTC).timestamp() - Path(backup_archive).stat().st_mtime
            backup_ok = (
                0 <= age <= 48 * 3600
                and verified["version"] == settings.APP_VERSION
                and verified["database"] == connection.vendor
            )
        except Exception:
            pass
    add(
        "recent_backup",
        backup_ok,
        "Pass --backup-archive for a verified bundle under 48 hours old matching app version/database engine. A separate restore drill is required.",
    )
    return {
        "version": settings.APP_VERSION,
        "configuration_checks_passed": all(c["status"] == "ok" for c in checks),
        "checks": checks,
        "manual_gates": [
            "Staging restore + real PostgreSQL tests",
            "TLS, firewall, monitoring and off-site encrypted backups",
            "Legal text, retention and actual service tariffs approved",
            "SMTP OTP and configured Expo/Gemini/bank integrations tested separately",
            "Fiscalization and official accident software are not connected",
        ],
    }
