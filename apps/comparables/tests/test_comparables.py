import copy
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.comparables.models import Estimate, EstimateEvent
from apps.orders.tests.test_orders import ROOT, OrderFixture


class ComparableTests(OrderFixture):
    def setUp(self):
        super().setUp()
        self.order = self.submitted()
        self.api.force_authenticate(self.reviewer)
        self.url = ROOT + f"review/orders/{self.order.pk}/market-estimates/"

    def data(self):
        today = timezone.localdate().isoformat()
        return {
            "expected_version": self.order.version,
            "client_request_id": str(uuid.uuid4()),
            "as_of": today,
            "comparability_confirmed": True,
            "comparables": [
                {
                    "source_url": f"https://listings.example.com/offer/{i}",
                    "source_label": f"SYNTHETIC {i}",
                    "observed_on": today,
                    "city": "Астана",
                    "area_sqm": "50.00",
                    "asking_price_kzt": str(price),
                }
                for i, price in enumerate([20000000, 25000000, 30000000])
            ],
        }

    def create_estimate(self):
        response = self.api.post(self.url, self.data())
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def test_decimal_median_and_immutable_snapshot(self):
        data = self.create_estimate()
        result = data["result"]
        self.assertEqual(Decimal(result["median_per_sqm"]), Decimal("500000.00"))
        self.assertEqual(Decimal(result["indication"]), Decimal("32750000.00"))
        self.assertEqual(Decimal(result["sample_min_indication"]), Decimal("26200000.00"))
        self.assertEqual(Decimal(result["sample_max_indication"]), Decimal("39300000.00"))
        self.assertFalse(result["automatic_market_data"])
        self.assertFalse(result["adjustments_applied"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "submitted")
        self.assertEqual(self.order.version, data["order_version"])

    def test_private_draft_share_and_withdraw_confirmed(self):
        obj = self.create_estimate()
        owner_url = ROOT + f"orders/{self.order.pk}/market-estimates/"
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.get(owner_url).json()["count"], 0)
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(owner_url).status_code, 404)
        self.api.force_authenticate(self.reviewer)
        path = self.url + obj["id"] + "/share/"
        self.assertEqual(self.api.post(path, {"expected_version": 1, "confirmed": False}).status_code, 400)
        shared = self.api.post(path, {"expected_version": 1, "confirmed": True})
        self.assertEqual(shared.status_code, 200)
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.get(owner_url).json()["count"], 1)
        self.assertEqual(self.api.post(path, {"expected_version": 2, "confirmed": True}).status_code, 403)
        self.api.force_authenticate(self.reviewer)
        path = self.url + obj["id"] + "/withdraw/"
        self.assertEqual(
            self.api.post(
                path, {"expected_version": 1, "confirmed": True, "reason": "bad source"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.api.post(
                path, {"expected_version": 2, "confirmed": True, "reason": "bad source"}
            ).status_code,
            200,
        )
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.get(owner_url).json()["count"], 0)
        self.api.force_authenticate(self.reviewer)
        self.assertEqual(
            self.api.post(
                path, {"expected_version": 2, "confirmed": True, "reason": "bad source"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.api.post(path, {"expected_version": 2, "confirmed": True, "reason": "changed"}).status_code,
            409,
        )
        self.assertEqual(EstimateEvent.objects.count(), 3)

    def test_idempotency_and_different_payload_conflict(self):
        data = self.data()
        first = self.api.post(self.url, data)
        second = self.api.post(self.url, data)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.api.post(self.url, {**data, "note": "changed"}).status_code, 409)
        self.assertEqual(Estimate.objects.count(), 1)

    def test_invalid_source_dates_prices_duplicates_and_outliers(self):
        for change in [
            {"source_url": "http://example.com"},
            {"source_url": "https://127.0.0.1/test"},
            {"source_url": "https://name:pw@example.com"},
            {"city": "Алматы"},
            {"area_sqm": "10.00"},
            {"asking_price_kzt": "-1"},
            {"asking_price_kzt": "NaN"},
            {"observed_on": (timezone.localdate() - timedelta(days=91)).isoformat()},
            {"observed_on": (timezone.localdate() + timedelta(days=1)).isoformat()},
        ]:
            data = self.data()
            data["comparables"][0].update(change)
            self.assertEqual(self.api.post(self.url, data).status_code, 400, change)
        data = self.data()
        data["comparables"][1] = copy.deepcopy(data["comparables"][0])
        self.assertEqual(self.api.post(self.url, data).status_code, 400)
        self.assertEqual(Estimate.objects.count(), 0)

    def test_minimum_sources_stale_order_and_confirmation(self):
        data = self.data()
        self.assertEqual(
            self.api.post(self.url, {**data, "comparables": data["comparables"][:2]}).status_code, 400
        )
        self.assertEqual(self.api.post(self.url, {**data, "comparability_confirmed": False}).status_code, 400)
        self.assertEqual(self.api.post(self.url, {**data, "expected_version": 999}).status_code, 409)

    def test_unsupported_service_has_no_fabricated_result(self):
        self.order.service_snapshot = {**self.order.service_snapshot, "slug": "accident-damage"}
        self.order.save()
        self.assertEqual(self.api.post(self.url, self.data()).status_code, 400)
        self.assertEqual(Estimate.objects.count(), 0)

    def test_subject_changed_prevents_sharing(self):
        obj = self.create_estimate()
        self.order.answers = {**self.order.answers, "area_sqm": "80"}
        self.order.save()
        self.assertEqual(
            self.api.post(
                self.url + obj["id"] + "/share/", {"expected_version": 1, "confirmed": True}
            ).status_code,
            409,
        )
