from django.conf import settings
from django.db import DatabaseError, connection
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.payments.providers import available_methods

from .serializers import ApiRootSerializer, HealthSerializer, PublicConfigSerializer


class PublicView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []


class ApiRootView(PublicView):
    @extend_schema(responses=ApiRootSerializer, tags=["System"])
    def get(self, request):
        return Response({"service": "ocenka360-api", "version": settings.APP_VERSION, "api_version": "v1"})


class LiveView(PublicView):
    throttle_classes = []

    @extend_schema(responses=HealthSerializer, tags=["System"])
    def get(self, request):
        return Response({"status": "ok"})


class ReadyView(PublicView):
    throttle_classes = []

    @extend_schema(responses={200: HealthSerializer, 503: HealthSerializer}, tags=["System"])
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except DatabaseError:
            return Response({"status": "unavailable"}, status=503)
        return Response({"status": "ok"})


class PublicConfigView(PublicView):
    @extend_schema(responses=PublicConfigSerializer, tags=["System"])
    def get(self, request):
        return Response(
            {
                "company": {
                    "name": "Оценка 360",
                    "legal_name": settings.COMPANY_LEGAL_NAME or None,
                    "phone": "+77017700026",
                    "email": "sati-ocenka@mail.ru",
                    "address": "Казахстан, Астана, проспект Кабанбай батыр, 46а",
                },
                "default_language": "ru",
                "supported_languages": ["ru", "kk", "en"],
                "currency": "KZT",
                "timezone": settings.TIME_ZONE,
                "features": {
                    "email_otp": True,
                    "catalog": True,
                    "orders": True,
                    "materials": True,
                    "pricing": True,
                    "scheduling": True,
                    "payments": available_methods()["kaspi_manual"]
                    or (available_methods()["halyk"] and settings.HALYK_ENVIRONMENT == "live"),
                    "ai": False,
                },
            }
        )
