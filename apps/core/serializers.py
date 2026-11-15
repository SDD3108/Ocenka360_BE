from rest_framework import serializers


class HealthSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["ok", "unavailable"])


class ApiRootSerializer(serializers.Serializer):
    service = serializers.CharField()
    version = serializers.CharField()
    api_version = serializers.CharField()


class CompanySerializer(serializers.Serializer):
    name = serializers.CharField()
    legal_name = serializers.CharField(allow_null=True)
    phone = serializers.CharField()
    email = serializers.EmailField()
    address = serializers.CharField()


class FeaturesSerializer(serializers.Serializer):
    email_otp = serializers.BooleanField()
    catalog = serializers.BooleanField()
    orders = serializers.BooleanField()
    materials = serializers.BooleanField()
    pricing = serializers.BooleanField()
    scheduling = serializers.BooleanField()
    payments = serializers.BooleanField()
    communications = serializers.BooleanField()
    notification_inbox = serializers.BooleanField()
    push = serializers.BooleanField()
    ai = serializers.BooleanField()


class PublicConfigSerializer(serializers.Serializer):
    company = CompanySerializer()
    default_language = serializers.CharField()
    supported_languages = serializers.ListField(child=serializers.CharField())
    currency = serializers.CharField()
    timezone = serializers.CharField()
    features = FeaturesSerializer()


class ErrorDetailSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()
    details = serializers.DictField()


class ErrorSerializer(serializers.Serializer):
    error = ErrorDetailSerializer()
