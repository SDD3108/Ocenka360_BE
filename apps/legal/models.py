import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class LegalDocument(models.Model):
    class Kind(models.TextChoices):
        TERMS = "terms", "Пользовательское соглашение"
        PRIVACY = "privacy", "Политика конфиденциальности"
        PERSONAL_DATA = "personal_data", "Согласие на обработку данных"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    version = models.CharField(max_length=40)
    language = models.CharField(max_length=2, choices=settings.LANGUAGES)
    title = models.CharField(max_length=200)
    content = models.TextField()
    checksum = models.CharField(max_length=64, editable=False)
    is_draft = models.BooleanField(default=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "language", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "version", "language"], name="legal_version_unique"),
            models.UniqueConstraint(
                fields=["kind", "language"], condition=models.Q(is_active=True), name="legal_active_unique"
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            immutable = ("kind", "version", "language", "title", "content", "is_draft")
            if any(getattr(self, field) != getattr(original, field) for field in immutable):
                raise ValidationError("Создайте новую версию документа; существующая версия неизменяема.")
        self.checksum = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.kind} / {self.language} / {self.version}"


class ConsentAcceptance(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="consents")
    document = models.ForeignKey(LegalDocument, on_delete=models.PROTECT)
    accepted_at = models.DateTimeField(auto_now_add=True)
    document_checksum = models.CharField(max_length=64)
    adult_confirmed = models.BooleanField(default=True)

    class Meta:
        ordering = ["-accepted_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "document"], name="consent_user_document_unique")
        ]
