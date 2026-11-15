from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import APIException
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.materials.services import get_order
from apps.orders.permissions import VerifiedAccount
from apps.pricing.views import validated

from . import services
from .models import Job, Permission
from .policy import NOTICE, POLICY_VERSION, availability, enabled
from .serializers import (
    AIDecisionInput,
    EmptyInput,
    JobInput,
    JobQuery,
    JobSerializer,
    PermissionInput,
    PermissionSerializer,
    ToolsSerializer,
)

CLIENT = [IsAuthenticated, VerifiedAccount]
REVIEW = [*CLIENT, IsAppraiserOrAdmin]


def render(job):
    data = JobSerializer(job).data
    # Retention/revocation/staleness remains effective even if cron has not run.
    if job.state == "succeeded":
        try:
            services.authorized(job)
        except (APIException, Http404):
            data.update(result={}, stale=True)
    if job.expires_at <= timezone.now():
        data.update(state="expired", result={}, decision_note="", error_code="retention_expired")
    return data


class ToolsView(APIView):
    permission_classes = CLIENT

    @extend_schema(responses=ToolsSerializer, tags=["Assistance"])
    def get(self, request):
        return Response(
            ToolsSerializer(
                {
                    "policy_version": POLICY_VERSION,
                    "notice": NOTICE[request.user.preferred_language],
                    "ai_status": availability(),
                    "ai_configured": enabled(),
                    "allowed_kinds": [
                        k
                        for k in Job.Kind.values
                        if k != "description_draft" or request.user.role in {"admin", "appraiser"}
                    ],
                    "market_mode": "reviewer_supplied_comparables",
                    "market_supported_services": ["apartment"],
                    "damage_calculation_status": "provider_not_configured",
                    "service_fee_calculator": "/api/v1/pricing/services/{slug}/estimate/",
                }
            ).data
        )


class PermissionView(APIView):
    permission_classes = CLIENT

    @extend_schema(responses=PermissionSerializer, tags=["Assistance"])
    def get(self, request, order_id):
        order = get_order(request.user, order_id)
        obj = Permission.objects.filter(order=order).first()
        return Response(
            PermissionSerializer(obj).data
            if obj
            else {
                "order_id": str(order.pk),
                "policy_version": POLICY_VERSION,
                "version": 0,
                "enabled": False,
                "allow_photos": False,
                "updated_at": None,
            }
        )

    @extend_schema(request=PermissionInput, responses=PermissionSerializer, tags=["Assistance"])
    def post(self, request, order_id):
        obj = services.set_permission(request.user, order_id, **validated(PermissionInput, request.data))
        return Response(PermissionSerializer(obj).data)


class JobsView(ListAPIView):
    permission_classes = CLIENT
    serializer_class = JobSerializer
    review = False

    def get_queryset(self):
        validated(JobQuery, self.request.query_params)
        return services.visible(self.request.user, self.review)

    @extend_schema(parameters=[JobQuery], responses=JobSerializer(many=True), tags=["Assistance"])
    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response([render(job) for job in page])

    @extend_schema(request=JobInput, responses={202: JobSerializer, 200: JobSerializer}, tags=["Assistance"])
    def post(self, request):
        job, created = services.create_job(request.user, validated(JobInput, request.data), self.review)
        return Response(render(job), status=202 if created else 200)


class ReviewJobsView(JobsView):
    permission_classes = REVIEW
    review = True


class JobView(APIView):
    permission_classes = CLIENT
    review = False

    @extend_schema(responses=JobSerializer, tags=["Assistance"])
    def get(self, request, job_id):
        return Response(render(get_object_or_404(services.visible(request.user, self.review), pk=job_id)))


class ReviewJobView(JobView):
    permission_classes = REVIEW
    review = True


class CancelView(JobView):
    @extend_schema(request=EmptyInput, responses=JobSerializer, tags=["Assistance"])
    def post(self, request, job_id):
        validated(EmptyInput, request.data)
        return Response(render(services.cancel_job(request.user, job_id, self.review)))


class ReviewCancelView(CancelView):
    permission_classes = REVIEW
    review = True


class DecisionView(APIView):
    permission_classes = REVIEW

    @extend_schema(request=AIDecisionInput, responses=JobSerializer, tags=["Assistance"])
    def post(self, request, job_id):
        return Response(
            render(services.decide_job(request.user, job_id, **validated(AIDecisionInput, request.data)))
        )
