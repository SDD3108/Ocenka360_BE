import io
import uuid
from copy import deepcopy

from django.contrib.admin.models import LogEntry
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.catalog.models import Category, FormRevision, Service
from apps.catalog.schema import evaluate, validate_definition
from apps.catalog.seed_data import definition_for
from apps.catalog.services import clone_form, publish_form
from apps.legal.models import LegalDocument
from apps.legal.services import accept_documents

BASE = "/api/v1/catalog/"


def sample_answers():
    return {
        "applicant_type": "individual",
        "applicant_role": "owner",
        "purpose": "sale",
        "city": "Астана",
        "address": "Тестовый адрес",
        "area_sqm": "65.5",
        "inspection_preference": "onsite",
        "urgency": "regular",
    }


class CatalogFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_catalog", stdout=io.StringIO())
        call_command("seed_legal_drafts", stdout=io.StringIO())
        cls.user = User.objects.create_user(email="catalog@example.com")
        cls.user.email_verified_at = timezone.now()
        cls.user.save()
        cls.user = accept_documents(
            cls.user,
            list(LegalDocument.objects.filter(language="ru", is_active=True).values_list("id", flat=True)),
            True,
        )

    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.service = Service.objects.get(slug="apartment")
        self.form = self.service.current_form

    def validate(self, answers=None, form_id=None, language="ru"):
        self.api.force_authenticate(self.user)
        return self.api.post(
            BASE + "services/apartment/validate/",
            {
                "form_id": str(form_id or self.form.pk),
                "language": language,
                "answers": sample_answers() if answers is None else answers,
            },
        )


class CatalogReadTests(CatalogFixture):
    def test_seed_covers_categories_and_services(self):
        self.assertEqual(Category.objects.count(), 10)
        self.assertEqual(Service.objects.count(), 18)
        self.assertEqual(FormRevision.objects.count(), 18)
        self.assertFalse(Service.objects.filter(current_form=None).exists())

    def test_seed_rerun_preserves_edits_and_disable(self):
        self.service.title["ru"] = "Отредактировано"
        self.service.is_active = False
        self.service.save()
        new_form = clone_form(self.form.pk)
        publish_form(new_form.pk)
        call_command("seed_catalog", stdout=io.StringIO())
        self.service.refresh_from_db()
        self.assertEqual(self.service.title["ru"], "Отредактировано")
        self.assertFalse(self.service.is_active)
        self.assertEqual(self.service.current_form_id, new_form.pk)
        self.assertEqual(FormRevision.objects.count(), 19)

    def test_public_category_counts_only_visible_services(self):
        self.service.is_active = False
        self.service.save()
        data = self.api.get(BASE + "categories/?language=en").json()
        estate = next(x for x in data if x["slug"] == "real-estate")
        self.assertEqual(estate["title"], "Real estate")
        self.assertEqual(estate["service_count"], 4)

    def test_service_list_is_paginated_and_localized(self):
        data = self.api.get(BASE + "services/?language=kk").json()
        self.assertEqual(data["count"], 18)
        self.assertEqual(data["results"][0]["title"], "Пәтерді бағалау")
        self.assertNotIn("definition", data["results"][0])

    def test_category_filter_and_title_search(self):
        data = self.api.get(BASE + "services/?language=en&category=real-estate&q=apartment").json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["slug"], "apartment")
        self.assertEqual(self.api.get(BASE + "services/?category=unknown").json()["count"], 0)

    def test_query_validation(self):
        for tail in ("language=kz", "page=0", "page=hello", "q=" + "x" * 101, "is_active=false"):
            with self.subTest(tail=tail):
                self.assertEqual(self.api.get(BASE + "services/?" + tail).status_code, 400)

    def test_form_has_stable_version_checksum_and_localized_labels(self):
        for lang in ("ru", "kk", "en"):
            data = self.api.get(BASE + f"services/apartment/form/?language={lang}").json()
            self.assertEqual(data["id"], str(self.form.pk))
            self.assertEqual(data["checksum"], self.form.checksum)
            self.assertEqual(data["fields"][0]["label"], self.form.definition["fields"][0]["label"][lang])
            self.assertNotIn("published_at", data)

    def test_disabled_service_hidden_at_every_route(self):
        self.service.is_active = False
        self.service.save()
        for path in ("services/apartment/", "services/apartment/form/"):
            self.assertEqual(self.api.get(BASE + path).status_code, 404)
        self.assertEqual(self.validate().status_code, 404)

    def test_disabled_category_hides_children(self):
        category = self.service.category
        category.is_active = False
        category.save()
        self.assertEqual(self.api.get(BASE + "services/apartment/").status_code, 404)
        self.assertEqual(self.api.get(BASE + "services/?category=real-estate").json()["count"], 0)
        self.assertNotIn("real-estate", [x["slug"] for x in self.api.get(BASE + "categories/").json()])

    def test_service_without_published_form_is_not_exposed(self):
        self.service.current_form = None
        self.service.save()
        self.assertEqual(self.api.get(BASE + "services/apartment/").status_code, 404)

    def test_public_catalog_ignores_stale_bearer(self):
        self.assertEqual(
            self.api.get(BASE + "services/", HTTP_AUTHORIZATION="Bearer expired").status_code, 200
        )

    def test_read_endpoints_cannot_modify_catalog(self):
        self.api.force_authenticate(self.user)
        for method, path in (
            ("post", "services/"),
            ("patch", "services/apartment/"),
            ("delete", "services/apartment/"),
        ):
            self.assertEqual(getattr(self.api, method)(BASE + path, {}).status_code, 405)

    def test_list_query_count_is_bounded(self):
        # Count + joined page; not one query for each title/category/form.
        with self.assertNumQueries(2):
            self.api.get(BASE + "services/")


class AnswerValidationTests(CatalogFixture):
    def test_validation_requires_authentication_and_current_consent(self):
        path = BASE + "services/apartment/validate/"
        self.assertEqual(self.api.post(path, {}).status_code, 401)
        user = User.objects.create_user("unaccepted@example.com")
        self.api.force_authenticate(user)
        self.assertEqual(self.api.post(path, {}).status_code, 403)

    def test_new_legal_version_requires_acceptance_again(self):
        doc = LegalDocument.objects.get(kind="terms", language="ru", is_active=True)
        doc.is_active = False
        doc.save()
        LegalDocument.objects.create(
            kind="terms", language="ru", version="dev-2", title="Next", content="Changed", is_active=True
        )
        self.assertEqual(self.validate().status_code, 403)

    def test_valid_apartment_returns_requirements_without_creating_order(self):
        data = self.validate().json()
        self.assertTrue(data["valid"])
        self.assertFalse(data["submission_created"])
        keys = {d["key"] for d in data["documents"]}
        self.assertTrue({"owner_id", "technical_passport", "ownership_basis", "encumbrance_f2"} <= keys)
        self.assertNotIn("company_registration", keys)
        self.assertNotIn("answers", data)
        self.assertNotIn("Тестовый адрес", str(data))

    def test_company_requires_name_and_changes_documents(self):
        answers = {**sample_answers(), "applicant_type": "company"}
        self.assertEqual(self.validate(answers).json()["errors"]["company_name"], "required")
        answers["company_name"] = "Test company"
        data = self.validate(answers, language="en").json()
        self.assertTrue(data["valid"])
        keys = {d["key"] for d in data["documents"]}
        self.assertIn("company_registration", keys)
        self.assertNotIn("owner_id", keys)

    def test_representative_requires_authority_document(self):
        data = self.validate({**sample_answers(), "applicant_role": "representative"}).json()
        self.assertIn("authority", {d["key"] for d in data["documents"]})

    def test_missing_fields_report_keys_not_personal_values(self):
        data = self.validate({}).json()
        self.assertFalse(data["valid"])
        self.assertEqual(data["errors"]["city"], "required")
        self.assertNotIn("company_name", data["visible_fields"])

    def test_other_purpose_reveals_required_detail(self):
        data = self.validate({**sample_answers(), "purpose": "other"}).json()
        self.assertEqual(data["errors"]["purpose_details"], "required")
        self.assertIn("purpose_details", data["visible_fields"])

    def test_hidden_and_unknown_answers_rejected(self):
        data = self.validate({**sample_answers(), "company_name": "hidden", "price": 1}).json()
        self.assertEqual(data["errors"], {"price": "unknown_field", "company_name": "field_not_applicable"})

    def test_bad_choice_does_not_unlock_condition(self):
        data = self.validate({**sample_answers(), "applicant_type": "administrator"}).json()
        self.assertEqual(data["errors"]["applicant_type"], "invalid_choice")
        self.assertNotIn("company_name", data["visible_fields"])

    def test_strict_decimal_integer_and_text_types(self):
        variants = [
            ("area_sqm", 65.5, "invalid_decimal"),
            ("area_sqm", "NaN", "invalid_decimal"),
            ("area_sqm", "-5", "out_of_range"),
            ("area_sqm", "0", "out_of_range"),
            ("rooms", True, "invalid_type"),
            ("rooms", 0, "out_of_range"),
            ("city", 42, "invalid_type"),
            ("city", "   ", "required"),
            ("city", "x" * 101, "too_long"),
        ]
        for key, value, error in variants:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.validate({**sample_answers(), key: value}).json()["errors"][key], error)

    def test_bad_outer_payload_and_nested_answers_rejected(self):
        self.api.force_authenticate(self.user)
        for value in ([], {"city": {}}, {"city": "a" * 4001}, {"not allowed": "x"}):
            response = self.api.post(
                BASE + "services/apartment/validate/", {"form_id": str(self.form.pk), "answers": value}
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.api.post(
                BASE + "services/apartment/validate/",
                {"form_id": str(self.form.pk), "answers": {}, "user_id": str(self.user.pk)},
            ).status_code,
            400,
        )

    def test_stale_or_other_service_form_returns_conflict(self):
        for form_id in (uuid.uuid4(), Service.objects.get(slug="vehicle").current_form_id):
            response = self.validate(form_id=form_id)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "stale_form")
        new = clone_form(self.form.pk)
        publish_form(new.pk)
        self.assertEqual(self.validate().status_code, 409)
        self.assertTrue(self.validate(form_id=new.pk).json()["valid"])

    def test_accident_template_requires_incident_information(self):
        definition = definition_for("accident")
        data = {
            "applicant_type": "individual",
            "applicant_role": "owner",
            "purpose": "insurance",
            "city": "Астана",
            "make_model": "Test",
            "inspection_preference": "onsite",
            "urgency": "regular",
            "incident_date": "2026-02-30",
        }
        result = evaluate(definition, data)
        self.assertEqual(result["errors"]["incident_date"], "invalid_date")
        self.assertEqual(result["errors"]["damage_description"], "required")
        data.update(incident_date="2026-02-28", damage_description="Test damage")
        self.assertTrue(evaluate(definition, data)["valid"])


class CatalogRevisionTests(CatalogFixture):
    def test_published_form_is_immutable(self):
        self.form.definition["fields"][0]["label"]["ru"] = "Changed"
        with self.assertRaises(ValidationError):
            self.form.save()
        self.form.refresh_from_db()
        self.assertNotEqual(self.form.definition["fields"][0]["label"]["ru"], "Changed")

    def test_clone_changes_only_new_revision_and_publish_switches_pointer(self):
        draft = clone_form(self.form.pk)
        self.assertEqual(draft.version, 2)
        self.assertIsNone(draft.published_at)
        draft.definition["fields"][0]["label"]["ru"] = "Новый вопрос"
        draft.save()
        self.service.refresh_from_db()
        self.assertEqual(self.service.current_form_id, self.form.pk)
        publish_form(draft.pk)
        self.service.refresh_from_db()
        self.assertEqual(self.service.current_form_id, draft.pk)
        self.form.refresh_from_db()
        self.assertNotEqual(draft.checksum, self.form.checksum)
        self.assertEqual(self.form.definition["fields"][0]["label"]["ru"], "Кто заказывает оценку?")

    def test_service_cannot_point_to_foreign_or_draft_form(self):
        for form in (Service.objects.get(slug="vehicle").current_form, clone_form(self.form.pk)):
            self.service.current_form = form
            with self.assertRaises(ValidationError):
                self.service.save()

    def test_service_slug_and_revision_identity_immutable(self):
        self.service.slug = "changed-slug"
        with self.assertRaises(ValidationError):
            self.service.save()
        draft = clone_form(self.form.pk)
        draft.version = 12
        with self.assertRaises(ValidationError):
            draft.save()

    def test_duplicate_keys_unknown_types_and_executable_conditions_rejected(self):
        mutations = [
            lambda d: d["fields"].append(deepcopy(d["fields"][0])),
            lambda d: d["fields"][0].update(type="python"),
            lambda d: d["fields"][1].update(visible_if={"expression": "eval(1)"}),
            lambda d: d["fields"][1].update(visible_if={"field": "future", "equals": True}),
            lambda d: d["fields"][0]["options"].append(deepcopy(d["fields"][0]["options"][0])),
            lambda d: d["fields"][0].update(label={"ru": "Only Russian"}),
            lambda d: d["documents"].append(deepcopy(d["documents"][0])),
        ]
        for mutation in mutations:
            definition = deepcopy(self.form.definition)
            mutation(definition)
            with self.assertRaises(ValidationError):
                validate_definition(definition)

    def test_boolean_condition_uses_false_as_valid_answer(self):
        definition = definition_for("custom")
        definition["fields"].extend(
            [
                {
                    "key": "has_question",
                    "type": "boolean",
                    "label": {"ru": "Вопрос", "kk": "Сұрақ", "en": "Question"},
                    "required": True,
                },
                {
                    "key": "question",
                    "type": "text",
                    "label": {"ru": "Текст", "kk": "Мәтін", "en": "Text"},
                    "required": True,
                    "visible_if": {"field": "has_question", "equals": True},
                },
            ]
        )
        validate_definition(definition)
        answers = {
            "applicant_type": "individual",
            "applicant_role": "owner",
            "purpose": "sale",
            "city": "Астана",
            "asset_description": "Test",
            "urgency": "regular",
            "has_question": False,
        }
        self.assertTrue(evaluate(definition, answers)["valid"])
        answers["has_question"] = "false"
        self.assertEqual(evaluate(definition, answers)["errors"]["has_question"], "invalid_type")

    def test_catalog_admin_denies_staff_and_clients(self):
        staff = User.objects.create_user("staff@example.com", role="admin", is_staff=True)
        for user in (staff, self.user):
            self.client.force_login(user)
            for path in ("/admin/catalog/service/", "/admin/catalog/formrevision/"):
                self.assertIn(self.client.get(path).status_code, (302, 403))

    def test_superuser_admin_can_clone_and_publish_with_audit(self):
        admin = User.objects.create_superuser("admin-catalog@example.com", "test-safe-password123")
        self.client.force_login(admin)
        endpoint = "/admin/catalog/formrevision/"
        self.assertEqual(self.client.get(endpoint).status_code, 200)
        self.client.post(endpoint, {"action": "clone_selected", "_selected_action": [str(self.form.pk)]})
        draft = self.service.forms.get(version=2)
        response = self.client.get(f"/admin/catalog/formrevision/{draft.pk}/change/")
        self.assertEqual(response.status_code, 200)
        self.client.post(endpoint, {"action": "publish_selected", "_selected_action": [str(draft.pk)]})
        self.service.refresh_from_db()
        self.assertEqual(self.service.current_form_id, draft.pk)
        self.assertEqual(LogEntry.objects.filter(user=admin).count(), 2)
        # Published detail is view-only; even crafted POST cannot change it.
        self.assertEqual(
            self.client.post(
                f"/admin/catalog/formrevision/{draft.pk}/change/", {"definition": "{}"}
            ).status_code,
            403,
        )
