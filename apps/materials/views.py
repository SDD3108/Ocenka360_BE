from django.conf import settings
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.catalog.serializers import LanguageQuery
from apps.core.serializers import ErrorSerializer
from apps.orders.permissions import ConsentForWrites, VerifiedAccount
from apps.orders.serializers import VersionSerializer

from . import services
from .models import Material
from .serializers import (
    MaterialListSerializer,
    MaterialResultSerializer,
    MaterialReviewSerializer,
    MaterialSerializer,
    RequirementsSerializer,
    UploadSerializer,
    VersionResultSerializer,
)
from .storage import PrivateStorage


class MaterialBase(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount, ConsentForWrites]
    review = False

    def order(self, request, order_id):
        return services.get_order(request.user, order_id, self.review)


class MaterialListView(MaterialBase):
    parser_classes = [MultiPartParser]

    @extend_schema(responses=MaterialListSerializer, tags=["Materials"])
    def get(self, request, order_id):
        order = self.order(request, order_id)
        return Response(
            {
                "materials": MaterialSerializer(order.materials.all(), many=True).data,
                "order_version": order.version,
            }
        )

    @extend_schema(
        request=UploadSerializer,
        responses={
            201: MaterialResultSerializer,
            200: MaterialResultSerializer,
            409: ErrorSerializer,
            413: ErrorSerializer,
            503: ErrorSerializer,
        },
        tags=["Materials"],
    )
    def post(self, request, order_id):
        self.order(request, order_id)  # Object scope before parsing the multipart file.
        if any(len(request.data.getlist(key)) != 1 for key in request.data):
            raise ValidationError("Send each multipart field exactly once and one file per request.")
        data = UploadSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        material, order, created = services.upload(request.user, order_id, data.validated_data, self.review)
        return Response(
            {"material": MaterialSerializer(material).data, "order_version": order.version},
            status=201 if created else 200,
        )


class MaterialRequirementsView(MaterialBase):
    @extend_schema(parameters=[LanguageQuery], responses=RequirementsSerializer, tags=["Materials"])
    def get(self, request, order_id):
        order = self.order(request, order_id)
        params = request.query_params.copy()
        params.setdefault("language", order.language)
        query = LanguageQuery(data=params)
        query.is_valid(raise_exception=True)
        return Response(services.checklist(order, query.validated_data["language"]))


class MaterialDownloadView(MaterialBase):
    @extend_schema(responses={(200, "application/octet-stream"): OpenApiTypes.BINARY}, tags=["Materials"])
    def get(self, request, order_id, material_id):
        order = self.order(request, order_id)
        material = get_object_or_404(order.materials, pk=material_id, withdrawn_at__isnull=True)
        if not settings.ALLOW_UNSCANNED_UPLOADS and material.scan_status != Material.Scan.CLEAN:
            raise NotFound()
        try:
            stream = PrivateStorage().open(material.storage_key, "rb")
        except FileNotFoundError as exc:
            raise NotFound() from exc
        extension = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[material.mime_type]
        response = FileResponse(
            stream, as_attachment=True, filename=f"{material.pk}.{extension}", content_type=material.mime_type
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


class MaterialWithdrawView(MaterialBase):
    @extend_schema(request=VersionSerializer, responses=VersionResultSerializer, tags=["Materials"])
    def post(self, request, order_id, material_id):
        data = VersionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        order = services.withdraw(
            request.user, order_id, material_id, **data.validated_data, review=self.review
        )
        return Response({"order_version": order.version})


REVIEW_PERMISSIONS = [IsAuthenticated, VerifiedAccount, IsAppraiserOrAdmin]


class ReviewMaterialListView(MaterialListView):
    permission_classes = REVIEW_PERMISSIONS
    review = True


class ReviewMaterialRequirementsView(MaterialRequirementsView):
    permission_classes = REVIEW_PERMISSIONS
    review = True


class ReviewMaterialDownloadView(MaterialDownloadView):
    permission_classes = REVIEW_PERMISSIONS
    review = True


class ReviewMaterialWithdrawView(MaterialWithdrawView):
    permission_classes = REVIEW_PERMISSIONS
    review = True


class MaterialReviewView(MaterialBase):
    permission_classes = REVIEW_PERMISSIONS
    review = True

    @extend_schema(request=MaterialReviewSerializer, responses=MaterialResultSerializer, tags=["Materials"])
    def post(self, request, order_id, material_id):
        data = MaterialReviewSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        material, order = services.review_material(request.user, order_id, material_id, **data.validated_data)
        return Response({"material": MaterialSerializer(material).data, "order_version": order.version})
