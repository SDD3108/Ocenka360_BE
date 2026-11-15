import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.core.backups import create_backup, postgres_dump, safe_path, sqlite_dump, verify_backup
from apps.core.jobs import job_lock, run_batch
from apps.core.network import client_ip
from apps.core.release import heartbeat_ok, release_checks


class BackupTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.media = self.project / "private_media" / "materials"
        self.media.mkdir(parents=True)
        (self.media / "document.pdf").write_bytes(b"synthetic document")
        self.db = self.project / "db.sqlite3"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE example (value TEXT)")
            db.execute("INSERT INTO example VALUES ('synthetic')")
        self.override = override_settings(BASE_DIR=self.project, MATERIAL_STORAGE_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.conn = patch(
            "apps.core.backups.connection",
            SimpleNamespace(vendor="sqlite", settings_dict={"NAME": str(self.db)}),
        )
        self.conn.start()
        self.addCleanup(self.conn.stop)
        self.refs = patch(
            "apps.core.backups.referenced_files",
            return_value=[("document.pdf", 18, hashlib.sha256(b"synthetic document").hexdigest())],
        )
        self.refs.start()
        self.addCleanup(self.refs.stop)

    def bundle(self):
        return create_backup(self.root / "backups", writers_stopped=True)

    def test_roundtrip_restores_db_private_bytes_and_manifest(self):
        bundle = self.bundle()
        result = verify_backup(bundle, destination=self.root / "restored")
        self.assertEqual(result["files"], 2)
        self.assertEqual(
            (self.root / "restored/private_media/materials/document.pdf").read_bytes(), b"synthetic document"
        )
        with sqlite3.connect(self.root / "restored/database.sqlite3") as db:
            self.assertEqual(db.execute("SELECT value FROM example").fetchone(), ("synthetic",))
        if os.name == "posix":
            self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.root / "restored").stat().st_mode & 0o777, 0o700)

    def test_sqlite_backup_includes_committed_wal(self):
        with sqlite3.connect(self.db) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO example VALUES ('wal')")
            db.commit()
            sqlite_dump(self.db, self.root / "copy.sqlite3")
            with sqlite3.connect(self.root / "copy.sqlite3") as copy:
                self.assertEqual(copy.execute("SELECT count(*) FROM example").fetchone()[0], 2)

    def test_requires_offline_acknowledgement(self):
        with self.assertRaises(CommandError):
            create_backup(self.root / "backups")
        self.assertFalse((self.root / "backups").exists())

    def test_destination_cannot_be_in_source_tree(self):
        for path in (self.project / "backups", self.media / "backups", self.root):
            with self.subTest(path=path), self.assertRaises(CommandError):
                create_backup(path, writers_stopped=True)

    def test_missing_or_corrupt_referenced_file_aborts_and_cleans_up(self):
        for data in (None, b"corrupt"):
            path = self.media / "document.pdf"
            if data is None:
                path.unlink()
            else:
                path.write_bytes(data)
            with self.assertRaises(CommandError):
                self.bundle()
            self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_report_revision_reference_is_checked_too(self):
        with patch("apps.core.backups.referenced_files", return_value=[("old-report.pdf", 50, "0" * 64)]):
            with self.assertRaises(CommandError):
                self.bundle()

    def test_no_secrets_or_project_files_are_bundled(self):
        (self.project / ".env").write_text("SECRET=do-not-export")
        with tarfile.open(self.bundle()) as bundle:
            self.assertEqual(
                set(bundle.getnames()),
                {"manifest.json", "database.sqlite3", "private_media/materials/document.pdf"},
            )

    def test_never_overwrites_existing_restore_directory(self):
        dest = self.root / "restored"
        dest.mkdir()
        (dest / "keep.txt").write_text("original")
        with self.assertRaises(CommandError):
            verify_backup(self.bundle(), destination=dest)
        self.assertEqual((dest / "keep.txt").read_text(), "original")

    def test_symlink_source_and_restore_destination_are_rejected(self):
        if os.name == "nt":
            self.skipTest("Windows symlink privilege not assumed")
        (self.media / "link").symlink_to(self.db)
        with self.assertRaises(CommandError):
            self.bundle()
        (self.media / "link").unlink()
        (self.root / "linked").symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(CommandError):
            verify_backup(self.bundle(), destination=self.root / "linked" / "restored")

    def test_database_failure_does_not_publish_partial_archive(self):
        with patch("apps.core.backups.sqlite_dump", side_effect=sqlite3.OperationalError("SECRET")):
            with self.assertRaises(CommandError) as caught:
                self.bundle()
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def modified_bundle(self, kind):
        original = self.bundle()
        altered = self.root / "altered.tar.gz"
        with tarfile.open(original) as source, tarfile.open(altered, "w:gz") as out:
            for member in source.getmembers():
                content = source.extractfile(member).read()
                if kind == "missing" and member.name == "database.sqlite3":
                    continue
                if member.name == "private_media/materials/document.pdf":
                    if kind == "corrupt":
                        content = b"x" * len(content)
                    elif kind == "traversal":
                        member.name = "../escaped"
                    elif kind == "symlink":
                        member.type = tarfile.SYMTYPE
                        member.linkname = "/etc/passwd"
                        member.size = 0
                    elif kind == "hardlink":
                        member.type = tarfile.LNKTYPE
                        member.linkname = "database.sqlite3"
                        member.size = 0
                    elif kind == "extra":
                        member.name = "private_media/materials/unlisted.pdf"
                out.addfile(member, io.BytesIO(content))
                if kind == "duplicate" and member.name == "database.sqlite3":
                    out.addfile(member, io.BytesIO(content))
        return altered

    def test_corruption_traversal_links_missing_extra_and_duplicates_fail_closed(self):
        for kind in ("corrupt", "traversal", "symlink", "hardlink", "missing", "extra", "duplicate"):
            with self.subTest(kind=kind):
                archive = self.modified_bundle(kind)
                dest = self.root / "restore-failed"
                with self.assertRaises(CommandError):
                    verify_backup(archive, destination=dest)
                self.assertFalse(dest.exists())
                self.assertFalse((self.root / "escaped").exists())

    def test_file_count_size_and_manifest_limits(self):
        bundle = self.bundle()
        for name, value in (("MAX_BYTES", 1), ("MAX_FILES", 1), ("MAX_MANIFEST", 2)):
            with patch(f"apps.core.backups.{name}", value), self.assertRaises(CommandError):
                verify_backup(bundle)

    def test_unsafe_names(self):
        for value in ("/etc/passwd", "a/../b", "a//b", "a/./b", "C:/file", "a\\b", "a\x00b", ""):
            with self.subTest(value=value), self.assertRaises(CommandError):
                safe_path(value)

    def test_corrupt_compressed_archive_is_rejected(self):
        path = self.root / "corrupt.tar.gz"
        path.write_bytes(b"not a tar archive")
        with self.assertRaises(CommandError):
            verify_backup(path)

    def test_management_commands_report_verified_summary(self):
        output = io.StringIO()
        call_command(
            "create_backup", destination=str(self.root / "backups"), writers_stopped=True, stdout=output
        )
        archive = output.getvalue().strip()
        output = io.StringIO()
        call_command("verify_backup", archive=archive, stdout=output)
        self.assertEqual(json.loads(output.getvalue())["database"], "sqlite")
        call_command(
            "restore_backup", archive=archive, destination=str(self.root / "restored"), stdout=io.StringIO()
        )


class PostgresDumpTests(SimpleTestCase):
    def test_credentials_are_only_in_child_env_no_shell_or_password_argv(self):
        config = {
            "HOST": "db",
            "NAME": "test",
            "USER": "app",
            "PASSWORD": "private-key",
            "OPTIONS": {"sslmode": "verify-full"},
        }
        with (
            patch("apps.core.backups.shutil.which", return_value="/usr/bin/pg_dump"),
            patch("apps.core.backups.subprocess.run") as run,
        ):
            postgres_dump(config, Path("/tmp/output.dump"))
        args, kwargs = run.call_args
        self.assertNotIn("private-key", str(args))
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "private-key")
        self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
        self.assertNotIn("DJANGO_SECRET_KEY", kwargs["env"])
        self.assertNotIn("shell", kwargs)
        self.assertIn("--format=custom", args[0])

    def test_failure_does_not_expose_stderr_or_credentials(self):
        with (
            patch("apps.core.backups.shutil.which", return_value="/usr/bin/pg_dump"),
            patch(
                "apps.core.backups.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["pg_dump"], stderr=b"SECRET"),
            ),
        ):
            with self.assertRaises(CommandError) as caught:
                postgres_dump({}, Path("unused"))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_unknown_connection_options_rejected(self):
        with self.assertRaises(CommandError):
            postgres_dump({"OPTIONS": {"service": "unknown"}}, Path("unused"))

    def test_missing_pg_dump_rejected(self):
        with patch("apps.core.backups.shutil.which", return_value=None), self.assertRaises(CommandError):
            postgres_dump({}, Path("unused"))


class JobsTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        override = override_settings(RUNTIME_ROOT=self.root, EXPO_PUSH_ENABLED=False, GEMINI_ENABLED=False)
        override.enable()
        self.addCleanup(override.disable)

    def test_default_batch_has_no_external_or_money_commands(self):
        with patch("apps.core.jobs.call_command") as call:
            result = run_batch(5)
        self.assertEqual(
            [c.args[0] for c in call.call_args_list],
            ["cleanup_auth", "cleanup_ai_content", "notify_overdue_tasks"],
        )
        self.assertTrue(result["ok"])
        self.assertTrue(heartbeat_ok(self.root / "jobs.json"))

    @override_settings(EXPO_PUSH_ENABLED=True, GEMINI_ENABLED=True)
    def test_enabled_workers_and_limits(self):
        with patch("apps.core.jobs.call_command") as call:
            run_batch(3)
        self.assertEqual(call.call_args_list[-1].args[0], "process_ai_jobs")
        self.assertEqual(call.call_args_list[-1].kwargs["limit"], 3)
        self.assertEqual(len(call.call_args_list), 6)

    def test_failure_isolated_and_secrets_not_logged(self):
        with patch("apps.core.jobs.call_command", side_effect=[RuntimeError("SECRET"), None, None]) as call:
            result = run_batch(5)
        self.assertFalse(result["ok"])
        self.assertEqual(call.call_count, 3)
        self.assertNotIn("SECRET", (self.root / "jobs.json").read_text())
        self.assertFalse(heartbeat_ok(self.root / "jobs.json"))

    def test_overlapping_batch_refused_and_lock_released(self):
        with job_lock(self.root), self.assertRaises(CommandError):
            run_batch(5)
        with patch("apps.core.jobs.call_command"):
            self.assertTrue(run_batch(5)["ok"])

    def test_stale_future_or_malformed_heartbeat_rejected(self):
        path = self.root / "jobs.json"
        for minutes in (-20, 20):
            path.write_text(
                json.dumps(
                    {
                        "finished_at": (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(),
                        "ok": True,
                        "jobs": {"cleanup": "ok"},
                    }
                )
            )
            self.assertFalse(heartbeat_ok(path))
        path.write_text("invalid")
        self.assertFalse(heartbeat_ok(path))

    def test_invalid_limits_and_failed_command_exit_nonzero(self):
        for limit in (0, 21):
            with self.assertRaises(CommandError):
                run_batch(limit)
        with (
            patch("apps.core.jobs.call_command", side_effect=RuntimeError("SECRET")),
            self.assertRaises(CommandError),
        ):
            call_command("run_background_tasks", stdout=io.StringIO())


class ReleaseTests(TestCase):
    def test_local_preflight_is_diagnostic_and_contains_no_secret_values(self):
        output = io.StringIO()
        call_command("check_release", stdout=output)
        result = json.loads(output.getvalue())
        self.assertFalse(result["configuration_checks_passed"])
        self.assertNotIn(settings.SECRET_KEY, output.getvalue())
        self.assertTrue(result["manual_gates"])

    def test_strict_preflight_blocks_incomplete_deployment(self):
        with self.assertRaises(CommandError):
            call_command("check_release", strict=True, stdout=io.StringIO())

    def test_database_exception_sanitized(self):
        with patch("apps.core.release.MigrationExecutor", side_effect=RuntimeError("PRIVATE_DATABASE_URL")):
            result = release_checks()
        self.assertNotIn("PRIVATE_DATABASE_URL", json.dumps(result))
        self.assertIn("database_queries", [c["check"] for c in result["checks"]])

    def test_corrupt_backup_does_not_pass(self):
        with patch("apps.core.release.verify_backup", side_effect=CommandError("SECRET")):
            result = release_checks("unused")
        self.assertEqual(
            next(c for c in result["checks"] if c["check"] == "recent_backup")["status"], "blocked"
        )

    @override_settings(ALLOW_ORDER_SIMULATION=False)
    def test_release_demo_forbidden_in_production(self):
        with self.assertRaises(CommandError):
            call_command("demo_release", stdout=io.StringIO())


class ProxyTests(SimpleTestCase):
    def test_untrusted_header_ignored(self):
        request = RequestFactory().get("/", REMOTE_ADDR="192.0.2.1", HTTP_X_FORWARDED_FOR="192.0.2.99")
        self.assertEqual(client_ip(request), "192.0.2.1")

    @override_settings(REST_FRAMEWORK={"NUM_PROXIES": 1})
    def test_trusted_single_proxy_ip_and_invalid_chain(self):
        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="2001:db8::1")
        self.assertEqual(client_ip(request), "2001:db8::1")
        request.META["HTTP_X_FORWARDED_FOR"] = "forged, 192.0.2.2"
        self.assertEqual(client_ip(request), "unknown")


class BackupOperatorTests(SimpleTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "offline_backup", settings.BASE_DIR / "deploy/offline_backup.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_stop_order_and_restart_after_backup_failure(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[0] == "runuser":
                raise subprocess.CalledProcessError(1, args)
            return SimpleNamespace(returncode=0)

        with self.assertRaises(subprocess.CalledProcessError):
            self.module.backup(run)
        self.assertEqual(
            [c[2] for c in calls if c[:2] == ["systemctl", "stop"]],
            [self.module.TIMER, self.module.JOBS, self.module.WEB],
        )
        self.assertEqual(
            [c[2] for c in calls if c[:2] == ["systemctl", "start"]], [self.module.WEB, self.module.TIMER]
        )

    def test_previously_inactive_services_not_started(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=3 if "is-active" in args else 0)

        self.module.backup(run)
        self.assertFalse(any(c[:2] == ["systemctl", "start"] for c in calls))
