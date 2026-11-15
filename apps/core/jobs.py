"""A bounded single-host runner; provider workers retain their own transaction/claim rules."""

import io
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError


@contextmanager
def job_lock(root):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "jobs.lock"
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        if os.name == "nt":
            import msvcrt

            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise CommandError("Another background batch is already running.") from None
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError("Another background batch is already running.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def write_heartbeat(root, result):
    fd, temporary = tempfile.mkstemp(prefix=".jobs-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, Path(root) / "jobs.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def run_batch(limit):
    if not 1 <= limit <= 20:
        raise CommandError("--limit must be 1–20.")
    with job_lock(settings.RUNTIME_ROOT):
        jobs = [("cleanup_auth", {}), ("cleanup_ai_content", {}), ("notify_overdue_tasks", {"limit": 100})]
        if settings.EXPO_PUSH_ENABLED:
            jobs += [("dispatch_notifications", {"limit": limit}), ("check_push_receipts", {"limit": limit})]
        if settings.GEMINI_ENABLED:
            jobs.append(("process_ai_jobs", {"limit": limit}))
        results = {}
        for name, arguments in jobs:
            try:
                call_command(name, **arguments, stdout=io.StringIO(), stderr=io.StringIO())
                results[name] = "ok"
            except Exception:
                # Provider/DB exception messages can contain secrets or customer data.
                results[name] = "failed"
        result = {
            "finished_at": datetime.now(UTC).isoformat(),
            "ok": "failed" not in results.values(),
            "jobs": results,
        }
        write_heartbeat(settings.RUNTIME_ROOT, result)
        return result
