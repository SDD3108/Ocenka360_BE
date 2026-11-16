from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.orders.permissions import VerifiedAccount
from apps.pricing.views import validated

from . import services
from .models import PushDevice
from .serializers import (
    CountSerializer,
    DeviceInput,
    DeviceSerializer,
    EmptyInput,
    NotificationQuery,
    NotificationSerializer,
)

PERMISSIONS = [IsAuthenticated, VerifiedAccount]


class NotificationsView(ListAPIView):
    permission_classes = PERMISSIONS
    serializer_class = NotificationSerializer

    def get_queryset(self):
        data = validated(NotificationQuery, self.request.query_params.dict())
        query = services.visible(self.request.user)
        if "unread" in data:
            query = query.filter(read_at__isnull=data["unread"])
        return query

    @extend_schema(parameters=[NotificationQuery], tags=["Notifications"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class NotificationDetailView(APIView):
    permission_classes = PERMISSIONS

    @extend_schema(responses=NotificationSerializer, tags=["Notifications"])
    def get(self, request, notification_id):
        obj = get_object_or_404(services.visible(request.user), pk=notification_id)
        return Response(NotificationSerializer(obj, context={"request": request}).data)


class ReadView(APIView):
    permission_classes = PERMISSIONS

    @extend_schema(request=EmptyInput, responses=NotificationSerializer, tags=["Notifications"])
    def post(self, request, notification_id):
        validated(EmptyInput, request.data)
        query = services.visible(request.user)
        obj = get_object_or_404(query, pk=notification_id)
        query.filter(pk=obj.pk, read_at__isnull=True).update(read_at=timezone.now())
        obj.refresh_from_db()
        return Response(NotificationSerializer(obj, context={"request": request}).data)


class CountView(APIView):
    permission_classes = PERMISSIONS

    @extend_schema(responses=CountSerializer, tags=["Notifications"])
    def get(self, request):
        return Response({"unread": services.visible(request.user).filter(read_at__isnull=True).count()})


class DevicesView(ListAPIView):
    permission_classes = PERMISSIONS
    serializer_class = DeviceSerializer

    def get_queryset(self):
        return PushDevice.objects.filter(user=self.request.user).order_by("-updated_at", "id")

    @extend_schema(request=DeviceInput, responses=DeviceSerializer, tags=["Notifications"])
    def post(self, request):
        session_id = request.auth.get("sid") if request.auth else None
        device = services.register_device(request.user, session_id, **validated(DeviceInput, request.data))
        return Response(DeviceSerializer(device).data)


class DeviceDeleteView(APIView):
    permission_classes = PERMISSIONS

    @extend_schema(responses={204: None}, tags=["Notifications"])
    def delete(self, request, device_id):
        services.unregister_device(request.user, device_id)
        return Response(status=204)
