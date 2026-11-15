"""Offline, private bundles. Checksums detect corruption, not malicious provenance."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid
import zlib
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.management.base import CommandError
from django.db import connection

MAX_FILES = 100_000
MAX_MANIFEST = 16 * 1024 * 1024
MAX_BYTES = 100 * 1024**3


def safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 500:
        raise CommandError("Invalid backup member path.")
    p = PurePosixPath(value)
    if (
        p.is_absolute()
        or any(x in {"", ".", ".."} for x in value.split("/"))
        or "\\" in value
        or ":" in value
    ):
        raise CommandError("Unsafe backup member path.")
    if any(ord(c) < 32 for c in value):
        raise CommandError("Invalid backup member path.")
    return value


def no_symlinks(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise CommandError("Symlinks are not supported for backup paths.")
    return path


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sqlite_dump(source, target):
    source = no_symlinks(source)
    if not source.is_file():
        raise CommandError("A file-backed SQLite database is required.")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise CommandError("SQLite integrity check failed.")


def postgres_dump(config, target):
    """No password/URL on argv; reject unsupported libpq options rather than silently omit them."""
    options = config.get("OPTIONS", {})
    supported = {
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
    }
    if set(options) - set(supported):
        raise CommandError("Backup does not support these PostgreSQL OPTIONS; review configuration.")
    executable = shutil.which("pg_dump")
    if not executable:
        raise CommandError("Install PostgreSQL 17 client tools (pg_dump).")
    environment = {"PATH": os.defpath, "PGCONNECT_TIMEOUT": "10", "PGAPPNAME": "ocenka360-backup"}
    for key, field in {
        "PGHOST": "HOST",
        "PGPORT": "PORT",
        "PGDATABASE": "NAME",
        "PGUSER": "USER",
        "PGPASSWORD": "PASSWORD",
    }.items():
        environment[key] = str(config.get(field) or "")
    for key, value in options.items():
        environment[supported[key]] = str(value)
    try:
        subprocess.run(
            [
                executable,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--no-password",
                "--lock-wait-timeout=30s",
                "--file",
                str(target),
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=1800,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise CommandError("pg_dump failed; database credentials and provider output are hidden.") from None


def referenced_files():
    from apps.materials.models import Material
    from apps.reports.models import Report

    for query in (Material.objects.filter(withdrawn_at__isnull=True), Report.objects.all()):
        yield from query.values_list("storage_key", "byte_size", "sha256").iterator()


def create_backup(destination, *, writers_stopped=False):
    if not writers_stopped:
        raise CommandError(
            "Stop ALL web/workers/manual writers, then pass --writers-stopped. This flag is an acknowledgement, not a lock."
        )
    destination = no_symlinks(destination)
    media = no_symlinks(settings.MATERIAL_STORAGE_ROOT)
    project = Path(settings.BASE_DIR).resolve()
    if (
        destination.is_relative_to(project)
        or destination.is_relative_to(media)
        or media.is_relative_to(destination)
    ):
        raise CommandError("Backup destination must be outside the project and private storage tree.")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and destination.stat().st_mode & 0o077:
        raise CommandError("Backup directory must be private (chmod 700).")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive = destination / f"ocenka360-{stamp}-{uuid.uuid4().hex}.tar.gz"
    partial = archive.with_suffix(".partial")
    try:
        with tempfile.TemporaryDirectory(prefix=".backup-", dir=destination) as work:
            work = Path(work)
            engine = connection.vendor
            if engine not in {"sqlite", "postgresql"}:
                raise CommandError("Unsupported backup database engine.")
            name = "database.sqlite3" if engine == "sqlite" else "database.dump"
            database = work / name
            if engine == "sqlite":
                sqlite_dump(connection.settings_dict["NAME"], database)
            else:
                postgres_dump(connection.settings_dict, database)
            os.chmod(database, 0o600)
            sources = {name: database}
            if media.exists():
                for path in sorted(media.rglob("*")):
                    no_symlinks(path)
                    if path.is_dir():
                        continue
                    if not path.is_file():
                        raise CommandError("Private storage contains a special file.")
                    sources[safe_path("private_media/materials/" + path.relative_to(media).as_posix())] = path
                    if len(sources) > MAX_FILES:
                        raise CommandError("Backup file count limit exceeded.")
            entries = {key: {"size": p.stat().st_size, "sha256": digest(p)} for key, p in sources.items()}
            if sum(e["size"] for e in entries.values()) > MAX_BYTES:
                raise CommandError("Backup size limit exceeded (100 GiB).")
            for key, size, sha in referenced_files():
                entry = entries.get("private_media/materials/" + safe_path(key))
                if not entry or entry != {"size": size, "sha256": sha}:
                    raise CommandError("Referenced private file is missing or corrupt; backup aborted.")
            manifest = {
                "format": 1,
                "version": settings.APP_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "database": engine,
                "files": entries,
            }
            data = json.dumps(manifest, sort_keys=True).encode()
            if len(data) > MAX_MANIFEST:
                raise CommandError("Backup manifest too large.")
            manifest_path = work / "manifest.json"
            manifest_path.write_bytes(data)
            with partial.open("xb") as raw:
                os.chmod(partial, 0o600)
                with tarfile.open(fileobj=raw, mode="w:gz") as bundle:
                    for key, path in {"manifest.json": manifest_path, **sources}.items():
                        info = bundle.gettarinfo(str(path), arcname=key)
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mode = 0o600
                        with path.open("rb") as stream:
                            bundle.addfile(info, stream)
                raw.flush()
                os.fsync(raw.fileno())
            verify_backup(partial)
            partial.rename(archive)
            return archive
    except (CommandError, OSError, sqlite3.Error, tarfile.TarError, zlib.error, ValueError):
        partial.unlink(missing_ok=True)
        raise CommandError(
            "Backup failed. Check storage, database and referenced files; no complete bundle was published."
        ) from None


def _manifest(data):
    try:
        result = json.loads(data)
        if result["format"] != 1 or result["database"] not in {"sqlite", "postgresql"}:
            raise ValueError
        entries = result["files"]
        if not isinstance(entries, dict) or not 1 <= len(entries) <= MAX_FILES:
            raise ValueError
        database = "database.sqlite3" if result["database"] == "sqlite" else "database.dump"
        if database not in entries:
            raise ValueError
        total = 0
        for key, entry in entries.items():
            safe_path(key)
            if key != database and not key.startswith("private_media/materials/"):
                raise ValueError
            if (
                type(entry["size"]) is not int
                or entry["size"] < 0
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
            ):
                raise ValueError
            total += entry["size"]
        if total > MAX_BYTES:
            raise ValueError
        return result
    except (TypeError, KeyError, ValueError):
        raise CommandError("Invalid or oversized backup manifest.") from None


def verify_backup(archive, *, destination=None):
    """Stream, never extractall. Restore creates only a NEW directory; removes its own partial output on failure."""
    archive = no_symlinks(archive)
    target = no_symlinks(destination) if destination is not None else None
    created = False
    try:
        if target is not None:
            target.mkdir(mode=0o700, parents=False, exist_ok=False)
            created = True
        with tarfile.open(archive, "r|gz") as bundle:
            first = next(iter(bundle), None)
            if not first or first.name != "manifest.json" or not first.isfile() or first.size > MAX_MANIFEST:
                raise CommandError("Backup must begin with a bounded regular manifest.")
            manifest_data = bundle.extractfile(first).read(MAX_MANIFEST + 1)
            manifest = _manifest(manifest_data)
            seen = set()
            for item in bundle:
                # Streaming TarFile iterators yield their first member again.
                if item is first:
                    continue
                key = safe_path(item.name)
                expected = manifest["files"].get(key)
                if (
                    not item.isfile()
                    or item.issparse()
                    or key in seen
                    or not expected
                    or item.size != expected["size"]
                ):
                    raise CommandError("Unexpected, duplicate or unsafe backup member.")
                seen.add(key)
                output = None
                if target is not None:
                    path = target / key
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    output = path.open("xb")
                    os.chmod(path, 0o600)
                sha = hashlib.sha256()
                count = 0
                try:
                    stream = bundle.extractfile(item)
                    while chunk := stream.read(1024 * 1024):
                        count += len(chunk)
                        sha.update(chunk)
                        if output:
                            output.write(chunk)
                finally:
                    if output:
                        output.close()
                if count != expected["size"] or sha.hexdigest() != expected["sha256"]:
                    raise CommandError("Backup checksum mismatch.")
            if seen != set(manifest["files"]):
                raise CommandError("Backup is missing files.")
            if target is not None:
                path = target / "manifest.json"
                path.write_bytes(manifest_data)
                os.chmod(path, 0o600)
                if manifest["database"] == "sqlite":
                    with sqlite3.connect((target / "database.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
                        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                            raise CommandError("Restored SQLite integrity check failed.")
            return {
                "database": manifest["database"],
                "files": len(seen),
                "version": manifest.get("version", "unknown"),
            }
    except (CommandError, OSError, sqlite3.Error, tarfile.TarError, zlib.error, EOFError, ValueError):
        if created:
            shutil.rmtree(target)
        raise CommandError(
            "Backup verification/restore failed; existing destinations are never overwritten."
        ) from None
