from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAppraiserOrAdmin
from apps.orders.permissions import VerifiedAccount
from apps.pricing.views import validated

from . import services
from .serializers import (
    SummaryQuery,
    SummarySerializer,
    TaskCreate,
    TaskEventSerializer,
    TaskPatch,
    TaskQuery,
    TaskSerializer,
    WorkQuery,
    WorkSerializer,
)

REVIEW = [IsAuthenticated, VerifiedAccount, IsAppraiserOrAdmin]


class TasksView(ListAPIView):
    permission_classes = REVIEW
    serializer_class = TaskSerializer

    def get_queryset(self):
        data = validated(TaskQuery, self.request.query_params.dict())
        query = services.task_query()
        for key in ["order_id", "state"]:
            if key in data:
                query = query.filter(**{key: data[key]})
        if data.get("overdue"):
            query = query.filter(state="open", due_at__lt=timezone.now())
        elif data.get("overdue") is False:
            query = query.exclude(state="open", due_at__lt=timezone.now())
        return query

    @extend_schema(parameters=[TaskQuery], tags=["Reviewer operations"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        request=TaskCreate, responses={201: TaskSerializer, 200: TaskSerializer}, tags=["Reviewer operations"]
    )
    def post(self, request):
        task, created = services.create_task(request.user, validated(TaskCreate, request.data))
        return Response(TaskSerializer(task).data, status=201 if created else 200)


class TaskView(APIView):
    permission_classes = REVIEW

    @extend_schema(responses=TaskSerializer, tags=["Reviewer operations"])
    def get(self, request, task_id):
        return Response(TaskSerializer(get_object_or_404(services.task_query(), pk=task_id)).data)

    @extend_schema(request=TaskPatch, responses=TaskSerializer, tags=["Reviewer operations"])
    def patch(self, request, task_id):
        return Response(
            TaskSerializer(
                services.update_task(request.user, task_id, validated(TaskPatch, request.data))
            ).data
        )


class TaskHistoryView(ListAPIView):
    permission_classes = REVIEW
    serializer_class = TaskEventSerializer

    def get_queryset(self):
        return get_object_or_404(services.task_query(), pk=self.kwargs["task_id"]).history.all()


class WorkQueueView(ListAPIView):
    permission_classes = REVIEW
    serializer_class = WorkSerializer

    def get_queryset(self):
        return services.work_queue(validated(WorkQuery, self.request.query_params)["overdue"])

    @extend_schema(parameters=[WorkQuery], tags=["Reviewer operations"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class SummaryView(APIView):
    permission_classes = REVIEW

    @extend_schema(parameters=[SummaryQuery], responses=SummarySerializer, tags=["Reviewer operations"])
    def get(self, request):
        data = validated(SummaryQuery, request.query_params)
        if data["include_test"] and not settings.ALLOW_ORDER_SIMULATION:
            raise ValidationError("Test statistics are disabled in this environment.")
        return Response(SummarySerializer(services.summary(**data)).data)
