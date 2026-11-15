import uuid

from django.contrib.auth import authenticate
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient, APIRequestFactory

from apps.accounts.models import User
from apps.accounts.permissions import IsAdminRole, IsAppraiserOrAdmin


class UserModelTests(TestCase):
    def test_email_normalization_and_password_hashing(self):
        user = User.objects.create_user("  Client@EXAMPLE.COM  ", "Strong-Test-Password!42")
        self.assertEqual(user.email, "client@example.com")
        self.assertIsInstance(user.pk, uuid.UUID)
        self.assertEqual(user.role, User.Role.CLIENT)
        self.assertTrue(user.check_password("Strong-Test-Password!42"))
        self.assertNotEqual(user.password, "Strong-Test-Password!42")
        self.assertFalse(user.is_staff)
        self.assertIsNone(user.email_verified_at)

    def test_user_without_password_cannot_password_login(self):
        user = User.objects.create_user("otp@example.com")
        self.assertFalse(user.has_usable_password())

    def test_invalid_and_blank_email_rejected(self):
        for value in (None, "", "   "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                User.objects.create_user(value)
        with self.assertRaises(ValidationError):
            User.objects.create_user("not-an-email")

    def test_database_rejects_case_variant_duplicate_even_when_bypassing_save(self):
        User.objects.create_user("client@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(email="CLIENT@example.com")])

    def test_superuser_invariants(self):
        user = User.objects.create_superuser("owner@example.com", "Strong-Test-Password!42")
        self.assertEqual(user.role, User.Role.ADMIN)
        self.assertTrue(user.is_staff and user.is_superuser)
        for fields in (
            {"is_staff": False},
            {"is_superuser": False},
            {"role": "appraiser"},
            {"is_active": False},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                User.objects.create_superuser("bad@example.com", "Strong-Test-Password!42", **fields)
        with self.assertRaises(ValueError):
            User.objects.create_superuser("bad@example.com")

    def test_database_role_and_staff_constraints(self):
        user = User.objects.create_user("client@example.com")
        for fields in (
            {"role": "root"},
            {"is_staff": True},
            {"is_superuser": True},
            {"preferred_language": "xx"},
        ):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                User.objects.filter(pk=user.pk).update(**fields)

    def test_invalid_phone_rejected(self):
        with self.assertRaises(ValidationError):
            User.objects.create_user("client@example.com", phone_number="123")

    def test_case_insensitive_login_and_inactive_rejection(self):
        user = User.objects.create_superuser("owner@example.com", "Strong-Test-Password!42")
        self.assertEqual(
            authenticate(username=" OWNER@EXAMPLE.COM ", password="Strong-Test-Password!42"), user
        )
        self.assertIsNone(authenticate(username=user.email, password="wrong"))
        self.assertIsNone(authenticate(username="missing@example.com", password="wrong"))
        user.is_active = False
        user.save()
        self.assertIsNone(authenticate(username=user.email, password="Strong-Test-Password!42"))


class AccountAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.user = User.objects.create_user("client@example.com")
        self.owner = User.objects.create_superuser("owner@example.com", "Strong-Test-Password!42")

    def test_me_denies_anonymous_without_leaking_profiles(self):
        response = self.api.get(reverse("accounts:me"))
        self.assertEqual(response.status_code, 403)  # SessionAuthentication, no WWW-Authenticate.
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")
        self.assertNotContains(response, "client@example.com", status_code=403)

    def test_me_returns_only_current_user_safe_fields(self):
        self.api.force_login(self.user)
        response = self.api.get(reverse("accounts:me"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], str(self.user.id))
        self.assertEqual(response.json()["email"], self.user.email)
        for field in ("password", "is_superuser", "is_staff", "groups", "user_permissions"):
            self.assertNotIn(field, response.json())
        self.assertNotContains(response, self.owner.email)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_me_cannot_change_role_or_profile(self):
        self.api.force_login(self.user)
        response = self.api.patch(reverse("accounts:me"), {"role": "admin", "is_superuser": True})
        self.assertEqual(response.status_code, 405)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, "client")
        self.assertFalse(self.user.is_superuser)

    def test_user_list_and_other_users_not_publicly_exposed(self):
        self.api.force_login(self.user)
        for path in ("/api/v1/accounts/", f"/api/v1/accounts/{self.owner.pk}/"):
            self.assertEqual(self.api.get(path).status_code, 404)

    def test_inactive_existing_session_loses_access(self):
        self.api.force_login(self.user)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.api.get(reverse("accounts:me")).status_code, 403)

    def test_role_permissions_reject_inactive_users(self):
        request = APIRequestFactory().get("/")
        for role, admin_access, appraiser_access in (
            ("client", False, False),
            ("appraiser", False, True),
            ("admin", True, True),
        ):
            with self.subTest(role=role):
                self.user.role = role
                request.user = self.user
                self.assertEqual(IsAdminRole().has_permission(request, None), admin_access)
                self.assertEqual(IsAppraiserOrAdmin().has_permission(request, None), appraiser_access)
                self.user.is_active = False
                self.assertFalse(IsAdminRole().has_permission(request, None))
                self.assertFalse(IsAppraiserOrAdmin().has_permission(request, None))
                self.user.is_active = True

    def test_client_and_appraiser_cannot_open_admin(self):
        for role in ("client", "appraiser"):
            self.user.role = role
            self.user.save()
            self.client.force_login(self.user)
            response = self.client.get(reverse("admin:accounts_user_changelist"))
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login/", response.url)

    def test_non_superuser_staff_cannot_manage_users_even_with_permissions(self):
        staff = User.objects.create_user("staff@example.com", role="admin", is_staff=True)
        staff.user_permissions.set(Permission.objects.filter(content_type__app_label="accounts"))
        self.client.force_login(staff)
        for path in (
            reverse("admin:accounts_user_changelist"),
            reverse("admin:accounts_user_add"),
            reverse("admin:accounts_user_change", args=[staff.pk]),
        ):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_superuser_admin_can_create_appraiser(self):
        self.client.force_login(self.owner)
        url = reverse("admin:accounts_user_add")
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(
            url,
            {
                "email": "APPRAISER@example.com",
                "first_name": "Мира",
                "last_name": "",
                "role": "appraiser",
                "preferred_language": "ru",
                "phone_number": "+77017700026",
                "password1": "Test-Appraiser-Password!73",
                "password2": "Test-Appraiser-Password!73",
                "is_active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        appraiser = User.objects.get(email="appraiser@example.com")
        self.assertEqual(appraiser.role, "appraiser")
        self.assertFalse(appraiser.is_staff)
        self.assertTrue(appraiser.check_password("Test-Appraiser-Password!73"))
        self.assertIsNone(appraiser.email_verified_at)

    def test_admin_login_requires_csrf(self):
        client = APIClient(enforce_csrf_checks=True)
        response = client.post(
            reverse("admin:login"),
            {
                "username": self.owner.email,
                "password": "Strong-Test-Password!42",
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, 403)

    def test_superuser_cannot_delete_users_via_admin(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("admin:accounts_user_delete", args=[self.user.pk]), {"post": "yes"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
