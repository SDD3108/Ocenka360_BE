"""Root-owned operator helper. Fixed single-host units; not for multi-server deployments.

Install a reviewed copy /usr/local/sbin/ocenka360-backup.py owned by root (0750).
All application code/pg_dump runs as the unprivileged service account.
"""

import os
import subprocess
import sys

TIMER = "ocenka360-jobs.timer"
JOBS = "ocenka360-jobs.service"
WEB = "ocenka360-web.service"


def command(args, *, check=True):
    return subprocess.run(
        args, check=check, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def backup(run=command):
    active = {
        unit: run(["systemctl", "is-active", "--quiet", unit], check=False).returncode == 0
        for unit in (TIMER, WEB)
    }
    try:
        # Timer must be stopped first, otherwise it can start another writer.
        for unit in (TIMER, JOBS, WEB):
            run(["systemctl", "stop", unit])
        run(
            [
                "runuser",
                "-u",
                "ocenka360",
                "--",
                "/usr/bin/env",
                "DJANGO_SETTINGS_MODULE=config.settings.production",
                "/opt/ocenka360/venv/bin/python",
                "/opt/ocenka360/backend/manage.py",
                "create_backup",
                "--destination",
                "/var/backups/ocenka360",
                "--writers-stopped",
            ]
        )
    finally:
        # Try every restart even if one fails. Do not activate previously disabled units.
        failed = False
        for unit in (WEB, TIMER):
            if active[unit]:
                try:
                    run(["systemctl", "start", unit])
                except subprocess.SubprocessError:
                    failed = True
        if failed:
            raise RuntimeError("Service restart failed; operator action required.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("Run this root-owned operator helper as root.")
    # A kernel lock also covers overlapping manual invocations; released on crash.
    import fcntl

    lock = open("/run/lock/ocenka360-backup.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another backup operator is already running.")
    try:
        backup()
    except Exception:
        sys.exit("Backup/restart failed. Inspect unit states; no automatic money retry is performed.")
    print("Backup completed; previously active web/timer restarted. Verify newest bundle and off-site copy.")
