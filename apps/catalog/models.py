import hashlib
import json
import uuid

from django.core.exceptions import ValidationError
from django.db import models, transaction

from .schema import translations, validate_definition


def title_translations(value):
    translations(value, 200)


class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True, max_length=80)
    title = models.JSONField(
        validators=[title_translations], help_text='{"ru": "...", "kk": "...", "en": "..."}'
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("sort_order", "slug")
        verbose_name = "категория"
        verbose_name_plural = "Категории"

    def __str__(self):
        return self.title.get("ru", self.slug)

    def save(self, *args, **kwargs):
        if not self._state.adding and type(self).objects.filter(pk=self.pk).exclude(slug=self.slug).exists():
            raise ValidationError("Category slug is immutable; create a new category.")
        self.full_clean()
        super().save(*args, **kwargs)


class Service(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="services")
    slug = models.SlugField(unique=True, max_length=80)
    title = models.JSONField(validators=[title_translations])
    summary = models.JSONField(validators=[translations])
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    current_form = models.ForeignKey(
        "FormRevision", null=True, blank=True, on_delete=models.PROTECT, related_name="current_for"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("category__sort_order", "sort_order", "slug")
        verbose_name = "услуга"
        verbose_name_plural = "Услуги"

    def __str__(self):
        return self.title.get("ru", self.slug)

    def clean(self):
        super().clean()
        if self.current_form_id and (
            self.current_form.service_id != self.pk or self.current_form.published_at is None
        ):
            raise ValidationError({"current_form": "Select a published revision of this service."})

    def save(self, *args, **kwargs):
        if not self._state.adding and type(self).objects.filter(pk=self.pk).exclude(slug=self.slug).exists():
            raise ValidationError("Service slug is immutable; create a new service.")
        self.full_clean()
        super().save(*args, **kwargs)


class FormRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="forms")
    version = models.PositiveIntegerField()
    definition = models.JSONField(
        validators=[validate_definition], help_text="См. docs/CATALOG.md и scripts/catalog_form_example.json"
    )
    checksum = models.CharField(max_length=64, editable=False)
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("service__slug", "-version")
        constraints = [
            models.UniqueConstraint(fields=["service", "version"], name="catalog_service_version_unique"),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="catalog_version_positive"),
        ]
        verbose_name = "версия анкеты"
        verbose_name_plural = "Версии анкет"

    def __str__(self):
        return f"{self.service} · v{self.version}" + (
            " · опубликована" if self.published_at else " · черновик"
        )

    @transaction.atomic
    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.select_for_update().get(pk=self.pk)
            if old.published_at:
                raise ValidationError("Published forms are immutable. Create a new draft version.")
            if old.service_id != self.service_id or old.version != self.version:
                raise ValidationError("Service and version are immutable. Create a new draft.")
        validate_definition(self.definition)
        self.checksum = hashlib.sha256(
            json.dumps(
                self.definition, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        self.full_clean()
        super().save(*args, **kwargs)
