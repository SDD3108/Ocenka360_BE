"""Synthetic files for workflow fixtures; upload validation is exercised in test_materials.py."""

import hashlib
import io
import uuid

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from pypdf import PdfWriter

from apps.materials.guides import requirements
from apps.materials.models import Material
from apps.materials.storage import PrivateStorage


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (24, 32), "green").save(stream, format="PNG")
    return stream.getvalue()


def pdf_upload():
    return SimpleUploadedFile("test.pdf", pdf_bytes(), content_type="application/pdf")


def photo_upload():
    return SimpleUploadedFile("test.png", image_bytes(), content_type="image/png")


def seed_test_materials(order):
    """Direct model fixtures preserve old workflow version assertions; never used by production code."""
    needed = requirements(order, "ru")
    for group, kind in (("documents", "document"), ("photos", "photo")):
        for slot in needed[group]:
            if not slot["required"] or order.materials.filter(kind=kind, slot_key=slot["key"]).exists():
                continue
            raw = pdf_bytes() if kind == "document" else image_bytes()
            key = PrivateStorage().save(f"{order.pk}/{uuid.uuid4()}.fixture", ContentFile(raw))
            digest = hashlib.sha256(raw).hexdigest()
            Material.objects.create(
                order=order,
                uploaded_by=order.owner,
                client_upload_id=uuid.uuid4(),
                kind=kind,
                slot_key=slot["key"],
                source="client",
                storage_key=key,
                mime_type="application/pdf" if kind == "document" else "image/png",
                byte_size=len(raw),
                sha256=digest,
                original_sha256=digest,
                scan_status="local_unscanned",
            )
