from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.assistance.views import CLIENT, REVIEW
from apps.materials.services import get_order
from apps.pricing.views import validated

from . import services
from .serializers import ActionInput, CreateInput, EstimateQuery, MarketEstimateSerializer


class EstimatesView(ListAPIView):
    permission_classes = CLIENT
    serializer_class = MarketEstimateSerializer
    review = False

    def get_queryset(self):
        validated(EstimateQuery, self.request.query_params)
        order = get_order(self.request.user, self.kwargs["order_id"], self.review)
        return order.market_estimates.all() if self.review else order.market_estimates.filter(state="shared")


class ReviewEstimatesView(EstimatesView):
    permission_classes = REVIEW
    review = True

    @extend_schema(
        request=CreateInput,
        responses={201: MarketEstimateSerializer, 200: MarketEstimateSerializer},
        tags=["Comparable estimates"],
    )
    def post(self, request, order_id):
        obj, created = services.create(request.user, order_id, validated(CreateInput, request.data))
        return Response(MarketEstimateSerializer(obj).data, status=201 if created else 200)


class ShareView(APIView):
    permission_classes = REVIEW
    share = True

    @extend_schema(request=ActionInput, responses=MarketEstimateSerializer, tags=["Comparable estimates"])
    def post(self, request, order_id, estimate_id):
        obj = services.action(
            request.user, order_id, estimate_id, **validated(ActionInput, request.data), share=self.share
        )
        return Response(MarketEstimateSerializer(obj).data)


class WithdrawView(ShareView):
    share = False
