from rest_framework import serializers

from apps.legal.services import requires_consent

from .models import AuthSession, User


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = set(data) - {name for name, field in self.fields.items() if not field.read_only}
            if unknown:
                raise serializers.ValidationError(
                    {name: "This field is not writable." for name in sorted(unknown)}
                )
        return super().to_internal_value(data)


class RequestCodeSerializer(StrictSerializer):
    email = serializers.EmailField(max_length=254)
    language = serializers.ChoiceField(choices=User.Language.choices, default="ru")

    def validate_email(self, value):
        return User.objects.normalize_email(value)


class CodeRequestedSerializer(serializers.Serializer):
    challenge_id = serializers.UUIDField()
    expires_in = serializers.IntegerField()
    resend_after = serializers.IntegerField()
    message = serializers.CharField()


class VerifyCodeSerializer(StrictSerializer):
    challenge_id = serializers.UUIDField()
    code = serializers.RegexField(r"^[0-9]{6}$", trim_whitespace=True)
    device_name = serializers.CharField(max_length=80, required=False, default="", allow_blank=True)


class RefreshSerializer(StrictSerializer):
    refresh = serializers.CharField(max_length=2048, trim_whitespace=False)


class CurrentUserSerializer(StrictSerializer, serializers.ModelSerializer):
    requires_consent = serializers.SerializerMethodField()
    phone_verified = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "phone_verified",
            "preferred_language",
            "role",
            "email_verified_at",
            "adult_confirmed_at",
            "date_joined",
            "requires_consent",
        )
        read_only_fields = (
            "id",
            "email",
            "role",
            "email_verified_at",
            "adult_confirmed_at",
            "date_joined",
            "requires_consent",
            "phone_verified",
        )

    def get_requires_consent(self, obj) -> bool:
        return requires_consent(obj)

    def get_phone_verified(self, obj) -> bool:
        # A manually entered contact number is NOT a verified login factor.
        return False


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()
    token_type = serializers.CharField()
    access_expires_in = serializers.IntegerField()
    session_expires_at = serializers.DateTimeField()
    session_id = serializers.UUIDField()


class VerifyResultSerializer(TokenPairSerializer):
    user = CurrentUserSerializer()
    is_new_user = serializers.BooleanField()


class AuthSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuthSession
        fields = ("id", "device_name", "created_at", "last_used_at", "expires_at")
        read_only_fields = fields
