from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email):
        # Project policy: the complete email is case-insensitive.
        return super().normalize_email(email.strip() if email else "").lower()

    def create_user(self, email, password=None, **extra_fields):
        if not email or not email.strip():
            raise ValueError("Email is required.")
        user = self.model(email=self.normalize_email(email), **extra_fields)
        user.set_password(password)  # None => unusable password (future OTP clients).
        user.full_clean()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("role", "admin")
        if any(extra_fields.get(key) is not True for key in ("is_staff", "is_superuser", "is_active")):
            raise ValueError("Superuser must be active, staff and superuser.")
        if extra_fields["role"] != "admin":
            raise ValueError("Superuser must have the admin role.")
        if not password:
            raise ValueError("Superuser requires a password.")
        return self.create_user(email, password, **extra_fields)
