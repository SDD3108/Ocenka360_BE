import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower

from .managers import UserManager


class User(AbstractUser):
    class Role(models.TextChoices):
        CLIENT = "client", "Клиент"
        APPRAISER = "appraiser", "Оценщик"
        ADMIN = "admin", "Администратор"

    class Language(models.TextChoices):
        RU = "ru", "Русский"
        KK = "kk", "Қазақша"
        EN = "en", "English"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField("Email", unique=True)
    role = models.CharField("Роль", max_length=16, choices=Role.choices, default=Role.CLIENT)
    preferred_language = models.CharField("Язык", max_length=2, choices=Language.choices, default=Language.RU)
    phone_number = models.CharField(
        "Телефон",
        max_length=16,
        blank=True,
        validators=[RegexValidator(r"^\+[1-9]\d{7,14}$", "Укажите телефон в формате +77001234567.")],
    )
    email_verified_at = models.DateTimeField("Email подтверждён", null=True, blank=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    objects = UserManager()
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"
        ordering = ["-date_joined"]
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique"),
            models.CheckConstraint(
                condition=models.Q(role__in=["client", "appraiser", "admin"]), name="accounts_valid_role"
            ),
            models.CheckConstraint(
                condition=models.Q(preferred_language__in=["ru", "kk", "en"]), name="accounts_valid_language"
            ),
            models.CheckConstraint(
                condition=models.Q(is_staff=False) | models.Q(role="admin"), name="accounts_staff_is_admin"
            ),
            models.CheckConstraint(
                condition=models.Q(is_superuser=False) | models.Q(role="admin", is_staff=True),
                name="accounts_superuser_is_staff_admin",
            ),
        ]

    def clean(self):
        super().clean()
        self.email = type(self).objects.normalize_email(self.email)
        errors = {}
        if self.is_staff and self.role != self.Role.ADMIN:
            errors["role"] = "Доступ к Django Admin разрешён только роли Администратор."
        if self.is_superuser and (self.role != self.Role.ADMIN or not self.is_staff):
            errors["is_superuser"] = "Суперпользователь должен быть администратором с доступом к панели."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.email = type(self).objects.normalize_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email
