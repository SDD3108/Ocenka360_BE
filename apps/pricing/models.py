import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models.functions import Round, Upper

from apps.orders.models import AppendOnlyRecord

MAX_MONEY = Decimal("999999999.99")


def money(default=None, nullable=False):
    return models.DecimalField(
        max_digits=11,
        decimal_places=2,
        default=default,
        null=nullable,
        blank=nullable,
        validators=[MinValueValidator(Decimal("0.00")), MaxValueValidator(MAX_MONEY)],
    )


class TariffRevision(models.Model):
    class Mode(models.TextChoices):
        MANUAL = "manual", "Индивидуальный расчёт"
        FLAT = "flat", "Базовая стоимость"
        AREA = "area", "База и превышение включённой площади"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    service = models.ForeignKey("catalog.Service", on_delete=models.PROTECT, related_name="tariffs")
    version = models.PositiveIntegerField(default=1)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.MANUAL)
    base_amount = money(nullable=True)
    included_area = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    per_sqm = money(nullable=True)
    urgent_surcharge = money(nullable=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_tariffs",
    )
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_tariffs",
    )
    published_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("service__slug", "-version")
        constraints = [
            models.UniqueConstraint(fields=["service", "version"], name="tariff_service_version_unique"),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="tariff_version_positive"),
        ]
        verbose_name = "версия тарифа"
        verbose_name_plural = "Версии тарифов"

    def __str__(self):
        return f"{self.service} · v{self.version} · {self.get_mode_display()}"

    def clean(self):
        if self.mode == self.Mode.MANUAL:
            if any(
                v is not None
                for v in (self.base_amount, self.included_area, self.per_sqm, self.urgent_surcharge)
            ):
                raise ValidationError("Manual tariffs must not contain invented amounts.")
        else:
            if self.base_amount is None or self.base_amount <= 0:
                raise ValidationError({"base_amount": "A positive base amount is required."})
            if self.mode == self.Mode.AREA:
                if self.included_area is None or self.per_sqm is None or self.per_sqm <= 0:
                    raise ValidationError("Area tariffs require included_area and a positive per_sqm.")
            elif self.included_area is not None or self.per_sqm is not None:
                raise ValidationError("Only area tariffs may specify area parameters.")

    @transaction.atomic
    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.select_for_update().get(pk=self.pk)
            if old.published_at:
                raise ValidationError("Published tariffs are immutable. Create a new revision.")
            if old.service_id != self.service_id or old.version != self.version:
                raise ValidationError("Service and version cannot change.")
        self.full_clean()
        return super().save(*args, **kwargs)


class ServiceTariff(models.Model):
    service = models.OneToOneField("catalog.Service", on_delete=models.PROTECT, primary_key=True)
    current = models.ForeignKey(TariffRevision, on_delete=models.PROTECT)


class PromoCode(models.Model):
    class Kind(models.TextChoices):
        FIXED = "fixed", "Сумма в тенге"
        PERCENT = "percent", "Процент"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(
        max_length=32, unique=True, validators=[RegexValidator(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")]
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    value = money()
    minimum_subtotal = money(default=Decimal("0.00"))
    maximum_discount = money(nullable=True)
    service = models.ForeignKey("catalog.Service", null=True, blank=True, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    max_uses = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    per_user_limit = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(1000)]
    )
    is_active = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(Upper("code"), name="promo_code_upper_unique"),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")), name="promo_period_valid"
            ),
            models.CheckConstraint(condition=models.Q(value__gt=0), name="promo_value_positive"),
            models.CheckConstraint(
                condition=~models.Q(kind="percent") | models.Q(value__lte=Decimal("99.99")),
                name="promo_percent_limit",
            ),
        ]
        verbose_name = "промокод"
        verbose_name_plural = "Промокоды"

    def __str__(self):
        return self.code

    def clean(self):
        if self.value is not None and (
            self.value <= 0 or self.kind == self.Kind.PERCENT and self.value > Decimal("99.99")
        ):
            raise ValidationError({"value": "Positive amount required; percentage must be <= 99.99."})
        if self.maximum_discount is not None and self.maximum_discount <= 0:
            raise ValidationError({"maximum_discount": "Must be positive."})
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({"ends_at": "Must be after starts_at."})

    @transaction.atomic
    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        if not self._state.adding:
            old = type(self).objects.select_for_update().get(pk=self.pk)
            for field in self._meta.concrete_fields:
                if field.name not in {"is_active", "version"} and getattr(old, field.attname) != getattr(
                    self, field.attname
                ):
                    raise ValidationError("Promo terms are immutable; create another code.")
            if self.version != old.version:
                raise ValidationError("Promo changed; reload it.")
            self.version += 1
        self.full_clean()
        return super().save(*args, **kwargs)


class PromoSelection(models.Model):
    order = models.OneToOneField(
        "orders.Order", primary_key=True, on_delete=models.PROTECT, related_name="promo_selection"
    )
    promo = models.ForeignKey(PromoCode, null=True, on_delete=models.PROTECT)


class PriceQuote(models.Model):
    class State(models.TextChoices):
        OFFERED = "offered", "Предложена"
        ACCEPTED = "accepted", "Согласована"
        DECLINED = "declined", "Отклонена клиентом"
        WITHDRAWN = "withdrawn", "Отозвана оценщиком"
        SUPERSEDED = "superseded", "Заменена новой"
        INVALIDATED = "invalidated", "Условия изменились"
        CANCELLED = "cancelled", "Заявка закрыта"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="price_quotes")
    revision = models.PositiveIntegerField()
    client_request_id = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    issued_order_version = models.PositiveIntegerField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issued_quotes"
    )
    lines = models.JSONField()
    context_snapshot = models.JSONField()
    promo = models.ForeignKey(PromoCode, null=True, on_delete=models.PROTECT)
    promo_snapshot = models.JSONField(default=dict, blank=True)
    subtotal = money()
    appraisal_subtotal = money()
    discount = money(default=Decimal("0.00"))
    total = money()
    terms = models.TextField(max_length=2000)
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=15, choices=State.choices, default=State.OFFERED)
    decided_at = models.DateTimeField(null=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="quote_decisions"
    )
    decision_comment = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-revision",)
        constraints = [
            models.UniqueConstraint(fields=["order", "revision"], name="quote_order_revision_unique"),
            models.UniqueConstraint(fields=["order", "client_request_id"], name="quote_order_request_unique"),
            models.UniqueConstraint(
                fields=["order"], condition=models.Q(state="offered"), name="quote_one_offer"
            ),
            models.UniqueConstraint(
                fields=["order"], condition=models.Q(state="accepted"), name="quote_one_accepted"
            ),
            models.CheckConstraint(condition=models.Q(total__gt=0), name="quote_total_positive"),
            models.CheckConstraint(
                condition=models.Q(discount__gte=0) & models.Q(discount__lte=models.F("appraisal_subtotal")),
                name="quote_discount_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(total=Round(models.F("subtotal") - models.F("discount"), precision=2)),
                name="quote_total_matches",
            ),
        ]
        verbose_name = "предложение цены"
        verbose_name_plural = "Предложения цены"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            old = type(self).objects.get(pk=self.pk)
            for field in self._meta.concrete_fields:
                if field.name not in {"state", "decided_at", "decided_by", "decision_comment"} and getattr(
                    old, field.attname
                ) != getattr(self, field.attname):
                    raise ValidationError("Quoted terms and amounts are immutable.")
        return super().save(*args, **kwargs)


class PromoRedemption(models.Model):
    quote = models.OneToOneField(PriceQuote, on_delete=models.PROTECT)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT)
    promo = models.ForeignKey(PromoCode, on_delete=models.PROTECT, related_name="redemptions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(released_at__isnull=True),
                name="one_order_promo_redemption",
            )
        ]
        indexes = [models.Index(fields=["promo", "user", "released_at"], name="promo_user_usage_idx")]


class PricingAudit(AppendOnlyRecord):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    target_id = models.UUIDField()
    created_at = models.DateTimeField(auto_now_add=True)


TARIFF_MODE_CHOICES = TariffRevision.Mode.choices
PROMO_KIND_CHOICES = PromoCode.Kind.choices
QUOTE_STATE_CHOICES = PriceQuote.State.choices


class SubmissionEstimate(AppendOnlyRecord):
    submission = models.OneToOneField(
        "orders.OrderSubmission", on_delete=models.PROTECT, related_name="price_estimate"
    )
    snapshot = models.JSONField()
