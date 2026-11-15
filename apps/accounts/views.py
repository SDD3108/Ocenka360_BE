from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.serializers import ErrorSerializer

from .authentication import SessionJWTAuthentication
from .models import AuthSession
from .serializers import (
    AuthSessionSerializer,
    CodeRequestedSerializer,
    CurrentUserSerializer,
    RefreshSerializer,
    RequestCodeSerializer,
    TokenPairSerializer,
    VerifyCodeSerializer,
    VerifyResultSerializer,
)
from .services import logout_all, request_code, rotate_refresh, verify_code


class AuthPublicView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get_authenticate_header(self, request):
        return 'Bearer realm="api"'


class RequestCodeView(AuthPublicView):
    @extend_schema(
        request=RequestCodeSerializer,
        responses={
            202: CodeRequestedSerializer,
            400: ErrorSerializer,
            429: ErrorSerializer,
            503: ErrorSerializer,
        },
        tags=["Auth"],
    )
    def post(self, request):
        serializer = RequestCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        challenge = request_code(**serializer.validated_data, ip=request.META.get("REMOTE_ADDR", "unknown"))
        return Response(
            {
                "challenge_id": challenge.id,
                "expires_in": settings.OTP_TTL_SECONDS,
                "resend_after": settings.OTP_RESEND_SECONDS,
                "message": "If sign-in is available, a code has been sent to this email.",
            },
            status=202,
        )


class VerifyCodeView(AuthPublicView):
    @extend_schema(
        request=VerifyCodeSerializer,
        responses={200: VerifyResultSerializer, 400: ErrorSerializer, 429: ErrorSerializer},
        tags=["Auth"],
    )
    def post(self, request):
        serializer = VerifyCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user, pair, created = verify_code(
            **serializer.validated_data, ip=request.META.get("REMOTE_ADDR", "unknown")
        )
        return Response({**pair, "user": CurrentUserSerializer(user).data, "is_new_user": created})


class RefreshView(AuthPublicView):
    @extend_schema(
        request=RefreshSerializer, responses={200: TokenPairSerializer, 401: ErrorSerializer}, tags=["Auth"]
    )
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(rotate_refresh(serializer.validated_data["refresh"]))


class JWTOnlyView(APIView):
    authentication_classes = [SessionJWTAuthentication]


class LogoutView(JWTOnlyView):
    @extend_schema(request=None, responses={204: None, 401: ErrorSerializer}, tags=["Auth"])
    def post(self, request):
        AuthSession.objects.filter(pk=request.auth["sid"], user=request.user, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        return Response(status=204)


class LogoutAllView(JWTOnlyView):
    @extend_schema(request=None, responses={204: None, 401: ErrorSerializer}, tags=["Auth"])
    def post(self, request):
        logout_all(request.user)
        return Response(status=204)


class SessionListView(JWTOnlyView):
    @extend_schema(responses={200: AuthSessionSerializer(many=True)}, tags=["Auth"])
    def get(self, request):
        sessions = request.user.app_sessions.filter(revoked_at__isnull=True, expires_at__gt=timezone.now())
        return Response(AuthSessionSerializer(sessions, many=True).data)


class SessionRevokeView(JWTOnlyView):
    @extend_schema(request=None, responses={204: None, 404: ErrorSerializer}, tags=["Auth"])
    def delete(self, request, session_id):
        session = get_object_or_404(AuthSession, pk=session_id, user=request.user)
        AuthSession.objects.filter(pk=session.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
        return Response(status=204)


@extend_schema(tags=["Accounts"])
class CurrentUserView(RetrieveUpdateAPIView):
    serializer_class = CurrentUserSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        return self.request.user
