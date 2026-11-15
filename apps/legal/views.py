from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.serializers import CurrentUserSerializer, StrictSerializer
from apps.core.serializers import ErrorSerializer

from .models import ConsentAcceptance, LegalDocument
from .services import accept_documents, required_documents


class LegalDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = LegalDocument
        fields = ("id", "kind", "version", "language", "title", "content", "checksum", "is_draft")
        read_only_fields = fields


class LanguageSerializer(serializers.Serializer):
    language = serializers.ChoiceField(choices=User.Language.choices, default="ru")


class ConsentSerializer(StrictSerializer):
    documents = serializers.ListField(child=serializers.UUIDField(), min_length=3, max_length=3)
    adult_confirmed = serializers.BooleanField()


class AcceptanceSerializer(serializers.ModelSerializer):
    document = LegalDocumentSerializer()

    class Meta:
        model = ConsentAcceptance
        fields = ("document", "accepted_at", "document_checksum", "adult_confirmed")


class DocumentsView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    @extend_schema(
        parameters=[LanguageSerializer],
        responses={200: LegalDocumentSerializer(many=True), 503: ErrorSerializer},
        tags=["Legal"],
    )
    def get(self, request):
        query = LanguageSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(
            LegalDocumentSerializer(required_documents(query.validated_data["language"]), many=True).data
        )


class ConsentsView(APIView):
    @extend_schema(responses={200: AcceptanceSerializer(many=True)}, tags=["Legal"])
    def get(self, request):
        records = request.user.consents.select_related("document").all()
        return Response(AcceptanceSerializer(records, many=True).data)

    @extend_schema(
        request=ConsentSerializer,
        responses={200: CurrentUserSerializer, 400: ErrorSerializer, 503: ErrorSerializer},
        tags=["Legal"],
    )
    def post(self, request):
        serializer = ConsentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = accept_documents(
            request.user, serializer.validated_data["documents"], serializer.validated_data["adult_confirmed"]
        )
        return Response(CurrentUserSerializer(user).data)
