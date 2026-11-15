from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import APIException
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import HasAcceptedCurrentDocuments
from apps.core.serializers import ErrorSerializer

from .models import Category
from .schema import evaluate, localize_document, localized_definition
from .serializers import (
    CategorySerializer,
    FormSerializer,
    LanguageQuery,
    ServiceQuery,
    ServiceSerializer,
    ValidateAnswersSerializer,
    ValidationResultSerializer,
)
from .services import published_services


class StaleForm(APIException):
    status_code = 409
    default_code = "stale_form"
    default_detail = "The form changed. Fetch the current form and review your answers."


class PublicCatalogView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def language(self, request):
        query = LanguageQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        return query.validated_data["language"]


class CategoryListView(PublicCatalogView):
    @extend_schema(parameters=[LanguageQuery], responses=CategorySerializer(many=True), tags=["Catalog"])
    def get(self, request):
        language = self.language(request)
        categories = Category.objects.filter(is_active=True).annotate(
            service_count=Count("services", filter=Q(services__in=published_services()))
        )
        return Response(CategorySerializer(categories, many=True, context={"language": language}).data)


class ServiceListView(ListAPIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = ServiceSerializer

    def get_queryset(self):
        params = ServiceQuery(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        self.params = params.validated_data
        query = published_services()
        if "category" in self.params:
            query = query.filter(category__slug=self.params["category"])
        if self.params.get("q"):
            term = self.params["q"]
            language = self.params["language"]
            # Bound parameters; no raw SQL or search expressions from the client.
            query = query.filter(Q(**{f"title__{language}__icontains": term}) | Q(slug__icontains=term))
        return query

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "language": self.params["language"]}

    @extend_schema(parameters=[ServiceQuery], tags=["Catalog"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ServiceDetailView(PublicCatalogView):
    @extend_schema(parameters=[LanguageQuery], responses=ServiceSerializer, tags=["Catalog"])
    def get(self, request, slug):
        language = self.language(request)
        service = get_object_or_404(published_services(), slug=slug)
        return Response(ServiceSerializer(service, context={"language": language}).data)


class ServiceFormView(PublicCatalogView):
    @extend_schema(parameters=[LanguageQuery], responses=FormSerializer, tags=["Catalog"])
    def get(self, request, slug):
        language = self.language(request)
        service = get_object_or_404(published_services(), slug=slug)
        form = service.current_form
        return Response(
            {
                "id": form.id,
                "version": form.version,
                "checksum": form.checksum,
                "service": service.slug,
                "language": language,
                **localized_definition(form.definition, language),
            }
        )


class ValidateFormView(APIView):
    permission_classes = [IsAuthenticated, HasAcceptedCurrentDocuments]

    @extend_schema(
        request=ValidateAnswersSerializer,
        responses={
            200: ValidationResultSerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            403: ErrorSerializer,
            409: ErrorSerializer,
        },
        tags=["Catalog"],
    )
    def post(self, request, slug):
        serializer = ValidateAnswersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        service = get_object_or_404(published_services(), slug=slug)
        form = service.current_form
        if form.id != data["form_id"]:
            raise StaleForm()
        result = evaluate(form.definition, data["answers"])
        # Preview only. Do not persist, log or echo personal answers.
        return Response(
            {
                "form_id": form.id,
                "form_version": form.version,
                "valid": result["valid"],
                "errors": result["errors"],
                "visible_fields": result["visible_fields"],
                "documents": [localize_document(d, data["language"]) for d in result["documents"]],
                "notice": form.definition["notice"][data["language"]],
                "submission_created": False,
            }
        )
