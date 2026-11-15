import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from statistics import median

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.security import reserve_quota
from apps.materials.services import get_order
from apps.orders import services as orders

from .models import Estimate, EstimateEvent

NOTICE = {
    "ru": "Предварительный ориентир по введённым ценам предложений, не официальный отчёт и не стоимость услуги. Минимум/максимум описывают только эту выборку, а не доверительный интервал рынка. Аналоги и выводы проверяет оценщик.",
    "kk": "Енгізілген ұсыныс бағалары бойынша алдын ала бағдар, ресми есеп немесе қызмет ақысы емес. Минимум/максимум тек осы үлгіні сипаттайды. Аналогтар мен қорытындыны бағалаушы тексереді.",
    "en": "Preliminary indication from supplied asking prices, not an official report or service fee. Minimum/maximum describe this sample, not a market confidence interval. The appraiser verifies comparables and conclusions.",
}


def target(order):
    if order.service_snapshot.get("slug") != "apartment":
        raise ValidationError("This preliminary comparable method supports apartments only.")
    try:
        area = Decimal(str(order.answers.get("area_sqm", "")))
    except InvalidOperation:
        raise ValidationError("Valid subject area required.") from None
    if not area.is_finite() or not Decimal(5) <= area <= Decimal(2000):
        raise ValidationError("Subject area outside supported range.")
    city = str(order.answers.get("city", "")).strip()
    if not city:
        raise ValidationError("Subject city required.")
    return {"service": "apartment", "area_sqm": str(area), "city": city}


def money(value):
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate(subject, rows):
    area = Decimal(subject["area_sqm"])
    units = []
    for row in rows:
        if row["city"].strip().casefold() != subject["city"].casefold():
            raise ValidationError("All comparables must have the same city as the subject.")
        if not area / 2 <= row["area_sqm"] <= area * 2:
            raise ValidationError("Comparable area must be between half and twice the subject area.")
        units.append(row["asking_price_kzt"] / row["area_sqm"])
    mid = median(units)
    return {
        "method": "asking_price_median_per_sqm_v1",
        "currency": "KZT",
        "sample_size": len(units),
        "median_per_sqm": money(mid),
        "indication": money(mid * area),
        "sample_min_indication": money(min(units) * area),
        "sample_max_indication": money(max(units) * area),
        "adjustments_applied": False,
        "automatic_market_data": False,
    }


@transaction.atomic
def create(actor, order_id, data):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    order = get_order(actor, order_id, True, lock=True)
    orders.supported(order)
    fingerprint = hashlib.sha256(
        json.dumps({**data, "order_id": str(order_id)}, default=str, sort_keys=True).encode()
    ).hexdigest()
    old = Estimate.objects.filter(created_by=actor, client_request_id=data["client_request_id"]).first()
    if old:
        if old.fingerprint != fingerprint:
            raise orders.Conflict(code="idempotency_conflict")
        return old, False
    orders.check_version(order, data["expected_version"])
    if order.market_estimates.count() >= 30:
        raise orders.Conflict("Estimate limit for this order reached.")
    reserve_quota("market-estimate", str(actor.pk), 20, 3600)
    subject = target(order)
    result = calculate(subject, data["comparables"])
    result["notice"] = NOTICE[order.language]
    snapshot = json.loads(
        json.dumps(
            {
                "subject": subject,
                "as_of": data["as_of"],
                "comparables": data["comparables"],
                "note": data["note"],
                "comparability_confirmed": True,
            },
            default=str,
        )
    )
    obj = Estimate.objects.create(
        order=order,
        created_by=actor,
        client_request_id=data["client_request_id"],
        fingerprint=fingerprint,
        order_version=order.version,
        snapshot=snapshot,
        result=result,
    )
    EstimateEvent.objects.create(estimate=obj, actor=actor, action="created")
    return obj, True


@transaction.atomic
def action(actor, order_id, estimate_id, expected_version, confirmed, reason, share):
    actor = orders.actor_for_write(actor, consent=False, reviewer=True)
    order = get_order(actor, order_id, True, lock=True)
    orders.supported(order)
    obj = get_object_or_404(order.market_estimates, pk=estimate_id)
    if not confirmed:
        raise ValidationError("Explicit confirmation required.")
    if share and obj.state == "shared":
        return obj
    if not share and obj.state == "withdrawn":
        previous = obj.events.filter(action="withdrawn").order_by("-created_at", "-id").first()
        if not previous or previous.note != reason:
            raise orders.Conflict("Withdrawal already recorded with a different reason.")
        return obj
    orders.check_version(obj, expected_version)
    if share:
        if obj.state != "draft":
            raise orders.Conflict("Only a new draft may be shared.")
        if target(order) != obj.snapshot["subject"]:
            raise orders.Conflict("Subject changed; create a new estimate.")
        obj.state = "shared"
        obj.shared_at = timezone.now()
    else:
        if not reason.strip():
            raise ValidationError({"reason": "Explain withdrawal."})
        obj.state = "withdrawn"
        obj.withdrawn_at = timezone.now()
    obj.version += 1
    obj.save(update_fields=["state", "version", "shared_at", "withdrawn_at"])
    EstimateEvent.objects.create(
        estimate=obj, actor=actor, action="shared" if share else "withdrawn", note=reason
    )
    return obj
