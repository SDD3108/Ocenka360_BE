from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    """Case-insensitive password login for the administrator, not public client auth."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        user_model = get_user_model()
        email = username or kwargs.get(user_model.USERNAME_FIELD)
        if not email or password is None:
            return None
        try:
            user = user_model.objects.get(email__iexact=user_model.objects.normalize_email(email))
        except user_model.DoesNotExist:
            user_model().set_password(password)  # Perform hashing on the unknown-user path too.
        else:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        return None
