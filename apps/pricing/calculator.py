from decimal import ROUND_HALF_UP, Decimal

from .models import MAX_MONEY, ServiceTariff, TariffRevision

CENT = Decimal("0.01")
NOTICE = {
    "ru": "Предварительная стоимость услуги. Итоговую стоимость утверждает оценщик.",
    "kk": "Қызметтің алдын ала құны. Соңғы құнын бағалаушы бекітеді.",
    "en": "Estimated service fee. The appraiser confirms the final price.",
}
ASTANA = {"астана", "astana", "нұр-сұлтан", "нур-султан", "nur-sultan"}


def amount(value):
    return format(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def estimate(service, city, urgency, area_sqm=None, language="ru"):
    binding = ServiceTariff.objects.select_related("current").filter(service=service).first()
    tariff = binding.current if binding else None
    result = {
        "currency": "KZT",
        "service_id": str(service.pk),
        "tariff_id": str(tariff.pk) if tariff else None,
        "tariff_version": tariff.version if tariff else None,
        "status": "requires_quote",
        "reason": "tariff_unconfigured",
        "subtotal": None,
        "breakdown": [],
        "notice": NOTICE[language],
        "is_final": False,
    }
    if not tariff:
        return result
    if tariff.mode == TariffRevision.Mode.MANUAL:
        result["reason"] = "individual_pricing"
        return result
    if city.strip().casefold() not in ASTANA:
        result["reason"] = "travel_requires_quote"
        return result
    lines = [{"code": "base", "amount": amount(tariff.base_amount)}]
    if tariff.mode == TariffRevision.Mode.AREA:
        if area_sqm is None:
            result["reason"] = "area_required"
            return result
        extra = max(Decimal("0"), Decimal(area_sqm) - tariff.included_area)
        lines.append({"code": "extra_area", "amount": amount(extra * tariff.per_sqm)})
    if urgency == "urgent":
        if tariff.urgent_surcharge is None:
            result["reason"] = "urgency_requires_quote"
            return result
        lines.append({"code": "urgency", "amount": amount(tariff.urgent_surcharge)})
    total = sum(Decimal(line["amount"]) for line in lines)
    if total > MAX_MONEY:
        result["reason"] = "amount_requires_quote"
        return result
    result.update(status="estimated", reason="", subtotal=amount(total), breakdown=lines)
    return result
