from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.catalog.models import Service
from apps.catalog.serializers import LanguageQuery
from apps.core.views import PublicView
from apps.orders import services as orders
from apps.orders.models import Order
from apps.orders.permissions import ConsentForWrites, VerifiedAccount

from . import services
from .calculator import estimate
from .models import PromoCode, TariffRevision
from .serializers import (
    AcceptQuoteSerializer,
    DeclineQuoteSerializer,
    EstimateInputSerializer,
    EstimateSerializer,
    OrderEstimateSerializer,
    PriceOverviewSerializer,
    PricingQuery,
    PromoCreateSerializer,
    PromoSelectionSerializer,
    PromoSerializer,
    PromoToggleSerializer,
    PublishSerializer,
    QuoteCreateSerializer,
    QuoteDetailSerializer,
    QuoteResultSerializer,
    QuoteSerializer,
    TariffCreateSerializer,
    TariffSerializer,
)


def validated(serializer, data):
    value = serializer(data=data)
    value.is_valid(raise_exception=True)
    return value.validated_data


def quote_result(quote, order):
    return {
        "quote": QuoteDetailSerializer(quote).data,
        "order_version": order.version,
        "order_status": order.status,
    }


class EstimateView(PublicView):
    @extend_schema(request=EstimateInputSerializer, responses=EstimateSerializer, tags=["Pricing"])
    def post(self, request, slug):
        service = get_object_or_404(
            Service,
            slug=slug,
            is_active=True,
            category__is_active=True,
            current_form__published_at__isnull=False,
        )
        return Response(estimate(service, **validated(EstimateInputSerializer, request.data)))


class OwnerPricingView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount, ConsentForWrites]
    review = False

    def get_order(self, request, order_id):
        query = orders.review_queryset() if self.review else Order.objects.filter(owner=request.user)
        return get_object_or_404(query, pk=order_id)

    @extend_schema(responses=PriceOverviewSerializer, tags=["Pricing"])
    def get(self, request, order_id):
        order = self.get_order(request, order_id)
        promo = services.selected_promo(order)
        return Response(
            {
                "order_version": order.version,
                "order_status": order.status,
                "selected_promo_code": promo.code if promo else None,
                "quotes": QuoteSerializer(order.price_quotes.all(), many=True).data,
            }
        )


class OrderEstimateView(OwnerPricingView):
    @extend_schema(parameters=[LanguageQuery], responses=OrderEstimateSerializer, tags=["Pricing"])
    def get(self, request, order_id):
        order = self.get_order(request, order_id)
        params = request.query_params.copy()
        params.setdefault("language", order.language)
        return Response(services.estimate_order(order, **validated(LanguageQuery, params)))


class SelectPromoView(OwnerPricingView):
    http_method_names = ["post", "options"]

    @extend_schema(request=PromoSelectionSerializer, responses=PriceOverviewSerializer, tags=["Pricing"])
    def post(self, request, order_id):
        services.select_promo(request.user, order_id, **validated(PromoSelectionSerializer, request.data))
        return super().get(request, order_id)


class QuoteDetailView(OwnerPricingView):
    @extend_schema(operation_id="order_price_quote_detail", responses=QuoteDetailSerializer, tags=["Pricing"])
    def get(self, request, order_id, quote_id):
        order = self.get_order(request, order_id)
        return Response(QuoteDetailSerializer(get_object_or_404(order.price_quotes, pk=quote_id)).data)


class AcceptQuoteView(OwnerPricingView):
    http_method_names = ["post", "options"]

    @extend_schema(request=AcceptQuoteSerializer, responses=QuoteResultSerializer, tags=["Pricing"])
    def post(self, request, order_id, quote_id):
        quote, order = services.decide_quote(
            request.user,
            order_id,
            quote_id,
            action="accept",
            **validated(AcceptQuoteSerializer, request.data),
        )
        return Response(quote_result(quote, order))


class DeclineQuoteView(OwnerPricingView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    http_method_names = ["post", "options"]

    @extend_schema(request=DeclineQuoteSerializer, responses=QuoteResultSerializer, tags=["Pricing"])
    def post(self, request, order_id, quote_id):
        quote, order = services.decide_quote(
            request.user,
            order_id,
            quote_id,
            action="decline",
            **validated(DeclineQuoteSerializer, request.data),
        )
        return Response(quote_result(quote, order))


REVIEW_PERMISSIONS = [IsAuthenticated, VerifiedAccount, IsAppraiserOrAdmin]


class ReviewPricingView(OwnerPricingView):
    permission_classes = REVIEW_PERMISSIONS
    review = True

    @extend_schema(
        request=QuoteCreateSerializer,
        responses={201: QuoteResultSerializer, 200: QuoteResultSerializer},
        tags=["Pricing review"],
    )
    def post(self, request, order_id):
        quote, order, created = services.issue_quote(
            request.user, order_id, validated(QuoteCreateSerializer, request.data)
        )
        return Response(quote_result(quote, order), status=201 if created else 200)


class ReviewQuoteDetailView(QuoteDetailView):
    permission_classes = REVIEW_PERMISSIONS
    review = True

    @extend_schema(
        operation_id="review_price_quote_detail", responses=QuoteDetailSerializer, tags=["Pricing review"]
    )
    def get(self, request, order_id, quote_id):
        return super().get(request, order_id, quote_id)


class WithdrawQuoteView(ReviewPricingView):
    http_method_names = ["post", "options"]

    @extend_schema(request=DeclineQuoteSerializer, responses=QuoteResultSerializer, tags=["Pricing review"])
    def post(self, request, order_id, quote_id):
        quote, order = services.decide_quote(
            request.user,
            order_id,
            quote_id,
            action="withdraw",
            review=True,
            **validated(DeclineQuoteSerializer, request.data),
        )
        return Response(quote_result(quote, order))


class TariffListView(ListAPIView):
    permission_classes = REVIEW_PERMISSIONS
    serializer_class = TariffSerializer

    def get_queryset(self):
        data = validated(PricingQuery, self.request.query_params)
        query = TariffRevision.objects.all()
        if data.get("service_id"):
            query = query.filter(service_id=data["service_id"])
        return query

    @extend_schema(parameters=[PricingQuery], tags=["Pricing management"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        request=TariffCreateSerializer, responses={201: TariffSerializer}, tags=["Pricing management"]
    )
    def post(self, request):
        tariff = services.create_tariff(request.user, validated(TariffCreateSerializer, request.data))
        return Response(TariffSerializer(tariff).data, status=201)


class PublishTariffView(APIView):
    permission_classes = REVIEW_PERMISSIONS

    @extend_schema(request=PublishSerializer, responses=TariffSerializer, tags=["Pricing management"])
    def post(self, request, tariff_id):
        tariff = services.publish_tariff(
            request.user, tariff_id, **validated(PublishSerializer, request.data)
        )
        return Response(TariffSerializer(tariff).data)


class IsPricingAdmin(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == "admin")


class PromoListView(ListAPIView):
    permission_classes = [IsAuthenticated, VerifiedAccount, IsPricingAdmin]
    serializer_class = PromoSerializer
    queryset = PromoCode.objects.all()

    @extend_schema(tags=["Pricing management"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        request=PromoCreateSerializer, responses={201: PromoSerializer}, tags=["Pricing management"]
    )
    def post(self, request):
        promo = services.create_promo(request.user, validated(PromoCreateSerializer, request.data))
        return Response(PromoSerializer(promo).data, status=201)


class PromoToggleView(APIView):
    permission_classes = PromoListView.permission_classes

    @extend_schema(request=PromoToggleSerializer, responses=PromoSerializer, tags=["Pricing management"])
    def patch(self, request, promo_id):
        promo = services.toggle_promo(
            request.user, promo_id, **validated(PromoToggleSerializer, request.data)
        )
        return Response(PromoSerializer(promo).data)
