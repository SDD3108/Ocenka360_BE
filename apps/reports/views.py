from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.materials.services import get_order
from apps.orders.permissions import VerifiedAccount
from apps.pricing.views import validated

from . import services
from .serializers import (
    DeliveryInput,
    OriginalInput,
    OriginalResult,
    PublishInput,
    ReportQuery,
    ReportResult,
    ReportSerializer,
    UploadInput,
    WithdrawInput,
)

REVIEW = [IsAuthenticated, VerifiedAccount, IsAppraiserOrAdmin]
CLIENT = [IsAuthenticated, VerifiedAccount]


def result(report, order):
    return ReportResult({"report": report, "order_version": order.version, "order_status": order.status}).data


def original_result(original, order):
    return OriginalResult(
        {
            "original": original,
            "order_version": order.version,
            "delivery_contact_url": services.delivery_contact(order),
        }
    ).data


class ReportsView(ListAPIView):
    permission_classes = CLIENT
    serializer_class = ReportSerializer
    review = False

    def get_queryset(self):
        validated(ReportQuery, self.request.query_params)
        order = get_order(self.request.user, self.kwargs["order_id"], self.review)
        query = order.reports.select_related("original")
        return query if self.review else query.filter(published_at__isnull=False)


class ReviewReportsView(ReportsView):
    permission_classes = REVIEW
    review = True
    parser_classes = [MultiPartParser]

    @extend_schema(request=UploadInput, responses={201: ReportResult, 200: ReportResult}, tags=["Reports"])
    def post(self, request, order_id):
        get_order(request.user, order_id, True)
        if any(len(request.data.getlist(key)) != 1 for key in request.data):
            raise ValidationError("Send each multipart field exactly once and one PDF.")
        report, order, created = services.upload(request.user, order_id, validated(UploadInput, request.data))
        return Response(result(report, order), status=201 if created else 200)


class PublishView(APIView):
    permission_classes = REVIEW

    @extend_schema(request=PublishInput, responses=ReportResult, tags=["Reports"])
    def post(self, request, order_id, report_id):
        report, order = services.publish(
            request.user, order_id, report_id, **validated(PublishInput, request.data)
        )
        return Response(result(report, order))


class WithdrawView(APIView):
    permission_classes = REVIEW

    @extend_schema(request=WithdrawInput, responses=ReportResult, tags=["Reports"])
    def post(self, request, order_id, report_id):
        report, order = services.withdraw(
            request.user, order_id, report_id, **validated(WithdrawInput, request.data)
        )
        return Response(result(report, order))


class DownloadView(APIView):
    permission_classes = CLIENT
    review = False

    @extend_schema(responses={(200, "application/pdf"): OpenApiTypes.BINARY}, tags=["Reports"])
    def get(self, request, order_id, report_id):
        report, stream = services.open_report(request.user, order_id, report_id, self.review)
        response = FileResponse(
            stream,
            as_attachment=True,
            filename=f"ocenka360-{report.id}-v{report.revision}.pdf",
            content_type="application/pdf",
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


class ReviewDownloadView(DownloadView):
    permission_classes = REVIEW
    review = True


class OriginalView(APIView):
    permission_classes = CLIENT
    review = False

    @extend_schema(responses=OriginalResult, tags=["Reports"])
    def get(self, request, order_id, report_id):
        order = get_order(request.user, order_id, self.review)
        report = get_object_or_404(order.reports, pk=report_id, state="published")
        return Response(original_result(report.original, order))


class ReviewOriginalView(OriginalView):
    permission_classes = REVIEW
    review = True

    @extend_schema(request=OriginalInput, responses=OriginalResult, tags=["Reports"])
    def post(self, request, order_id, report_id):
        original, order = services.original_action(
            request.user, order_id, report_id, **validated(OriginalInput, request.data)
        )
        return Response(original_result(original, order))


class DeliveryView(APIView):
    permission_classes = CLIENT

    @extend_schema(request=DeliveryInput, responses=OriginalResult, tags=["Reports"])
    def post(self, request, order_id, report_id):
        original, order = services.request_delivery(
            request.user, order_id, report_id, **validated(DeliveryInput, request.data)
        )
        return Response(original_result(original, order))
