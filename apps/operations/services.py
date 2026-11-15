import hashlib
import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.accounts.security import reserve_quota
from apps.materials.services import get_order
from apps.orders import services as orders
from apps.orders.models import Order
from apps.payments.models import Receipt, Refund, RefundEntry
from apps.reports.models import Original, Report

from .models import Reminder, Task, TaskEvent

CLOSED = {Order.Status.CANCELLED, Order.Status.REJECTED, Order.Status.SERVICE_STOPPED}


def task_query():
    return Task.objects.filter(order__in=orders.review_queryset()).select_related("order")


def task_event(task, actor):
    TaskEvent.objects.create(
        task=task,
        actor=actor,
        version=task.version,
        snapshot={
            "title": task.title,
            "note": task.note,
            "due_at": task.due_at.isoformat(),
            "state": task.state,
        },
    )


@transaction.atomic
def create_task(actor, data):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
    old = Task.objects.filter(created_by=actor, client_request_id=data["client_request_id"]).first()
    if old:
        get_order(actor, old.order_id, True)
        if old.fingerprint != digest:
            raise orders.Conflict("Task request id used with different fields.")
        return old, False
    order = get_order(actor, data["order_id"], True, lock=True)
    orders.supported(order)
    reserve_quota("review-task", str(actor.pk), 50, 3600)
    if order.review_tasks.count() >= 100:
        raise orders.Conflict("Task limit for this order reached.")
    task = Task.objects.create(created_by=actor, fingerprint=digest, **data)
    task_event(task, actor)
    return task, True


@transaction.atomic
def update_task(actor, task_id, data):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    ref = get_object_or_404(task_query(), pk=task_id)
    order = get_order(actor, ref.order_id, True, lock=True)
    orders.supported(order)
    task = Task.objects.select_for_update().get(pk=task_id)
    data = dict(data)
    orders.check_version(task, data.pop("expected_version"))
    for key, value in data.items():
        setattr(task, key, value)
    task.completed_at = (task.completed_at or timezone.now()) if task.state == "done" else None
    task.version += 1
    task.save()
    task_event(task, actor)
    return task


def work_queue(overdue=False):
    query = orders.review_queryset().exclude(status__in=CLOSED)
    query = query.filter(
        ~Q(status="completed")
        | Q(reports__state="published", reports__original__state__in=["preparing", "ready"])
    )
    if overdue:
        query = query.filter(
            status__in=["in_progress", "awaiting_report"], inspection__report_due_at__lt=timezone.now()
        )
    return (
        query.select_related("inspection")
        .prefetch_related("reports__original")
        .order_by(F("inspection__report_due_at").asc(nulls_last=True), "created_at", "id")
        .distinct()
    )


def next_action(order):
    mapping = {
        "submitted": "review",
        "under_review": "review",
        "needs_information": "wait_client",
        "accepted": "agree_price",
        "price_agreed": "plan_inspection",
        "inspection_scheduled": "inspect",
        "in_progress": "finish_inspection",
        "awaiting_report": "prepare_report",
    }
    if order.status == "completed":
        report = next((r for r in order.reports.all() if r.state == "published"), None)
        if report:
            return "prepare_original" if report.original.state == "preparing" else "handover_original"
    return mapping.get(order.status, "review")


def summary(start_date, end_date, include_test=False):
    zone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(start_date, time.min), zone)
    end = timezone.make_aware(datetime.combine(end_date + timedelta(days=1), time.min), zone)
    query = Order.objects.all()
    if not include_test:
        query = query.filter(is_test=False)
    incoming = query.filter(review_unlocked_at__gte=start, review_unlocked_at__lt=end)
    receipts = Receipt.objects.filter(intent__order__in=query, created_at__gte=start, created_at__lt=end)
    refunds = RefundEntry.objects.filter(
        refund__receipt__intent__order__in=query, created_at__gte=start, created_at__lt=end
    )
    gross = receipts.aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    returned = refunds.aggregate(v=Sum("amount"))["v"] or Decimal("0.00")
    return {
        "start_date": start_date,
        "end_date": end_date,
        "timezone": settings.TIME_ZONE,
        "include_test": include_test,
        "currency": "KZT",
        "new_paid_orders": incoming.count(),
        "completed_orders": query.filter(
            status="completed", completed_at__gte=start, completed_at__lt=end
        ).count(),
        "report_publications": Report.objects.filter(
            order__in=query, published_at__gte=start, published_at__lt=end
        ).count(),
        "active_orders": query.filter(review_unlocked_at__isnull=False)
        .exclude(status__in=CLOSED | {"completed"})
        .count(),
        "overdue_reports": query.filter(
            status__in=["in_progress", "awaiting_report"], inspection__report_due_at__lt=timezone.now()
        ).count(),
        "overdue_manual_tasks": Task.objects.filter(
            order__in=query, state="open", due_at__lt=timezone.now()
        ).count(),
        "originals_ready": Original.objects.filter(
            report__order__in=query, report__state="published", state="ready"
        ).count(),
        "pending_refunds": Refund.objects.filter(receipt__intent__order__in=query)
        .exclude(state="completed")
        .count(),
        "gross_receipts": gross,
        "refunds": returned,
        "net_cash_flow": gross - returned,
        "sources": list(
            incoming.values("acquisition_source").annotate(count=Count("id")).order_by("acquisition_source")
        ),
        "current_statuses": list(
            query.filter(review_unlocked_at__isnull=False)
            .values("status")
            .annotate(count=Count("id"))
            .order_by("status")
        ),
    }


def notify_overdue(limit=100):
    now, day = timezone.now(), timezone.localdate()
    visible = orders.review_queryset()
    # Exclude already sent keys before slicing: old reminders must not starve later orders.
    notified_orders = Reminder.objects.filter(kind="report", local_date=day).values("order_id")
    report_ids = list(
        visible.filter(status__in=["in_progress", "awaiting_report"], inspection__report_due_at__lt=now)
        .exclude(pk__in=notified_orders)
        .order_by("inspection__report_due_at", "id")
        .values_list("pk", flat=True)[:limit]
    )
    count = 0
    for order_id in report_ids:
        with transaction.atomic():
            order = visible.select_for_update().get(pk=order_id)
            if (
                order.status not in {"in_progress", "awaiting_report"}
                or order.inspection.report_due_at >= timezone.now()
            ):
                continue
            _, created = Reminder.objects.get_or_create(
                key=f"report:{order.pk}:{day}", defaults={"kind": "report", "order": order, "local_date": day}
            )
            if created:
                orders.changed(order, None, "report_overdue", order.status)
                count += 1
    remaining = max(0, limit - count)
    notified_tasks = Reminder.objects.filter(kind="task", local_date=day).values("task_id")
    tasks = list(
        task_query()
        .filter(state="open", due_at__lt=now)
        .exclude(pk__in=notified_tasks)
        .order_by("due_at", "id")
        .values_list("pk", "order_id")[:remaining]
    )
    for task_id, order_id in tasks:
        with transaction.atomic():
            order = visible.select_for_update().get(pk=order_id)
            task = Task.objects.select_for_update().get(pk=task_id)
            if task.state != "open" or task.due_at >= timezone.now():
                continue
            _, created = Reminder.objects.get_or_create(
                key=f"task:{task.pk}:{day}",
                defaults={"kind": "task", "order": order, "task": task, "local_date": day},
            )
            if created:
                orders.changed(order, None, "review_task_due", order.status, str(task.pk))
                count += 1
    return count
