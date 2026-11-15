from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.materials.services import get_order
from apps.orders.permissions import VerifiedAccount
from apps.orders.services import review_queryset
from apps.pricing.views import validated

from . import services
from .models import Appointment, CalendarBlock, Inspection
from .serializers import (
    AcceptSerializer,
    BlockCancelSerializer,
    BlockCreateSerializer,
    BlockSerializer,
    CalendarSerializer,
    EventSerializer,
    FinishSerializer,
    OfferSerializer,
    OverviewSerializer,
    PromiseSerializer,
    RangeQuery,
    RequestSerializer,
    ResultSerializer,
    ScheduleCancelSerializer,
    StartSerializer,
)

REVIEW_PERMISSIONS = [IsAuthenticated, VerifiedAccount, IsAppraiserOrAdmin]


def result(appointment, order):
    return {"appointment": appointment, "order_version": order.version, "order_status": order.status}


class OrderScheduleView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    review = False

    @extend_schema(responses=OverviewSerializer, tags=["Scheduling"])
    def get(self, request, order_id):
        order = get_order(request.user, order_id, self.review)
        return Response(
            OverviewSerializer(
                {
                    "order_version": order.version,
                    "order_status": order.status,
                    "timezone": settings.TIME_ZONE,
                    "appointments": order.appointments.all(),
                    "inspection": getattr(order, "inspection", None),
                }
            ).data
        )

    @extend_schema(
        request=RequestSerializer,
        responses={201: ResultSerializer, 200: ResultSerializer},
        tags=["Scheduling"],
    )
    def post(self, request, order_id):
        appointment, order, created = services.propose(
            request.user, order_id, validated(RequestSerializer, request.data)
        )
        return Response(ResultSerializer(result(appointment, order)).data, status=201 if created else 200)


class ReviewScheduleView(OrderScheduleView):
    permission_classes = REVIEW_PERMISSIONS
    review = True

    @extend_schema(
        request=OfferSerializer,
        responses={201: ResultSerializer, 200: ResultSerializer},
        tags=["Scheduling review"],
    )
    def post(self, request, order_id):
        appointment, order, created = services.propose(
            request.user, order_id, validated(OfferSerializer, request.data), review=True
        )
        return Response(ResultSerializer(result(appointment, order)).data, status=201 if created else 200)


class ScheduleHistoryView(ListAPIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    serializer_class = EventSerializer
    review = False

    def get_queryset(self):
        return get_order(self.request.user, self.kwargs["order_id"], self.review).schedule_events.all()

    @extend_schema(tags=["Scheduling"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ReviewScheduleHistoryView(ScheduleHistoryView):
    permission_classes = REVIEW_PERMISSIONS
    review = True


class ConfirmView(APIView):
    permission_classes = REVIEW_PERMISSIONS

    @extend_schema(request=PromiseSerializer, responses=ResultSerializer, tags=["Scheduling review"])
    def post(self, request, order_id, appointment_id):
        appointment, order = services.confirm_request(
            request.user, order_id, appointment_id, validated(PromiseSerializer, request.data)
        )
        return Response(ResultSerializer(result(appointment, order)).data)


class AcceptView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]

    @extend_schema(request=AcceptSerializer, responses=ResultSerializer, tags=["Scheduling"])
    def post(self, request, order_id, appointment_id):
        appointment, order = services.accept_offer(
            request.user, order_id, appointment_id, **validated(AcceptSerializer, request.data)
        )
        return Response(ResultSerializer(result(appointment, order)).data)


class CancelView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    review = False

    @extend_schema(request=ScheduleCancelSerializer, responses=ResultSerializer, tags=["Scheduling"])
    def post(self, request, order_id, appointment_id):
        appointment, order = services.cancel_appointment(
            request.user,
            order_id,
            appointment_id,
            review=self.review,
            **validated(ScheduleCancelSerializer, request.data),
        )
        return Response(ResultSerializer(result(appointment, order)).data)


class ReviewCancelView(CancelView):
    permission_classes = REVIEW_PERMISSIONS
    review = True


class StartView(APIView):
    permission_classes = REVIEW_PERMISSIONS

    @extend_schema(request=StartSerializer, responses=ResultSerializer, tags=["Scheduling review"])
    def post(self, request, order_id, appointment_id):
        appointment, order = services.start_inspection(
            request.user, order_id, appointment_id, **validated(StartSerializer, request.data)
        )
        return Response(ResultSerializer(result(appointment, order)).data)


class FinishView(APIView):
    permission_classes = REVIEW_PERMISSIONS

    @extend_schema(request=FinishSerializer, responses=ResultSerializer, tags=["Scheduling review"])
    def post(self, request, order_id, appointment_id):
        appointment, order = services.finish_inspection(
            request.user, order_id, appointment_id, **validated(FinishSerializer, request.data)
        )
        return Response(ResultSerializer(result(appointment, order)).data)


class CalendarView(APIView):
    permission_classes = REVIEW_PERMISSIONS

    @extend_schema(parameters=[RangeQuery], responses=CalendarSerializer, tags=["Scheduling review"])
    def get(self, request):
        data = validated(RangeQuery, request.query_params)
        start, end = data["starts_at"], data["ends_at"]
        visible_orders = review_queryset().values("pk")
        appointments = list(
            Appointment.objects.filter(
                order_id__in=visible_orders,
                state__in=["confirmed", "in_progress", "completed"],
                busy_from__lt=end,
                busy_until__gt=start,
            ).order_by("starts_at", "id")[:501]
        )
        blocks = list(
            CalendarBlock.objects.filter(cancelled_at__isnull=True, starts_at__lt=end, ends_at__gt=start)[
                :501
            ]
        )
        deadlines = list(
            Inspection.objects.select_related("order")
            .filter(
                order_id__in=visible_orders,
                order__status__in=["in_progress", "awaiting_report"],
                report_due_at__gte=start,
                report_due_at__lt=end,
            )
            .order_by("report_due_at", "id")[:501]
        )
        return Response(
            CalendarSerializer(
                {
                    "timezone": settings.TIME_ZONE,
                    "appointments": appointments[:500],
                    "blocks": blocks[:500],
                    "report_deadlines": deadlines[:500],
                    "truncated": any(len(items) > 500 for items in (appointments, blocks, deadlines)),
                }
            ).data
        )


class BlockView(APIView):
    permission_classes = REVIEW_PERMISSIONS

    @extend_schema(
        request=BlockCreateSerializer,
        responses={201: BlockSerializer, 200: BlockSerializer},
        tags=["Scheduling review"],
    )
    def post(self, request):
        block, created = services.create_block(request.user, validated(BlockCreateSerializer, request.data))
        return Response(BlockSerializer(block).data, status=201 if created else 200)


class BlockCancelView(APIView):
    permission_classes = REVIEW_PERMISSIONS

    @extend_schema(request=BlockCancelSerializer, responses=BlockSerializer, tags=["Scheduling review"])
    def post(self, request, block_id):
        block = services.cancel_block(
            request.user, block_id, **validated(BlockCancelSerializer, request.data)
        )
        return Response(BlockSerializer(block).data)
