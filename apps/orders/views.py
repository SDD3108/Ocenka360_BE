from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.catalog.schema import localized_definition
from apps.catalog.serializers import FormSerializer, LanguageQuery
from apps.core.serializers import ErrorSerializer

from . import services
from .models import Asset, Order
from .permissions import ConsentForWrites, VerifiedAccount
from .serializers import (
    AssetCreateSerializer,
    AssetPatchSerializer,
    AssetQuery,
    AssetSerializer,
    CancelSerializer,
    OrderCreateSerializer,
    OrderDetailSerializer,
    OrderEventSerializer,
    OrderPatchSerializer,
    OrderQuery,
    OrderSubmissionSerializer,
    OrderSummarySerializer,
    ReviewActionSerializer,
    ReviewOrderDetailSerializer,
    SubmitSerializer,
)


class CustomerView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount, ConsentForWrites]


class AssetListView(ListAPIView):
    permission_classes = CustomerView.permission_classes
    serializer_class = AssetSerializer

    def get_queryset(self):
        query = AssetQuery(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        return Asset.objects.filter(owner=self.request.user, is_archived=query.validated_data["archived"])

    @extend_schema(parameters=[AssetQuery], tags=["Assets"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(request=AssetCreateSerializer, responses={201: AssetSerializer}, tags=["Assets"])
    def post(self, request):
        serializer = AssetCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        asset = services.create_asset(request.user, serializer.validated_data)
        return Response(AssetSerializer(asset).data, status=201)


class AssetDetailView(CustomerView):
    @extend_schema(responses=AssetSerializer, tags=["Assets"])
    def get(self, request, asset_id):
        return Response(AssetSerializer(get_object_or_404(Asset, pk=asset_id, owner=request.user)).data)

    @extend_schema(
        request=AssetPatchSerializer, responses={200: AssetSerializer, 409: ErrorSerializer}, tags=["Assets"]
    )
    def patch(self, request, asset_id):
        data = AssetPatchSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(
            AssetSerializer(services.update_asset(request.user, asset_id, data.validated_data)).data
        )


class OrderListView(ListAPIView):
    permission_classes = CustomerView.permission_classes
    serializer_class = OrderSummarySerializer

    def get_queryset(self):
        query = OrderQuery(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        orders = Order.objects.filter(owner=self.request.user)
        if "status" in query.validated_data:
            orders = orders.filter(status=query.validated_data["status"])
        return orders

    @extend_schema(parameters=[OrderQuery], tags=["Orders"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        request=OrderCreateSerializer,
        responses={201: OrderDetailSerializer, 200: OrderDetailSerializer, 409: ErrorSerializer},
        tags=["Orders"],
    )
    def post(self, request):
        data = OrderCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        order, created = services.create_order(request.user, data.validated_data)
        return Response(OrderDetailSerializer(order).data, status=201 if created else 200)


class OrderDetailView(CustomerView):
    @extend_schema(responses=OrderDetailSerializer, tags=["Orders"])
    def get(self, request, order_id):
        return Response(OrderDetailSerializer(get_object_or_404(Order, pk=order_id, owner=request.user)).data)

    @extend_schema(
        request=OrderPatchSerializer,
        responses={200: OrderDetailSerializer, 409: ErrorSerializer},
        tags=["Orders"],
    )
    def patch(self, request, order_id):
        data = OrderPatchSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(
            OrderDetailSerializer(services.update_order(request.user, order_id, data.validated_data)).data
        )


class OrderSubmitView(CustomerView):
    @extend_schema(
        request=SubmitSerializer,
        responses={200: OrderDetailSerializer, 409: ErrorSerializer},
        tags=["Orders"],
    )
    def post(self, request, order_id):
        data = SubmitSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(
            OrderDetailSerializer(services.submit_order(request.user, order_id, **data.validated_data)).data
        )


class OrderCancelView(CustomerView):
    permission_classes = [IsAuthenticated, VerifiedAccount]

    @extend_schema(
        request=CancelSerializer,
        responses={200: OrderDetailSerializer, 409: ErrorSerializer},
        tags=["Orders"],
    )
    def post(self, request, order_id):
        data = CancelSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(
            OrderDetailSerializer(services.cancel_order(request.user, order_id, **data.validated_data)).data
        )


class OrderHistoryView(ListAPIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    serializer_class = OrderEventSerializer
    review = False
    submissions = False

    def get_queryset(self):
        orders = services.review_queryset() if self.review else Order.objects.filter(owner=self.request.user)
        order = get_object_or_404(orders, pk=self.kwargs["order_id"])
        if self.submissions:
            return order.submissions.all()
        query = order.events.all()
        if not self.review:
            query = query.exclude(
                action__in=["report_uploaded", "report_draft_discarded", "report_overdue", "review_task_due"]
            )
        return query

    @extend_schema(tags=["Orders"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class OrderSubmissionsView(OrderHistoryView):
    serializer_class = OrderSubmissionSerializer
    submissions = True


class ReviewListView(ListAPIView):
    permission_classes = [IsAuthenticated, VerifiedAccount, IsAppraiserOrAdmin]
    serializer_class = OrderSummarySerializer

    def get_queryset(self):
        query = OrderQuery(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        orders = services.review_queryset()
        if "status" in query.validated_data:
            orders = orders.filter(status=query.validated_data["status"])
        return orders

    @extend_schema(parameters=[OrderQuery], tags=["Order review"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ReviewDetailView(APIView):
    permission_classes = ReviewListView.permission_classes

    @extend_schema(responses=ReviewOrderDetailSerializer, tags=["Order review"])
    def get(self, request, order_id):
        return Response(
            ReviewOrderDetailSerializer(get_object_or_404(services.review_queryset(), pk=order_id)).data
        )


class ReviewActionView(ReviewDetailView):
    http_method_names = ["post", "options"]

    @extend_schema(
        request=ReviewActionSerializer,
        responses={200: ReviewOrderDetailSerializer, 409: ErrorSerializer},
        tags=["Order review"],
    )
    def post(self, request, order_id):
        data = ReviewActionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(
            ReviewOrderDetailSerializer(
                services.review_action(request.user, order_id, **data.validated_data)
            ).data
        )


class ReviewHistoryView(OrderHistoryView):
    permission_classes = ReviewListView.permission_classes
    review = True

    @extend_schema(tags=["Order review"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ReviewSubmissionsView(ReviewHistoryView):
    serializer_class = OrderSubmissionSerializer
    submissions = True


class OrderFormView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    review = False

    @extend_schema(parameters=[LanguageQuery], responses=FormSerializer, tags=["Orders"])
    def get(self, request, order_id):
        orders = services.review_queryset() if self.review else Order.objects.filter(owner=request.user)
        order = get_object_or_404(orders.select_related("form"), pk=order_id)
        params = request.query_params.copy()
        params.setdefault("language", order.language)
        query = LanguageQuery(data=params)
        query.is_valid(raise_exception=True)
        language = query.validated_data["language"]
        form = order.form
        return Response(
            {
                "id": form.id,
                "version": form.version,
                "checksum": form.checksum,
                "service": order.service_snapshot["slug"],
                "language": language,
                **localized_definition(form.definition, language),
            }
        )


class ReviewFormView(OrderFormView):
    permission_classes = ReviewListView.permission_classes
    review = True

    @extend_schema(parameters=[LanguageQuery], responses=FormSerializer, tags=["Order review"])
    def get(self, request, order_id):
        return super().get(request, order_id)
