import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from rest_framework.exceptions import APIException, ValidationError

from .models import Material
from .upload_handlers import UploadTooLarge


class ScannerUnavailable(APIException):
    status_code = 503
    default_code = "scanner_unavailable"
    default_detail = "File security checks are unavailable. Please try again later."


def scan(path):
    executable = settings.MATERIAL_CLAMSCAN_PATH
    if not executable:
        if settings.ALLOW_UNSCANNED_UPLOADS:
            return Material.Scan.LOCAL_UNSCANNED
        raise ScannerUnavailable()
    try:
        result = subprocess.run(
            [executable, "--no-summary", "--alert-exceeds-max=yes", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScannerUnavailable() from exc
    if result.returncode == 1:
        raise ValidationError({"file": "File rejected by antivirus."}, code="unsafe_file")
    if result.returncode != 0:
        raise ScannerUnavailable()
    return Material.Scan.CLEAN


@contextmanager
def validated_file(upload, kind):
    suffix = Path(upload.name).suffix.lower()
    if suffix not in {".pdf", ".jpg", ".jpeg", ".png"} or (kind == "photo" and suffix == ".pdf"):
        raise ValidationError({"file": "Use PDF, JPEG or PNG; photographs must be JPEG or PNG."})
    if not upload.size:
        raise ValidationError({"file": "Empty file."})
    if upload.size > settings.MATERIAL_MAX_FILE_BYTES:
        raise UploadTooLarge()
    with TemporaryDirectory(prefix="o360-upload-") as folder:
        source, output = Path(folder) / "input", Path(folder) / "output"
        digest = hashlib.sha256()
        size = 0
        with source.open("wb") as target:
            for chunk in upload.chunks():
                size += len(chunk)
                if size > settings.MATERIAL_MAX_FILE_BYTES:
                    raise UploadTooLarge()
                digest.update(chunk)
                target.write(chunk)
        scan_status = scan(source)
        # Do not inherit DATABASE_URL, SMTP passwords, tokens or other application secrets.
        child_env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if k in os.environ}
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("file_worker.py")),
                    str(source),
                    str(output),
                    kind,
                    suffix,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=20,
                env=child_env,
                check=False,
            )
            info = json.loads(result.stdout)
            if result.returncode or "error" in info or not output.is_file():
                raise ValueError()
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(
                {
                    "file": "Invalid, unsupported or overly complex file. Use a flat PDF (no forms/password), JPEG or PNG."
                },
                code="invalid_file",
            ) from exc
        length = output.stat().st_size
        if not 0 < length <= settings.MATERIAL_MAX_FILE_BYTES:
            raise UploadTooLarge()
        # Normalized image is checked as well; PDFs remain byte-for-byte unchanged.
        if info["mime_type"] != "application/pdf" and scan_status == Material.Scan.CLEAN:
            scan(output)
        info.update(
            byte_size=length,
            sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            original_sha256=digest.hexdigest(),
            scan_status=scan_status,
        )
        yield output, info
