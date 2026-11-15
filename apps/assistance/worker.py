from datetime import timedelta

from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import APIException

from apps.accounts.models import User
from apps.materials.services import get_order
from apps.orders import services as orders

from . import provider, services
from .models import Job
from .policy import enabled


@transaction.atomic
def claim():
    if not enabled():
        return None
    query = Job.objects.filter(state="queued", expires_at__gt=timezone.now()).order_by("created_at", "id")
    query = (
        query.select_for_update(skip_locked=True)
        if connection.features.has_select_for_update_skip_locked
        else query.select_for_update()
    )
    job = query.first()
    if not job:
        return None
    job.state = "running"
    job.claimed_at = timezone.now()
    job.save(update_fields=["state", "claimed_at"])
    return job.pk


def lock_job(job_id):
    ref = Job.objects.select_related("actor").get(pk=job_id)
    actor = orders.actor_for_write(ref.actor, consent=True, reviewer=ref.audience == "review")
    if ref.order_id:
        get_order(actor, ref.order_id, ref.audience == "review", lock=True)
    return Job.objects.select_for_update(of=("self",)).select_related("actor", "order").get(pk=job_id)


def mark_failure(job_id, code, state="failed"):
    # CAS prevents a late response overwriting cancellation, expiry or another terminal state.
    Job.objects.filter(pk=job_id, state="running").update(
        state=state, error_code=code, result={}, finished_at=timezone.now()
    )


def run_one(job_id):
    try:
        with transaction.atomic():
            job = lock_job(job_id)
            if job.state != "running":
                return
            services.authorized(job)
        images = services.read_images(job)
        # Recheck consent/version immediately before the network; never hold DB locks across it.
        with transaction.atomic():
            job = lock_job(job_id)
            if job.state != "running":
                return
            services.authorized(job)
        result, usage = provider.generate(job.context, images, job.model_name)
        with transaction.atomic():
            current = lock_job(job_id)
            if current.state != "running":
                return
            services.authorized(current)
            current.result = {
                **result,
                "answer": services.redact(result["answer"]),
                "observations": [services.redact(x) for x in result["observations"]],
                "warnings": [services.redact(x) for x in result["warnings"]],
            }
            current.input_tokens = usage["promptTokenCount"]
            current.output_tokens = usage["candidatesTokenCount"]
            current.state = "succeeded"
            current.finished_at = timezone.now()
            current.save(update_fields=["result", "input_tokens", "output_tokens", "state", "finished_at"])
    except provider.ProviderError as exc:
        mark_failure(job_id, exc.code, "unknown" if exc.uncertain else "failed")
    except (APIException, Http404, User.DoesNotExist):
        mark_failure(job_id, "input_or_permission_changed", "stale")
    except (OSError, ValueError):
        mark_failure(job_id, "photo_unavailable")
    except Exception:
        # No provider exception text, prompts or secrets in stdout/logs; interrupted attempt is not replayed.
        mark_failure(job_id, "worker_error", "unknown")


def recover():
    return Job.objects.filter(state="running", claimed_at__lt=timezone.now() - timedelta(minutes=5)).update(
        state="unknown", error_code="interrupted_worker", result={}, finished_at=timezone.now()
    )


def cleanup():
    return (
        Job.objects.filter(expires_at__lte=timezone.now())
        .exclude(state="expired")
        .update(
            state="expired",
            context={},
            photo_refs=[],
            result={},
            decision_note="",
            error_code="retention_expired",
            finished_at=timezone.now(),
        )
    )


def process(limit=10):
    recover()
    cleanup()
    processed = 0
    for _ in range(limit):
        ident = claim()
        if not ident:
            break
        run_one(ident)
        processed += 1
    return processed
