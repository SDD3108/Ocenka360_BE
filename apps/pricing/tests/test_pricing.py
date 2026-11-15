import io
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.core.exceptions import ValidationError as ModelValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import AuthRateBucket
from apps.catalog.models import Service
from apps.orders import services as orders
from apps.orders.models import Order
from apps.orders.tests.test_orders import ROOT, OrderFixture
from apps.pricing import services
from apps.pricing.models import PriceQuote, PromoCode, PromoRedemption, ServiceTariff, TariffRevision

MANAGE = ROOT + "management/pricing/"


class PricingFixture(OrderFixture):
    def setUp(self):
        super().setUp()
        self.admin = self.make_user(
            "pricing-admin@example.com", role="admin", is_staff=True, is_superuser=True
        )

    def tariff(self, **extra):
        return services.create_tariff(
            self.reviewer,
            {
                "service_id": self.service.pk,
                "mode": "flat",
                "base_amount": Decimal("20000.00"),
                **extra,
            },
        )

    def publish(self, tariff):
        binding = ServiceTariff.objects.filter(service=tariff.service).first()
        return services.publish_tariff(self.reviewer, tariff.pk, binding.current_id if binding else None)

    def promo(self, **extra):
        return services.create_promo(
            self.admin,
            {
                "code": "TEST10",
                "kind": "percent",
                "value": Decimal("10.00"),
                "starts_at": timezone.now() - timedelta(hours=1),
                "ends_at": timezone.now() + timedelta(days=3),
                **extra,
            },
        )

    def negotiable(self):
        order = self.submitted()
        order = orders.review_action(self.reviewer, order.pk, order.version, "start_review")
        return orders.review_action(self.reviewer, order.pk, order.version, "accept")

    def quote_data(self, order, **extra):
        return {
            "expected_version": order.version,
            "client_request_id": str(uuid.uuid4()),
            "lines": [{"kind": "appraisal", "title": "Оценка с осмотром", "amount": "20000.00"}],
            "terms": "TEST ONLY: осмотр и электронная копия; бумажный оригинал в офисе.",
            "expires_at": (timezone.now() + timedelta(hours=24)).isoformat(),
            **extra,
        }

    def issue(self, order, **extra):
        self.api.force_authenticate(self.reviewer)
        return self.api.post(ROOT + f"review/orders/{order.pk}/pricing/", self.quote_data(order, **extra))

    def issued(self, order, **extra):
        response = self.issue(order, **extra)
        self.assertEqual(response.status_code, 201, response.json())
        order.refresh_from_db()
        return PriceQuote.objects.get(pk=response.json()["quote"]["id"])

    def accept(self, order, quote, **extra):
        self.api.force_authenticate(self.owner)
        order.refresh_from_db()
        return self.api.post(
            ROOT + f"orders/{order.pk}/pricing/{quote.pk}/accept/",
            {
                "expected_version": order.version,
                "confirmed": True,
                **extra,
            },
        )


class TariffTests(PricingFixture):
    def public_estimate(self, **extra):
        self.api.force_authenticate(None)
        return self.api.post(ROOT + "pricing/services/apartment/estimate/", {"city": "Астана", **extra})

    def test_no_default_invented_price(self):
        data = self.public_estimate().json()
        self.assertEqual(data["status"], "requires_quote")
        self.assertEqual(data["reason"], "tariff_unconfigured")
        self.assertIsNone(data["subtotal"])
        self.assertFalse(data["is_final"])

    def test_public_estimate_only_current_published_tariff(self):
        tariff = self.tariff()
        self.assertIsNone(self.public_estimate().json()["subtotal"])
        self.publish(tariff)
        response = self.public_estimate(language="kk")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subtotal"], "20000.00")
        self.assertIn("бағалаушы", response.json()["notice"])
        self.tariff(base_amount=Decimal("30000.00"))
        self.assertEqual(self.public_estimate().json()["subtotal"], "20000.00")

    def test_area_and_urgency_use_decimal_rounding(self):
        self.publish(
            self.tariff(
                mode="area",
                included_area=Decimal("50.00"),
                per_sqm=Decimal("100.01"),
                urgent_surcharge=Decimal("5000.00"),
            )
        )
        data = self.public_estimate(area_sqm="50.50", urgency="urgent").json()
        self.assertEqual(data["subtotal"], "25050.01")
        self.assertEqual(data["breakdown"][1]["amount"], "50.01")
        self.assertEqual(self.public_estimate(area_sqm="40.00").json()["subtotal"], "20000.00")
        self.assertEqual(self.public_estimate().json()["reason"], "area_required")

    def test_manual_and_unknown_travel_urgent_require_appraiser(self):
        self.publish(self.tariff())
        self.assertEqual(self.public_estimate(city="Алматы").json()["reason"], "travel_requires_quote")
        self.assertEqual(self.public_estimate(urgency="urgent").json()["reason"], "urgency_requires_quote")
        self.publish(self.tariff(mode="manual", base_amount=None))
        self.assertEqual(self.public_estimate().json()["reason"], "individual_pricing")

    def test_tariff_revision_conflict_and_immutable_published_values(self):
        old = self.publish(self.tariff())
        new = self.tariff(base_amount=Decimal("25000.00"))
        with self.assertRaises(orders.Conflict):
            services.publish_tariff(self.reviewer, new.pk, None)
        self.publish(new)
        old.base_amount = Decimal("1.00")
        with self.assertRaises(ModelValidationError):
            old.save()
        self.assertEqual(self.public_estimate().json()["subtotal"], "25000.00")
        with self.assertRaises(orders.Conflict):
            services.publish_tariff(self.reviewer, old.pk, new.pk)

    def test_staff_api_rejects_clients_and_accepts_reviewer(self):
        self.assertEqual(self.api.get(MANAGE + "tariffs/").status_code, 403)
        self.api.force_authenticate(self.reviewer)
        data = {"service_id": str(self.service.pk), "mode": "flat", "base_amount": "20000.00"}
        created = self.api.post(MANAGE + "tariffs/", data)
        self.assertEqual(created.status_code, 201, created.json())
        self.assertFalse(created.json()["is_current"])
        published = self.api.post(
            MANAGE + f"tariffs/{created.json()['id']}/publish/", {"expected_current_id": None}
        )
        self.assertEqual(published.status_code, 200, published.json())
        self.assertTrue(published.json()["is_current"])

    def test_bad_tariff_and_money_input_rejected(self):
        self.api.force_authenticate(self.reviewer)
        for extra in (
            {"base_amount": 20000},
            {"base_amount": "NaN"},
            {"base_amount": "0.00"},
            {"base_amount": "1.001"},
            {"mode": "manual", "base_amount": "20000"},
            {"mode": "area"},
            {"published_at": timezone.now().isoformat()},
        ):
            data = {"service_id": str(self.service.pk), "mode": "flat", "base_amount": "20000.00", **extra}
            self.assertEqual(self.api.post(MANAGE + "tariffs/", data).status_code, 400)

    def test_public_inputs_strict_and_inactive_service_hidden(self):
        self.assertEqual(self.public_estimate(area_sqm=50).status_code, 400)
        self.assertEqual(self.public_estimate(promo_code="SECRET").status_code, 400)
        self.service.is_active = False
        self.service.save()
        self.assertEqual(self.public_estimate().status_code, 404)

    def test_seed_only_creates_manual_drafts_once(self):
        original = self.publish(self.tariff())
        call_command("seed_pricing_drafts", stdout=io.StringIO())
        count = TariffRevision.objects.count()
        call_command("seed_pricing_drafts", stdout=io.StringIO())
        self.assertEqual(TariffRevision.objects.count(), count)
        self.assertEqual(count, Service.objects.count())
        self.assertEqual(ServiceTariff.objects.get(service=self.service).current_id, original.pk)
        self.assertFalse(TariffRevision.objects.exclude(service=self.service).exclude(mode="manual").exists())


class PromoTests(PricingFixture):
    def test_admin_only_create_normalization_and_toggle(self):
        body = {
            "code": " hello10 ",
            "kind": "percent",
            "value": "10.00",
            "starts_at": timezone.now().isoformat(),
            "ends_at": (timezone.now() + timedelta(days=2)).isoformat(),
        }
        self.assertEqual(self.api.post(MANAGE + "promocodes/", body).status_code, 403)
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(self.api.get(MANAGE + "promocodes/").status_code, 403)
        self.api.force_authenticate(self.admin)
        result = self.api.post(MANAGE + "promocodes/", body)
        self.assertEqual(result.status_code, 201, result.json())
        self.assertEqual(result.json()["code"], "HELLO10")
        route = MANAGE + f"promocodes/{result.json()['id']}/"
        self.assertEqual(self.api.patch(route, {"expected_version": 1, "is_active": False}).status_code, 200)
        self.assertEqual(self.api.patch(route, {"expected_version": 1, "is_active": True}).status_code, 409)
        self.assertEqual(self.api.patch(route, {"expected_version": 2, "value": "90"}).status_code, 400)
        self.assertEqual(self.api.post(MANAGE + "promocodes/", body).status_code, 400)

    def test_promo_constraints_and_terms_immutable(self):
        from rest_framework.exceptions import ValidationError

        for extra in (
            {"value": Decimal("100")},
            {"value": Decimal("0")},
            {"ends_at": timezone.now() - timedelta(days=2)},
            {"maximum_discount": Decimal("0")},
        ):
            with self.assertRaises(ValidationError):
                self.promo(**extra)
        promo = self.promo()
        promo.value = Decimal("30.00")
        with self.assertRaises(ModelValidationError):
            promo.save()

    def test_selection_normalization_clear_and_owner_boundary(self):
        promo = self.promo()
        order = Order.objects.get(pk=self.draft()["id"])
        route = ROOT + f"orders/{order.pk}/pricing/promo/"
        result = self.api.post(route, {"expected_version": order.version, "code": "test10"})
        self.assertEqual(result.status_code, 200, result.json())
        self.assertEqual(result.json()["selected_promo_code"], promo.code)
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.post(route, {"expected_version": 2, "code": "TEST10"}).status_code, 404)
        self.api.force_authenticate(self.owner)
        self.assertEqual(
            self.api.post(route, {"expected_version": 2, "code": ""}).json()["selected_promo_code"], None
        )

    def test_selection_does_not_reserve_usage_and_order_estimate_discount(self):
        self.publish(self.tariff())
        promo = self.promo()
        order = Order.objects.get(pk=self.draft()["id"])
        services.select_promo(self.owner, order.pk, order.version, promo.code)
        data = self.api.get(ROOT + f"orders/{order.pk}/pricing/estimate/").json()
        self.assertEqual(
            (data["subtotal"], data["discount"], data["total"]), ("20000.00", "2000.00", "18000.00")
        )
        self.assertEqual(PromoRedemption.objects.count(), 0)

    def test_invalid_codes_consume_durable_quota(self):
        order = Order.objects.get(pk=self.draft()["id"])
        before = sum(AuthRateBucket.objects.values_list("count", flat=True))
        route = ROOT + f"orders/{order.pk}/pricing/promo/"
        for _ in range(30):
            self.assertEqual(
                self.api.post(route, {"expected_version": 1, "code": "UNKNOWN"}).status_code, 409
            )
        self.assertEqual(self.api.post(route, {"expected_version": 1, "code": "UNKNOWN"}).status_code, 429)
        self.assertEqual(sum(AuthRateBucket.objects.values_list("count", flat=True)) - before, 30)

    def test_disabled_future_expired_wrong_service_unavailable(self):
        promo = self.promo()
        order = Order.objects.get(pk=self.draft()["id"])
        variants = [
            {"is_active": False},
            {"starts_at": timezone.now() + timedelta(days=1)},
            {"ends_at": timezone.now() - timedelta(seconds=1)},
            {"service_id": Service.objects.exclude(pk=self.service.pk).first().pk},
        ]
        for variant in variants:
            # DB fixture simulates a promotion's state, not an allowed editing API.
            original = {k: getattr(promo, k) for k in variant}
            PromoCode.objects.filter(pk=promo.pk).update(**variant)
            with self.assertRaises(orders.Conflict):
                services.select_promo(self.owner, order.pk, order.version, promo.code)
            PromoCode.objects.filter(pk=promo.pk).update(**original)


class QuoteTests(PricingFixture):
    def test_quote_accept_explicit_freezes_total_and_is_not_payment(self):
        order = self.negotiable()
        quote = self.issued(order)
        self.assertEqual(self.accept(order, quote, confirmed=False).status_code, 400)
        data = self.accept(order, quote).json()
        self.assertEqual(data["order_status"], "price_agreed")
        self.assertEqual(data["quote"]["total"], "20000.00")
        self.assertEqual(data["quote"]["currency"], "KZT")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "test_confirmed")
        self.publish(self.tariff(base_amount=Decimal("40000.00")))
        quote.refresh_from_db()
        self.assertEqual(quote.total, Decimal("20000.00"))
        quote.total = Decimal("1.00")
        with self.assertRaises(ModelValidationError):
            quote.save()

    def test_offer_only_after_review_and_no_other_owner_access(self):
        draft = Order.objects.get(pk=self.draft()["id"])
        self.assertEqual(self.issue(draft).status_code, 404)
        self.api.force_authenticate(self.owner)
        order = self.negotiable()
        quote = self.issued(order)
        self.api.force_authenticate(self.other)
        base = ROOT + f"orders/{order.pk}/pricing/"
        for suffix in ("", "estimate/", f"{quote.pk}/"):
            self.assertEqual(self.api.get(base + suffix).status_code, 404)
        self.assertEqual(
            self.api.post(
                base + f"{quote.pk}/accept/", {"expected_version": order.version, "confirmed": True}
            ).status_code,
            404,
        )
        self.api.force_authenticate(self.owner)
        self.assertEqual(
            self.api.post(ROOT + f"review/orders/{order.pk}/pricing/", self.quote_data(order)).status_code,
            403,
        )

    def test_quote_create_idempotency_and_stale_version(self):
        order = self.negotiable()
        data = self.quote_data(order)
        self.api.force_authenticate(self.reviewer)
        route = ROOT + f"review/orders/{order.pk}/pricing/"
        first = self.api.post(route, data)
        again = self.api.post(route, data)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(first.json(), again.json())
        self.assertEqual(order.price_quotes.count(), 1)
        self.assertEqual(self.api.post(route, {**data, "terms": "Different"}).status_code, 409)
        self.assertEqual(
            self.api.post(route, {**data, "client_request_id": str(uuid.uuid4())}).status_code, 409
        )

    def test_decline_and_revision_preserve_history(self):
        order = self.negotiable()
        quote = self.issued(order)
        self.api.force_authenticate(self.owner)
        result = self.api.post(
            ROOT + f"orders/{order.pk}/pricing/{quote.pk}/decline/",
            {"expected_version": order.version, "comment": "Дорого"},
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["order_status"], "accepted")
        order.refresh_from_db()
        next_quote = self.issued(
            order, lines=[{"kind": "appraisal", "title": "Новая цена", "amount": "18000.00"}]
        )
        self.assertEqual(next_quote.revision, 2)
        self.assertEqual(self.accept(order, quote).status_code, 409)
        self.assertEqual(self.accept(order, next_quote).status_code, 200)
        quote.refresh_from_db()
        self.assertEqual(quote.decision_comment, "Дорого")

    def test_new_offer_supersedes_old_and_withdraw_blocks_acceptance(self):
        order = self.negotiable()
        old = self.issued(order)
        new = self.issued(order)
        old.refresh_from_db()
        self.assertEqual(old.state, "superseded")
        self.assertEqual(self.accept(order, old).status_code, 409)
        self.api.force_authenticate(self.reviewer)
        response = self.api.post(
            ROOT + f"review/orders/{order.pk}/pricing/{new.pk}/withdraw/",
            {"expected_version": order.version, "comment": "Уточнение"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.accept(order, new).status_code, 409)

    def test_expiry_effective_state_and_accept_rejected(self):
        order = self.negotiable()
        expires = timezone.now() + timedelta(minutes=2)
        quote = self.issued(order, expires_at=expires.isoformat())
        with patch("apps.pricing.services.timezone.now", return_value=expires + timedelta(seconds=1)):
            self.assertEqual(self.accept(order, quote).json()["error"]["code"], "quote_expired")
            self.assertEqual(
                self.api.get(ROOT + f"orders/{order.pk}/pricing/{quote.pk}/").json()["effective_state"],
                "expired",
            )

    def test_acceptance_retry_does_not_double_redeem(self):
        order = self.negotiable()
        promo = self.promo()
        order = services.select_promo(self.owner, order.pk, order.version, promo.code)
        quote = self.issued(order)
        stale_version = order.version
        self.assertEqual(self.accept(order, quote).status_code, 200)
        order.refresh_from_db()
        version = order.version
        self.assertEqual(self.accept(order, quote, expected_version=stale_version).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.version, version)
        self.assertEqual(PromoRedemption.objects.count(), 1)
        self.assertEqual(quote.total, Decimal("18000.00"))

    def test_discount_only_appraisal_not_travel_and_cap_rounding(self):
        order = self.negotiable()
        promo = self.promo(value=Decimal("12.50"), maximum_discount=Decimal("2000.00"))
        order = services.select_promo(self.owner, order.pk, order.version, promo.code)
        quote = self.issued(
            order,
            lines=[
                {"kind": "appraisal", "title": "Оценка", "amount": "20000.00"},
                {"kind": "travel", "title": "Перелёт и жильё", "amount": "100000.00"},
            ],
        )
        self.assertEqual(
            (quote.subtotal, quote.discount, quote.total),
            (Decimal("120000"), Decimal("2000"), Decimal("118000")),
        )
        self.assertEqual(self.accept(order, quote).status_code, 200)

    def test_fixed_discount_and_minimum_and_no_zero_price(self):
        order = self.negotiable()
        promo = self.promo(kind="fixed", value=Decimal("2000"), minimum_subtotal=Decimal("25000"))
        order = services.select_promo(self.owner, order.pk, order.version, promo.code)
        self.assertEqual(self.issue(order).status_code, 409)
        quote = self.issued(order, lines=[{"kind": "appraisal", "title": "Оценка", "amount": "30000.00"}])
        self.assertEqual(quote.total, Decimal("28000"))
        with self.assertRaises(orders.Conflict):
            services.promo_discount(promo, order, Decimal("1000"))

    def test_promotion_changed_after_offer_requires_new_offer(self):
        order = self.negotiable()
        promo = self.promo()
        order = services.select_promo(self.owner, order.pk, order.version, promo.code)
        quote = self.issued(order)
        promo = services.toggle_promo(self.admin, promo.pk, promo.version, False)
        self.assertEqual(self.accept(order, quote).status_code, 409)
        services.toggle_promo(self.admin, promo.pk, promo.version, True)
        self.assertEqual(self.accept(order, quote).status_code, 409)
        newer = self.issued(order)
        self.assertEqual(self.accept(order, newer).status_code, 200)

    def test_global_usage_limit_rechecked_at_acceptance(self):
        promo = self.promo(max_uses=1, per_user_limit=2)
        first = self.negotiable()
        first = services.select_promo(self.owner, first.pk, first.version, promo.code)
        q1 = self.issued(first)
        self.api.force_authenticate(self.owner)
        second = self.negotiable()
        second = services.select_promo(self.owner, second.pk, second.version, promo.code)
        q2 = self.issued(second)
        self.assertEqual(self.accept(first, q1).status_code, 200)
        self.assertEqual(self.accept(second, q2).status_code, 409)
        self.assertEqual(PromoRedemption.objects.filter(released_at__isnull=True).count(), 1)

    def test_per_user_limit_and_cancel_releases_usage_without_refund(self):
        promo = self.promo()
        first = self.negotiable()
        first = services.select_promo(self.owner, first.pk, first.version, promo.code)
        quote = self.issued(first)
        self.accept(first, quote)
        self.api.force_authenticate(self.owner)
        second = self.negotiable()
        with self.assertRaises(orders.Conflict):
            services.select_promo(self.owner, second.pk, second.version, promo.code)
        first.refresh_from_db()
        orders.cancel_order(self.owner, first.pk, first.version, "Передумал")
        quote.refresh_from_db()
        self.assertEqual(quote.state, "cancelled")
        self.assertIsNotNone(PromoRedemption.objects.get().released_at)
        self.assertIsNotNone(services.select_promo(self.owner, second.pk, second.version, promo.code))

    def test_company_rejection_closes_agreement_and_releases_promo(self):
        promo = self.promo()
        order = self.negotiable()
        order = services.select_promo(self.owner, order.pk, order.version, promo.code)
        quote = self.issued(order)
        self.accept(order, quote)
        order.refresh_from_db()
        result = orders.review_action(self.reviewer, order.pk, order.version, "reject", "Отказ компании")
        self.assertEqual(result.status, "rejected")
        quote.refresh_from_db()
        self.assertEqual(quote.state, "cancelled")
        self.assertIsNotNone(PromoRedemption.objects.get().released_at)

    def test_cancel_failure_rolls_back_release_and_order(self):
        order = self.negotiable()
        quote = self.issued(order)
        self.accept(order, quote)
        order.refresh_from_db()
        version = order.version
        with patch("apps.pricing.services.close_order_pricing", side_effect=RuntimeError("rollback")):
            with self.assertRaises(RuntimeError):
                orders.cancel_order(self.owner, order.pk, version)
        order.refresh_from_db()
        quote.refresh_from_db()
        self.assertEqual(order.status, "price_agreed")
        self.assertEqual(order.version, version)
        self.assertEqual(quote.state, "accepted")

    def test_selecting_new_promo_invalidates_offer(self):
        order = self.negotiable()
        quote = self.issued(order)
        promo = self.promo()
        order = services.select_promo(self.owner, order.pk, order.version, promo.code)
        quote.refresh_from_db()
        self.assertEqual(quote.state, "invalidated")
        self.assertEqual(self.accept(order, quote).status_code, 409)

    def test_cannot_reprice_agreed_or_modify_amount_from_customer(self):
        order = self.negotiable()
        quote = self.issued(order)
        self.assertEqual(self.accept(order, quote, total="1.00").status_code, 400)
        self.accept(order, quote)
        order.refresh_from_db()
        self.assertEqual(self.issue(order).status_code, 409)
        self.api.force_authenticate(self.owner)
        self.assertEqual(
            self.api.patch(
                ROOT + f"orders/{order.pk}/", {"expected_version": order.version, "answers": {}}
            ).status_code,
            409,
        )

    def test_bad_quote_lines_dates_and_totals_rejected(self):
        order = self.negotiable()
        for extra in (
            {"total": "1.00"},
            {"lines": []},
            {"expires_at": (timezone.now() - timedelta(seconds=1)).isoformat()},
            {"expires_at": (timezone.now() + timedelta(days=31)).isoformat()},
            {"lines": [{"kind": "travel", "title": "Travel", "amount": "20000"}]},
            {"lines": [{"kind": "appraisal", "title": "X", "amount": 20000}]},
            {"lines": [{"kind": "appraisal", "title": "X", "amount": "-1"}]},
            {
                "lines": [
                    {"kind": "appraisal", "title": "X", "amount": "999999999.99"},
                    {"kind": "other", "title": "Y", "amount": "1"},
                ]
            },
        ):
            self.assertEqual(self.issue(order, **extra).status_code, 400)
        self.assertEqual(PriceQuote.objects.count(), 0)

    def test_quote_beyond_promotion_expiry_rejected(self):
        promo = self.promo(ends_at=timezone.now() + timedelta(hours=1))
        order = self.negotiable()
        order = services.select_promo(self.owner, order.pk, order.version, promo.code)
        self.assertEqual(self.issue(order).status_code, 400)

    def test_new_legal_version_blocks_accept_but_not_decline(self):
        from apps.legal.models import LegalDocument

        order = self.negotiable()
        quote = self.issued(order)
        LegalDocument.objects.filter(kind="terms", language="ru", is_active=True).update(is_active=False)
        LegalDocument.objects.create(
            kind="terms", language="ru", version="pricing-next", title="Next", content="Next", is_active=True
        )
        self.assertEqual(self.accept(order, quote).status_code, 403)
        self.assertEqual(
            self.api.post(
                ROOT + f"orders/{order.pk}/pricing/{quote.pk}/decline/", {"expected_version": order.version}
            ).status_code,
            200,
        )

    def test_admin_quote_is_read_only(self):
        self.assertIn(PriceQuote, site._registry)
        order = self.negotiable()
        quote = self.issued(order)
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.post(f"/admin/pricing/pricequote/{quote.pk}/change/", {"total": "1.00"}).status_code,
            403,
        )
        self.assertEqual(self.client.get("/admin/pricing/pricequote/add/").status_code, 403)


class AdminPricingTests(PricingFixture):
    def test_admin_can_create_publish_and_clone_tariff(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            "/admin/pricing/tariffrevision/add/",
            {
                "service": str(self.service.pk),
                "mode": "flat",
                "base_amount": "23000.00",
                "included_area": "",
                "per_sqm": "",
                "urgent_surcharge": "",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        tariff = TariffRevision.objects.get()
        self.assertEqual(tariff.base_amount, Decimal("23000.00"))
        self.assertIsNone(tariff.published_at)
        for action in ("publish_selected", "clone_selected"):
            response = self.client.post(
                "/admin/pricing/tariffrevision/",
                {
                    "action": action,
                    "_selected_action": [str(tariff.pk)],
                    "index": "0",
                    "select_across": "0",
                },
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(TariffRevision.objects.count(), 2)
        tariff.refresh_from_db()
        self.assertIsNotNone(tariff.published_at)
        self.assertEqual(ServiceTariff.objects.get(service=self.service).current_id, tariff.pk)
        self.assertEqual(
            self.client.post(
                f"/admin/pricing/tariffrevision/{tariff.pk}/change/", {"base_amount": "1.00"}
            ).status_code,
            403,
        )

    def test_admin_can_create_and_disable_promo(self):
        self.client.force_login(self.admin)
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=3)
        response = self.client.post(
            "/admin/pricing/promocode/add/",
            {
                "code": "ADMIN10",
                "kind": "percent",
                "value": "10.00",
                "minimum_subtotal": "0.00",
                "maximum_discount": "",
                "service": "",
                "starts_at_0": start.strftime("%Y-%m-%d"),
                "starts_at_1": "00:00:00",
                "ends_at_0": end.strftime("%Y-%m-%d"),
                "ends_at_1": "23:00:00",
                "max_uses": "100",
                "per_user_limit": "1",
                "is_active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        promo = PromoCode.objects.get()
        response = self.client.post(
            "/admin/pricing/promocode/",
            {
                "action": "disable_codes",
                "_selected_action": [str(promo.pk)],
                "index": "0",
                "select_across": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        promo.refresh_from_db()
        self.assertFalse(promo.is_active)
        self.assertEqual(promo.version, 2)

    def test_demo_preserves_existing_tariffs_and_promos(self):
        self.publish(self.tariff())
        self.promo()
        before = (TariffRevision.objects.count(), PromoCode.objects.count(), PriceQuote.objects.count())
        output = io.StringIO()
        call_command("demo_orders", stdout=output)
        self.assertIn("price_agreed", output.getvalue())
        self.assertIn("PASS", output.getvalue())
        self.assertEqual(
            before, (TariffRevision.objects.count(), PromoCode.objects.count(), PriceQuote.objects.count())
        )

    def test_fractional_price_and_discount_preserve_tiyn(self):
        promo = self.promo(value=Decimal("12.50"))
        order = self.negotiable()
        order = services.select_promo(self.owner, order.pk, order.version, promo.code)
        quote = self.issued(order, lines=[{"kind": "appraisal", "title": "TEST", "amount": "20000.10"}])
        self.assertEqual(quote.discount, Decimal("2500.01"))
        self.assertEqual(quote.total, Decimal("17500.09"))
        self.assertEqual(self.accept(order, quote).status_code, 200)

    def test_submission_retains_estimate_when_current_tariff_changes(self):
        self.publish(self.tariff())
        order = self.draft()
        self.assertEqual(self.confirm(order).status_code, 200)
        url = ROOT + f"orders/{order['id']}/submissions/"
        before = self.api.get(url).json()["results"][0]["price_estimate_snapshot"]
        self.assertEqual(before["subtotal"], "20000.00")
        self.publish(self.tariff(base_amount=Decimal("30000.00")))
        self.assertEqual(self.api.get(url).json()["results"][0]["price_estimate_snapshot"], before)
        self.assertFalse(before["is_final"])
