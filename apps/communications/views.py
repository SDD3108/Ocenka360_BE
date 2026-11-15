from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.materials.services import get_order
from apps.orders.permissions import VerifiedAccount
from apps.pricing.views import validated

from . import services
from .models import MaterialRequest
from .serializers import (
    CommentInput,
    CommentResult,
    CommentSerializer,
    DecisionInput,
    ReplyInput,
    ReplyResult,
    ReplySerializer,
    RequestInput,
    RequestResult,
    RequestSerializer,
)

REVIEW = [IsAuthenticated, VerifiedAccount, IsAppraiserOrAdmin]


class CommentsView(ListAPIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    serializer_class = CommentSerializer
    review = False

    def get_queryset(self):
        return get_order(self.request.user, self.kwargs["order_id"], self.review).comments.all()

    @extend_schema(
        request=CommentInput, responses={201: CommentResult, 200: CommentResult}, tags=["Communication"]
    )
    def post(self, request, order_id):
        obj, order, created = services.comment(
            request.user, order_id, review=self.review, **validated(CommentInput, request.data)
        )
        return Response(
            CommentResult({"comment": obj, "order_version": order.version}).data,
            status=201 if created else 200,
        )


class ReviewCommentsView(CommentsView):
    permission_classes = REVIEW
    review = True


class RequestsView(ListAPIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    serializer_class = RequestSerializer
    review = False

    def get_queryset(self):
        order = get_order(self.request.user, self.kwargs["order_id"], self.review)
        return order.material_requests.select_related("order")


class ReviewRequestsView(RequestsView):
    permission_classes = REVIEW
    review = True

    @extend_schema(
        request=RequestInput, responses={201: RequestResult, 200: RequestResult}, tags=["Communication"]
    )
    def post(self, request, order_id):
        obj, order, created = services.request_materials(
            request.user, order_id, **validated(RequestInput, request.data)
        )
        return Response(
            RequestResult(
                {"request": obj, "order_version": order.version, "order_status": order.status}
            ).data,
            status=201 if created else 200,
        )


class RepliesView(ListAPIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    serializer_class = ReplySerializer
    review = False

    def get_queryset(self):
        order = get_order(self.request.user, self.kwargs["order_id"], self.review)
        req = get_object_or_404(order.material_requests, pk=self.kwargs["request_id"])
        return req.replies.prefetch_related("links")

    @extend_schema(request=ReplyInput, responses={201: ReplyResult, 200: ReplyResult}, tags=["Communication"])
    def post(self, request, order_id, request_id):
        obj, order, created = services.respond(
            request.user, order_id, request_id, **validated(ReplyInput, request.data)
        )
        return Response(
            ReplyResult({"reply": obj, "order_version": order.version, "order_status": order.status}).data,
            status=201 if created else 200,
        )


class ReviewRepliesView(RepliesView):
    permission_classes = REVIEW
    review = True
    http_method_names = ["get", "head", "options"]


class DecisionView(APIView):
    permission_classes = REVIEW

    @extend_schema(request=DecisionInput, responses=RequestResult, tags=["Communication"])
    def post(self, request, order_id, request_id):
        obj, order = services.decide(
            request.user, order_id, request_id, **validated(DecisionInput, request.data)
        )
        return Response(
            RequestResult({"request": obj, "order_version": order.version, "order_status": order.status}).data
        )


class RequestQueueView(ListAPIView):
    permission_classes = REVIEW
    serializer_class = RequestSerializer

    def get_queryset(self):
        from apps.orders.services import review_queryset

        return (
            MaterialRequest.objects.filter(order__in=review_queryset(), state__in=["open", "submitted"])
            .select_related("order")
            .order_by("created_at", "id")
        )
