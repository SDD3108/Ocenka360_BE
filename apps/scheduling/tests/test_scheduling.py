import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.admin.sites import site
from django.test import override_settings
from django.utils import timezone

from apps.materials import services as materials
from apps.materials.models import Material
from apps.materials.tests.fixtures import photo_upload, seed_test_materials
from apps.orders import services as orders
from apps.orders.tests.test_orders import ROOT
from apps.pricing.models import PriceQuote, PromoRedemption
from apps.pricing.tests.test_pricing import PricingFixture
from apps.scheduling import services
from apps.scheduling.models import Appointment, CalendarLock, Inspection, ScheduleEvent


class SchedulingFixture(PricingFixture):
    def agreed(self):
        self.api.force_authenticate(self.owner)
        order = self.negotiable()
        quote = self.issued(order)
        response = self.accept(order, quote)
        self.assertEqual(response.status_code, 200, response.json())
        order.refresh_from_db()
        return order

    def payload(self, order, **extra):
        start = timezone.now() + timedelta(days=1)
        return {
            "expected_version": order.version,
            "client_request_id": str(uuid.uuid4()),
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=2)).isoformat(),
            "mode": "onsite",
            "note": "Test appointment",
            **extra,
        }

    def request_slot(self, order, **extra):
        self.api.force_authenticate(self.owner)
        response = self.api.post(ROOT + f"orders/{order.pk}/schedule/", self.payload(order, **extra))
        self.assertEqual(response.status_code, 201, response.json())
        order.refresh_from_db()
        return Appointment.objects.get(pk=response.json()["appointment"]["id"])

    def confirm_slot(self, order, appointment, **extra):
        self.api.force_authenticate(self.reviewer)
        return self.api.post(
            ROOT + f"review/orders/{order.pk}/schedule/{appointment.pk}/confirm/",
            {
                "expected_version": order.version,
                "starts_at": appointment.starts_at.isoformat(),
                "ends_at": appointment.ends_at.isoformat(),
                "report_hours": 24,
                **extra,
            },
        )

    def scheduled(self, **extra):
        order = self.agreed()
        appointment = self.request_slot(order, **extra)
        response = self.confirm_slot(order, appointment)
        self.assertEqual(response.status_code, 200, response.json())
        order.refresh_from_db()
        appointment.refresh_from_db()
        return order, appointment

    def offer_slot(self, order, **extra):
        self.api.force_authenticate(self.reviewer)
        response = self.api.post(
            ROOT + f"review/orders/{order.pk}/schedule/", self.payload(order, report_hours=2, **extra)
        )
        self.assertEqual(response.status_code, 201, response.json())
        order.refresh_from_db()
        return Appointment.objects.get(pk=response.json()["appointment"]["id"])

    def accept_slot(self, order, appointment, **extra):
        self.api.force_authenticate(self.owner)
        return self.api.post(
            ROOT + f"orders/{order.pk}/schedule/{appointment.pk}/accept/",
            {"expected_version": order.version, "confirmed": True, **extra},
        )

    def start_slot(self, order, appointment, **extra):
        self.api.force_authenticate(self.reviewer)
        with patch(
            "apps.scheduling.services.timezone.now",
            return_value=appointment.starts_at + timedelta(minutes=10),
        ):
            return self.api.post(
                ROOT + f"review/orders/{order.pk}/schedule/{appointment.pk}/start/",
                {"expected_version": order.version, **extra},
            )


class SchedulingTests(SchedulingFixture):
    def test_seeded_mutex_and_initial_empty_calendar(self):
        self.assertEqual(list(CalendarLock.objects.values_list("id", flat=True)), [1])
        order = self.agreed()
        data = self.api.get(ROOT + f"orders/{order.pk}/schedule/").json()
        self.assertEqual(data["appointments"], [])
        self.assertIsNone(data["inspection"])

    def test_complete_onsite_flow_deadline_from_actual_start_and_history(self):
        order, appointment = self.scheduled()
        self.assertEqual(order.status, "inspection_scheduled")
        self.assertEqual(self.start_slot(order, appointment).status_code, 200)
        order.refresh_from_db()
        inspection = order.inspection
        self.assertEqual(inspection.report_due_at, inspection.started_at + timedelta(hours=24))
        due = inspection.report_due_at
        with patch(
            "apps.scheduling.services.timezone.now",
            return_value=inspection.started_at + timedelta(minutes=30),
        ):
            response = self.api.post(
                ROOT + f"review/orders/{order.pk}/schedule/{appointment.pk}/finish/",
                {"expected_version": order.version, "note": "Осмотр завершён"},
            )
        self.assertEqual(response.status_code, 200, response.json())
        order.refresh_from_db()
        inspection.refresh_from_db()
        self.assertEqual(order.status, "awaiting_report")
        self.assertEqual(inspection.report_due_at, due)
        self.assertEqual(inspection.material_links.count(), order.materials.count())
        self.assertEqual(order.schedule_events.count(), 4)
        self.assertEqual(order.price_quotes.get().state, "accepted")
        self.assertEqual(
            self.api.get(ROOT + f"review/orders/{order.pk}/schedule/history/").json()["count"], 4
        )
        with patch("apps.scheduling.serializers.timezone.now", return_value=due + timedelta(seconds=1)):
            self.assertTrue(
                self.api.get(ROOT + f"review/orders/{order.pk}/schedule/").json()["inspection"]["overdue"]
            )

    def test_cannot_schedule_before_accepted_quote(self):
        order = self.negotiable()
        self.api.force_authenticate(self.owner)
        self.assertEqual(
            self.api.post(ROOT + f"orders/{order.pk}/schedule/", self.payload(order)).status_code, 409
        )
        self.assertFalse(Appointment.objects.exists())

    def test_owner_other_owner_and_reviewer_routes(self):
        order, appointment = self.scheduled()
        self.api.force_authenticate(self.other)
        for suffix in ["schedule/", "schedule/history/"]:
            self.assertEqual(self.api.get(ROOT + f"orders/{order.pk}/" + suffix).status_code, 404)
        self.assertEqual(
            self.api.post(ROOT + f"orders/{order.pk}/schedule/", self.payload(order)).status_code, 404
        )
        self.api.force_authenticate(self.owner)
        for path in ["review/calendar/", f"review/orders/{order.pk}/schedule/"]:
            self.assertEqual(self.api.get(ROOT + path).status_code, 403)
        self.assertEqual(
            self.api.post(
                ROOT + f"review/orders/{order.pk}/schedule/{appointment.pk}/start/",
                {"expected_version": order.version},
            ).status_code,
            403,
        )
        self.api.force_authenticate(None)
        self.assertEqual(self.api.get(ROOT + f"orders/{order.pk}/schedule/").status_code, 401)

    def test_timezone_is_required_and_equivalent_offsets_round_trip(self):
        order = self.agreed()
        data = self.payload(order)
        path = ROOT + f"orders/{order.pk}/schedule/"
        data["starts_at"] = "2027-01-15T12:00:00"
        self.assertEqual(self.api.post(path, data).status_code, 400)
        start = timezone.now() + timedelta(days=1)
        data.update(starts_at=start.isoformat(), ends_at=(start + timedelta(hours=1)).isoformat())
        response = self.api.post(path, data)
        self.assertEqual(response.status_code, 201, response.json())
        appointment = Appointment.objects.get(pk=response.json()["appointment"]["id"])
        self.assertEqual(appointment.starts_at, start)
        self.assertTrue(response.json()["appointment"]["starts_at"].endswith("+05:00"))

    def test_invalid_interval_bounds_unknown_fields_and_price_injection(self):
        order = self.agreed()
        path = ROOT + f"orders/{order.pk}/schedule/"
        now = timezone.now()
        for extra in [
            {"starts_at": (now - timedelta(days=1)).isoformat()},
            {"ends_at": (now + timedelta(days=3)).isoformat()},
            {
                "starts_at": (now + timedelta(days=181)).isoformat(),
                "ends_at": (now + timedelta(days=181, hours=1)).isoformat(),
            },
            {"report_hours": 1},
            {"state": "confirmed"},
            {"total": "1.00"},
            {"mode": "invalid"},
        ]:
            self.assertEqual(self.api.post(path, self.payload(order, **extra)).status_code, 400)
        self.assertFalse(Appointment.objects.exists())

    def test_client_proposal_retry_and_conflicting_request_id(self):
        order = self.agreed()
        data = self.payload(order)
        path = ROOT + f"orders/{order.pk}/schedule/"
        self.assertEqual(self.api.post(path, data).status_code, 201)
        self.assertEqual(self.api.post(path, data).status_code, 200)
        self.assertEqual(self.api.post(path, {**data, "note": "different"}).status_code, 409)
        self.assertEqual(order.appointments.count(), 1)
        self.assertEqual(order.schedule_events.count(), 1)

    def test_stale_confirmation_and_outside_client_window_rejected(self):
        order = self.agreed()
        appointment = self.request_slot(order)
        self.assertEqual(
            self.confirm_slot(order, appointment, expected_version=order.version - 1).status_code, 409
        )
        self.assertEqual(
            self.confirm_slot(
                order, appointment, starts_at=(appointment.starts_at - timedelta(minutes=1)).isoformat()
            ).status_code,
            400,
        )
        self.assertEqual(self.confirm_slot(order, appointment, report_hours=49).status_code, 400)
        self.assertEqual(self.confirm_slot(order, appointment, buffer_after_minutes=-1).status_code, 400)
        self.assertFalse(order.appointments.filter(state="confirmed").exists())

    def test_reschedule_preserves_old_booking_until_explicit_accept(self):
        order, old = self.scheduled()
        new = self.offer_slot(
            order,
            starts_at=(old.starts_at + timedelta(days=1)).isoformat(),
            ends_at=(old.ends_at + timedelta(days=1)).isoformat(),
        )
        old.refresh_from_db()
        self.assertEqual(old.state, "confirmed")
        self.assertEqual(self.accept_slot(order, new, confirmed=False).status_code, 400)
        self.assertEqual(self.accept_slot(order, new).status_code, 200)
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.state, "superseded")
        self.assertEqual(new.state, "confirmed")
        self.assertEqual(self.accept_slot(order, new).status_code, 409)

    def test_cancel_pending_keeps_old_booking_and_price(self):
        order, old = self.scheduled()
        pending = self.offer_slot(order)
        self.api.force_authenticate(self.owner)
        response = self.api.post(
            ROOT + f"orders/{order.pk}/schedule/{pending.pk}/cancel/",
            {"expected_version": order.version, "reason": "Не подходит"},
        )
        self.assertEqual(response.status_code, 200)
        old.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(old.state, "confirmed")
        self.assertEqual(order.status, "inspection_scheduled")
        self.assertEqual(order.price_quotes.get().state, "accepted")

    def test_cancel_active_clears_pending_but_keeps_agreed_price(self):
        order, old = self.scheduled()
        pending = self.offer_slot(order)
        appointment, order = services.cancel_appointment(
            self.owner, order.pk, old.pk, order.version, "Перенесу позже"
        )
        pending.refresh_from_db()
        self.assertEqual(appointment.state, "cancelled")
        self.assertEqual(pending.state, "cancelled")
        self.assertEqual(order.status, "price_agreed")
        self.assertEqual(order.price_quotes.get().state, "accepted")

    def test_order_cancellation_and_rejection_close_calendar(self):
        for action in ["cancel", "reject"]:
            order, appointment = self.scheduled()
            if action == "cancel":
                orders.cancel_order(self.owner, order.pk, order.version, "Передумал")
            else:
                orders.review_action(
                    self.reviewer, order.pk, order.version, "reject", "Компания не может выполнить"
                )
            appointment.refresh_from_db()
            self.assertEqual(appointment.state, "cancelled")
            self.assertEqual(order.price_quotes.get().state, "cancelled")

    def test_failed_order_cancel_rolls_back_schedule_and_price(self):
        order, appointment = self.scheduled()
        with patch("apps.pricing.services.close_order_pricing", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                orders.cancel_order(self.owner, order.pk, order.version)
        order.refresh_from_db()
        appointment.refresh_from_db()
        self.assertEqual(order.status, "inspection_scheduled")
        self.assertEqual(appointment.state, "confirmed")

    def test_overlap_buffers_and_adjacent_intervals(self):
        order = self.agreed()
        first = self.request_slot(order)
        self.assertEqual(self.confirm_slot(order, first, buffer_after_minutes=30).status_code, 200)
        second_order = self.agreed()
        second = self.request_slot(
            second_order,
            starts_at=first.ends_at.isoformat(),
            ends_at=(first.ends_at + timedelta(hours=1)).isoformat(),
        )
        self.assertEqual(self.confirm_slot(second_order, second).status_code, 409)
        second_order.refresh_from_db()
        second = self.request_slot(
            second_order,
            starts_at=(first.ends_at + timedelta(minutes=30)).isoformat(),
            ends_at=(first.ends_at + timedelta(hours=1)).isoformat(),
        )
        self.assertEqual(self.confirm_slot(second_order, second).status_code, 200)

    def test_new_block_can_invalidate_pending_offer_not_old_booking(self):
        order, old = self.scheduled()
        new = self.offer_slot(
            order,
            starts_at=(old.starts_at + timedelta(days=2)).isoformat(),
            ends_at=(old.ends_at + timedelta(days=2)).isoformat(),
        )
        block, _ = services.create_block(
            self.reviewer,
            {
                "client_request_id": uuid.uuid4(),
                "starts_at": new.starts_at,
                "ends_at": new.ends_at,
                "reason": "Недоступна",
            },
        )
        self.assertEqual(self.accept_slot(order, new).status_code, 409)
        old.refresh_from_db()
        self.assertEqual(old.state, "confirmed")
        services.cancel_block(self.reviewer, block.pk, "Снова доступна")
        self.assertEqual(self.accept_slot(order, new).status_code, 200)

    def test_block_conflict_permissions_and_idempotent_cancel(self):
        order, appointment = self.scheduled()
        data = {
            "client_request_id": str(uuid.uuid4()),
            "starts_at": appointment.starts_at.isoformat(),
            "ends_at": appointment.ends_at.isoformat(),
            "reason": "TEST",
        }
        path = ROOT + "review/calendar/blocks/"
        self.assertEqual(self.api.post(path, data).status_code, 409)
        data.update(
            starts_at=(appointment.starts_at + timedelta(days=2)).isoformat(),
            ends_at=(appointment.ends_at + timedelta(days=2)).isoformat(),
        )
        response = self.api.post(path, data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.api.post(path, data).status_code, 200)
        self.assertEqual(self.api.post(path, {**data, "reason": "Another"}).status_code, 409)
        cancel = path + response.json()["id"] + "/cancel/"
        self.assertEqual(self.api.post(cancel, {"reason": "TEST"}).status_code, 200)
        self.assertEqual(self.api.post(cancel, {"reason": "TEST"}).status_code, 200)
        self.assertEqual(ScheduleEvent.objects.filter(action="calendar_block_cancelled").count(), 1)
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.post(path, data).status_code, 403)

    def test_calendar_range_and_no_public_access(self):
        order, appointment = self.scheduled()
        path = ROOT + "review/calendar/"
        self.assertEqual(self.api.get(path).status_code, 400)
        data = {"starts_at": appointment.starts_at.isoformat(), "ends_at": appointment.ends_at.isoformat()}
        response = self.api.get(path, data)
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(len(response.json()["appointments"]), 1)
        self.assertNotIn("owner", response.json()["appointments"][0])
        self.assertEqual(
            self.api.get(
                path, {**data, "ends_at": (appointment.starts_at + timedelta(days=32)).isoformat()}
            ).status_code,
            400,
        )
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.get(path, data).status_code, 403)

    def test_no_early_start_and_no_cancel_after_work_starts(self):
        order, appointment = self.scheduled()
        path = ROOT + f"review/orders/{order.pk}/schedule/{appointment.pk}/start/"
        self.assertEqual(self.api.post(path, {"expected_version": order.version}).status_code, 409)
        self.assertEqual(self.start_slot(order, appointment).status_code, 200)
        order.refresh_from_db()
        with self.assertRaises(orders.Conflict):
            orders.cancel_order(self.owner, order.pk, order.version)
        with self.assertRaises(orders.Conflict):
            services.cancel_appointment(
                self.reviewer, order.pk, appointment.pk, order.version, "TEST", review=True
            )
        with self.assertRaises(orders.Conflict):
            orders.review_action(self.reviewer, order.pk, order.version, "reject", "TEST")

    def test_start_repeat_and_finish_before_start_do_not_mutate(self):
        order, appointment = self.scheduled()
        path = ROOT + f"review/orders/{order.pk}/schedule/{appointment.pk}/finish/"
        self.assertEqual(
            self.api.post(path, {"expected_version": order.version, "note": "TEST"}).status_code, 409
        )
        self.assertEqual(self.start_slot(order, appointment).status_code, 200)
        self.assertEqual(self.start_slot(order, appointment).status_code, 409)
        self.assertEqual(Inspection.objects.count(), 1)

    def test_no_two_inspections_in_progress_even_after_booked_end(self):
        order, first = self.scheduled()
        second_order = self.agreed()
        second = self.request_slot(
            second_order,
            starts_at=first.ends_at.isoformat(),
            ends_at=(first.ends_at + timedelta(hours=1)).isoformat(),
        )
        self.assertEqual(self.confirm_slot(second_order, second).status_code, 200)
        second_order.refresh_from_db()
        self.assertEqual(self.start_slot(order, first).status_code, 200)
        self.assertEqual(self.start_slot(second_order, second).status_code, 409)

    def test_remote_requires_appraiser_acceptance_and_all_evidence(self):
        order, appointment = self.scheduled(mode="remote")
        self.assertEqual(self.start_slot(order, appointment).status_code, 400)
        self.assertEqual(self.start_slot(order, appointment, materials_adequate=True).status_code, 400)
        self.assertFalse(Inspection.objects.exists())
        # Synthetic test files only. The real multipart route is tested separately below.
        original = order.answers
        order.answers = {**original, "inspection_preference": "remote"}
        seed_test_materials(order)
        order.answers = original
        order.materials.update(review_status="accepted")
        self.assertEqual(self.start_slot(order, appointment, materials_adequate=True).status_code, 200)
        inspection = Inspection.objects.get(order=order)
        self.assertTrue(inspection.remote_materials_adequate)
        self.assertGreater(inspection.material_links.count(), 4)
        self.assertTrue(inspection.materials_snapshot)

    def test_remote_client_upload_is_allowed_only_for_confirmed_remote(self):
        order, appointment = self.scheduled(mode="remote")
        self.api.force_authenticate(self.owner)
        guide = materials.checklist(order)
        slot = next(s["key"] for s in guide["photos"] if s["required"])
        self.assertFalse(guide["complete"])
        response = self.api.post(
            ROOT + f"orders/{order.pk}/materials/",
            {
                "expected_version": order.version,
                "client_upload_id": str(uuid.uuid4()),
                "kind": "photo",
                "slot_key": slot,
                "file": photo_upload(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        order.refresh_from_db()
        services.cancel_appointment(self.owner, order.pk, appointment.pk, order.version, "TEST")
        order.refresh_from_db()
        with self.assertRaises(orders.Conflict):
            materials.editable(order, False)

    def test_reviewer_can_upload_onsite_photo_and_evidence_is_pinned_at_finish(self):
        order, appointment = self.scheduled()
        self.assertEqual(self.start_slot(order, appointment).status_code, 200)
        order.refresh_from_db()
        slot = materials.checklist(order)["photos"][0]["key"]
        response = self.api.post(
            ROOT + f"review/orders/{order.pk}/materials/",
            {
                "expected_version": order.version,
                "client_upload_id": str(uuid.uuid4()),
                "kind": "photo",
                "slot_key": slot,
                "file": photo_upload(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.json())
        order.refresh_from_db()
        with patch("apps.scheduling.services.timezone.now", return_value=appointment.ends_at):
            services.finish_inspection(self.reviewer, order.pk, appointment.pk, order.version, "TEST")
        self.assertTrue(Material.objects.get(pk=response.json()["material"]["id"]).inspection_links.exists())

    def test_admin_is_readonly_and_old_versions_do_not_gain_write_access(self):
        self.assertFalse(
            site._registry[Appointment].has_add_permission(type("R", (), {"user": self.admin})())
        )
        order, appointment = self.scheduled()
        with override_settings(ALLOW_ORDER_SIMULATION=False):
            self.assertEqual(self.api.get(ROOT + f"review/orders/{order.pk}/schedule/").status_code, 404)
            self.api.force_authenticate(self.owner)
            self.assertEqual(
                self.api.post(ROOT + f"orders/{order.pk}/schedule/", self.payload(order)).status_code, 403
            )

    def test_cancellation_keeps_working_when_legal_consent_changes(self):
        order, appointment = self.scheduled()
        with patch("apps.orders.services.requires_consent", return_value=True):
            _, order = services.cancel_appointment(
                self.owner, order.pk, appointment.pk, order.version, "TEST"
            )
        self.assertEqual(order.status, "price_agreed")

    def test_quote_and_promo_survive_reschedule_but_order_cancel_releases_them(self):
        order = self.negotiable()
        promo = self.promo()
        from apps.pricing import services as pricing

        order = pricing.select_promo(self.owner, order.pk, order.version, promo.code)
        quote = self.issued(order)
        self.assertEqual(self.accept(order, quote).status_code, 200)
        order.refresh_from_db()
        appt = self.request_slot(order)
        self.assertEqual(self.confirm_slot(order, appt).status_code, 200)
        order.refresh_from_db()
        self.assertTrue(PromoRedemption.objects.filter(released_at__isnull=True).exists())
        _, order = services.cancel_appointment(self.owner, order.pk, appt.pk, order.version, "TEST")
        self.assertTrue(PromoRedemption.objects.filter(released_at__isnull=True).exists())
        orders.cancel_order(self.owner, order.pk, order.version)
        self.assertFalse(PromoRedemption.objects.filter(released_at__isnull=True).exists())
        self.assertEqual(PriceQuote.objects.get(pk=quote.pk).state, "cancelled")

    def test_demo_inspection_rolls_back_calendar_and_material_links(self):
        import io

        from django.core.management import call_command

        before = (Appointment.objects.count(), Inspection.objects.count(), ScheduleEvent.objects.count())
        output = io.StringIO()
        call_command("demo_orders", inspection=True, stdout=output)
        self.assertIn("awaiting_report", output.getvalue())
        self.assertIn("report due", output.getvalue())
        self.assertIn("PASS", output.getvalue())
        self.assertEqual(
            before, (Appointment.objects.count(), Inspection.objects.count(), ScheduleEvent.objects.count())
        )

    def test_two_pending_offers_do_not_reserve_but_acceptance_checks_availability(self):
        first_order = self.agreed()
        first = self.offer_slot(first_order)
        second_order = self.agreed()
        second = self.offer_slot(
            second_order, starts_at=first.starts_at.isoformat(), ends_at=first.ends_at.isoformat()
        )
        self.assertEqual(self.accept_slot(first_order, first).status_code, 200)
        response = self.accept_slot(second_order, second)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(str(first_order.pk), str(response.json()))
        self.assertNotIn(self.owner.email, str(response.json()))

    def test_appointment_id_cannot_be_used_under_another_order(self):
        first_order, appointment = self.scheduled()
        second_order = self.agreed()
        self.api.force_authenticate(self.owner)
        response = self.api.post(
            ROOT + f"orders/{second_order.pk}/schedule/{appointment.pk}/cancel/",
            {"expected_version": second_order.version, "reason": "TEST"},
        )
        self.assertEqual(response.status_code, 404)
        appointment.refresh_from_db()
        self.assertEqual(appointment.state, "confirmed")

    def test_remote_rejected_or_missing_file_blocks_start(self):
        order, appointment = self.scheduled(mode="remote")
        original = order.answers
        order.answers = {**original, "inspection_preference": "remote"}
        seed_test_materials(order)
        order.answers = original
        order.materials.update(review_status="accepted")
        photo = order.materials.filter(kind="photo").first()
        photo.review_status = "rejected"
        photo.save()
        self.assertEqual(self.start_slot(order, appointment, materials_adequate=True).status_code, 400)
        photo.review_status = "accepted"
        photo.save()
        from apps.materials.storage import PrivateStorage

        PrivateStorage().delete(photo.storage_key)
        self.assertEqual(self.start_slot(order, appointment, materials_adequate=True).status_code, 400)
        self.assertFalse(Inspection.objects.exists())

    def test_post_work_evidence_cannot_be_withdrawn_or_uploaded(self):
        order, appointment = self.scheduled()
        self.assertEqual(self.start_slot(order, appointment).status_code, 200)
        order.refresh_from_db()
        with patch("apps.scheduling.services.timezone.now", return_value=appointment.ends_at):
            _, order = services.finish_inspection(
                self.reviewer, order.pk, appointment.pk, order.version, "TEST"
            )
        with self.assertRaises(orders.Conflict):
            materials.editable(order, True)
        with self.assertRaises(orders.Conflict):
            materials.editable(order, False)
        self.assertEqual(order.inspection.material_links.count(), order.materials.count())
