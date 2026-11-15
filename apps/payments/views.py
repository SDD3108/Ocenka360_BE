from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.core.views import PublicView
from apps.orders.permissions import VerifiedAccount
from apps.pricing.views import validated

from . import providers, services
from .models import CancellationCase, PaymentIntent, Refund
from .serializers import (
    BankConfirmationSerializer,
    CallbackResultSerializer,
    CallbackSerializer,
    CaseCreateSerializer,
    CaseDecisionSerializer,
    CaseOfferSerializer,
    CaseResultSerializer,
    CaseSerializer,
    CheckoutSerializer,
    NoInputSerializer,
    PaymentCreateSerializer,
    PaymentEventSerializer,
    PaymentMethodsSerializer,
    PaymentOverviewSerializer,
    PaymentQueueQuery,
    PaymentResultSerializer,
    PaymentSerializer,
    RefundExecuteSerializer,
    RefundSerializer,
)

REVIEW = [IsAuthenticated, VerifiedAccount, IsAppraiserOrAdmin]


def payment_result(intent, order):
    return PaymentResultSerializer(
        {
            "payment": intent,
            "order_version": order.version,
            "order_status": order.status,
            "balance": services.balance(order),
        }
    ).data


def case_result(case, order):
    return CaseResultSerializer(
        {"case": case, "order_version": order.version, "order_status": order.status}
    ).data


class MethodsView(PublicView):
    @extend_schema(responses=PaymentMethodsSerializer, tags=["Payments"])
    def get(self, request):
        return Response(
            PaymentMethodsSerializer(
                {
                    **providers.available_methods(),
                    "advance_amount": services.default_advance(),
                    "halyk_environment": settings.HALYK_ENVIRONMENT if providers.halyk_configured() else None,
                    "currency": "KZT",
                }
            ).data
        )


class PaymentsView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    review = False

    @extend_schema(responses=PaymentOverviewSerializer, tags=["Payments"])
    def get(self, request, order_id):
        order = services.get_order(request.user, order_id, review=self.review, queue=False)
        return Response(
            PaymentOverviewSerializer(
                {
                    "order_version": order.version,
                    "order_status": order.status,
                    "balance": services.balance(order),
                    "intents": order.payment_intents.select_related("receipt"),
                    "refunds": Refund.objects.filter(receipt__intent__order=order).select_related("entry"),
                }
            ).data
        )

    @extend_schema(
        request=PaymentCreateSerializer,
        responses={201: PaymentResultSerializer, 200: PaymentResultSerializer},
        tags=["Payments"],
    )
    def post(self, request, order_id):
        intent, order, created = services.create_intent(
            request.user, order_id, validated(PaymentCreateSerializer, request.data)
        )
        return Response(payment_result(intent, order), status=201 if created else 200)


class ReviewPaymentsView(PaymentsView):
    permission_classes = REVIEW
    review = True
    http_method_names = ["get", "head", "options"]


class CheckoutView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]

    @extend_schema(request=NoInputSerializer, responses=CheckoutSerializer, tags=["Payments"])
    def post(self, request, order_id, intent_id):
        validated(NoInputSerializer, request.data)
        return Response(services.checkout(request.user, order_id, intent_id))


class RefreshView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    review = False

    @extend_schema(request=NoInputSerializer, responses=PaymentResultSerializer, tags=["Payments"])
    def post(self, request, order_id, intent_id):
        validated(NoInputSerializer, request.data)
        intent, order = services.refresh_for_actor(request.user, order_id, intent_id, self.review)
        return Response(payment_result(intent, order))


class ReviewRefreshView(RefreshView):
    permission_classes = REVIEW
    review = True


class ManualPaymentView(APIView):
    permission_classes = REVIEW

    @extend_schema(
        request=BankConfirmationSerializer, responses=PaymentResultSerializer, tags=["Payment review"]
    )
    def post(self, request, order_id, intent_id):
        receipt, order = services.confirm_manual(
            request.user, order_id, intent_id, **validated(BankConfirmationSerializer, request.data)
        )
        return Response(payment_result(receipt.intent, order))


class RefundExecuteView(APIView):
    permission_classes = REVIEW

    @extend_schema(request=RefundExecuteSerializer, responses=RefundSerializer, tags=["Payment review"])
    def post(self, request, order_id, refund_id):
        refund, order = services.execute_refund(
            request.user, order_id, refund_id, **validated(RefundExecuteSerializer, request.data)
        )
        return Response(RefundSerializer(refund).data)


class ManualRefundView(APIView):
    permission_classes = REVIEW

    @extend_schema(request=BankConfirmationSerializer, responses=RefundSerializer, tags=["Payment review"])
    def post(self, request, order_id, refund_id):
        entry, order = services.confirm_manual_refund(
            request.user, order_id, refund_id, **validated(BankConfirmationSerializer, request.data)
        )
        return Response(RefundSerializer(entry.refund).data)


class PostlinkView(PublicView):
    @extend_schema(
        request=CallbackSerializer, responses={202: CallbackResultSerializer}, tags=["Bank callbacks"]
    )
    def post(self, request):
        if len(request.body) > 16384:
            raise ValidationError("Callback too large.")
        data = validated(CallbackSerializer, request.data)
        intent = get_object_or_404(PaymentIntent, provider="halyk", invoice_id=data["invoiceId"])
        if not providers.valid_callback(intent, data["secret_hash"]):
            raise PermissionDenied("Invalid callback.")
        # A valid callback only queues a check, never credits funds based on client-supplied JSON.
        PaymentIntent.objects.filter(pk=intent.pk, sync_requested_at__isnull=True).update(
            sync_requested_at=timezone.now()
        )
        return Response({"status": "queued_for_verification"}, status=202)


class PaymentHistoryView(ListAPIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    serializer_class = PaymentEventSerializer
    review = False

    def get_queryset(self):
        return services.get_order(
            self.request.user, self.kwargs["order_id"], review=self.review, queue=False
        ).payment_events.all()

    @extend_schema(tags=["Payments"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ReviewPaymentHistoryView(PaymentHistoryView):
    permission_classes = REVIEW
    review = True


class CancellationView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]
    review = False

    @extend_schema(responses=CaseSerializer(many=True), tags=["Cancellations"])
    def get(self, request, order_id):
        order = services.get_order(request.user, order_id, review=self.review)
        return Response(CaseSerializer(order.cancellation_cases.all(), many=True).data)

    @extend_schema(
        request=CaseCreateSerializer,
        responses={201: CaseResultSerializer, 200: CaseResultSerializer},
        tags=["Cancellations"],
    )
    def post(self, request, order_id):
        case, order, created = services.request_cancellation(
            request.user, order_id, **validated(CaseCreateSerializer, request.data)
        )
        return Response(case_result(case, order), status=201 if created else 200)


class ReviewCancellationView(CancellationView):
    permission_classes = REVIEW
    review = True
    http_method_names = ["get", "head", "options"]


class CaseOfferView(APIView):
    permission_classes = REVIEW

    @extend_schema(request=CaseOfferSerializer, responses=CaseResultSerializer, tags=["Cancellations"])
    def post(self, request, order_id, case_id):
        case, order = services.offer_cancellation(
            request.user, order_id, case_id, **validated(CaseOfferSerializer, request.data)
        )
        return Response(case_result(case, order))


class CaseDecisionView(APIView):
    permission_classes = [IsAuthenticated, VerifiedAccount]

    @extend_schema(request=CaseDecisionSerializer, responses=CaseResultSerializer, tags=["Cancellations"])
    def post(self, request, order_id, case_id):
        case, order = services.decide_cancellation(
            request.user, order_id, case_id, **validated(CaseDecisionSerializer, request.data)
        )
        return Response(case_result(case, order))


class PaymentQueueView(ListAPIView):
    permission_classes = REVIEW
    serializer_class = PaymentSerializer

    def get_queryset(self):
        data = validated(PaymentQueueQuery, self.request.query_params)
        query = PaymentIntent.objects.select_related("order", "receipt").order_by("-created_at", "-id")
        if not settings.ALLOW_ORDER_SIMULATION:
            query = query.filter(order__is_test=False)
        for key in ("state", "provider", "needs_review"):
            if key in data:
                query = query.filter(**{key: data[key]})
        return query

    @extend_schema(parameters=[PaymentQueueQuery], tags=["Payment review"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class RefundQueueView(ListAPIView):
    permission_classes = REVIEW
    serializer_class = RefundSerializer

    def get_queryset(self):
        query = Refund.objects.exclude(state="completed").select_related("entry").order_by("created_at", "id")
        if not settings.ALLOW_ORDER_SIMULATION:
            query = query.filter(receipt__intent__order__is_test=False)
        return query

    @extend_schema(tags=["Payment review"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class CancellationQueueView(ListAPIView):
    permission_classes = REVIEW
    serializer_class = CaseSerializer

    def get_queryset(self):
        query = CancellationCase.objects.filter(state__in=["requested", "offered"])
        if not settings.ALLOW_ORDER_SIMULATION:
            query = query.filter(order__is_test=False)
        return query

    @extend_schema(tags=["Cancellations"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)
