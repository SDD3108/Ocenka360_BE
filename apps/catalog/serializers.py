from rest_framework import serializers

from apps.accounts.serializers import StrictSerializer

from .models import Category, Service
from .schema import KEY


class LanguageQuery(StrictSerializer):
    language = serializers.ChoiceField(choices=["ru", "kk", "en"], default="ru")


class ServiceQuery(LanguageQuery):
    category = serializers.SlugField(max_length=80, required=False)
    q = serializers.CharField(max_length=100, required=False, allow_blank=True)
    page = serializers.IntegerField(min_value=1, required=False)


class CategorySerializer(serializers.ModelSerializer):
    title = serializers.SerializerMethodField()
    service_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Category
        fields = ("id", "slug", "title", "service_count")

    def get_title(self, obj) -> str:
        return obj.title[self.context["language"]]


class ServiceSerializer(serializers.ModelSerializer):
    title = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    category = serializers.CharField(source="category.slug", read_only=True)
    form_id = serializers.UUIDField(source="current_form_id", read_only=True)
    form_version = serializers.IntegerField(source="current_form.version", read_only=True)

    class Meta:
        model = Service
        fields = ("id", "slug", "category", "title", "summary", "form_id", "form_version")

    def get_title(self, obj) -> str:
        return obj.title[self.context["language"]]

    def get_summary(self, obj) -> str:
        return obj.summary[self.context["language"]]


class ConditionSerializer(serializers.Serializer):
    field = serializers.CharField()
    equals = serializers.JSONField(help_text="Exact choice string or boolean; no expressions.")


class OptionSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()


class FormFieldSerializer(serializers.Serializer):
    key = serializers.CharField()
    type = serializers.ChoiceField(
        choices=["text", "textarea", "integer", "decimal", "date", "choice", "boolean"]
    )
    label = serializers.CharField()
    required = serializers.BooleanField()
    help = serializers.CharField(required=False)
    options = OptionSerializer(many=True, required=False)
    min = serializers.JSONField(required=False, help_text="Integer or exact decimal string.")
    max = serializers.JSONField(required=False, help_text="Integer or exact decimal string.")
    max_length = serializers.IntegerField(required=False)
    visible_if = ConditionSerializer(required=False)


class DocumentRequirementSerializer(serializers.Serializer):
    key = serializers.CharField()
    title = serializers.CharField()
    help = serializers.CharField()
    required = serializers.BooleanField()
    when = ConditionSerializer(required=False)


class FormSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    version = serializers.IntegerField()
    checksum = serializers.CharField()
    service = serializers.CharField()
    language = serializers.CharField()
    notice = serializers.CharField()
    fields = FormFieldSerializer(many=True)
    documents = DocumentRequirementSerializer(many=True)


class ValidateAnswersSerializer(StrictSerializer):
    form_id = serializers.UUIDField()
    language = serializers.ChoiceField(choices=["ru", "kk", "en"], default="ru")
    answers = serializers.JSONField(
        help_text="Object keyed by field key. Decimal values are strings; integers/booleans are JSON numbers/booleans."
    )

    def validate_answers(self, value):
        if not isinstance(value, dict) or len(value) > 50:
            raise serializers.ValidationError("Provide an object with at most 50 answers.")
        if any(not isinstance(k, str) or not KEY.fullmatch(k) for k in value):
            raise serializers.ValidationError("Invalid answer key.")
        for v in value.values():
            if isinstance(v, (dict, list)) or (isinstance(v, str) and len(v) > 4000):
                raise serializers.ValidationError(
                    "Answers must be scalar values, strings at most 4000 characters."
                )
        return value


class ValidationResultSerializer(serializers.Serializer):
    form_id = serializers.UUIDField()
    form_version = serializers.IntegerField()
    valid = serializers.BooleanField()
    errors = serializers.DictField(child=serializers.CharField())
    visible_fields = serializers.ListField(child=serializers.CharField())
    documents = DocumentRequirementSerializer(many=True)
    notice = serializers.CharField()
    submission_created = serializers.BooleanField()
