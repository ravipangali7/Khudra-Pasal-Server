"""Super-admin database cleanup API (RBAC, validation, registry safety)."""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import AuditLog, EmployeeProfile, Role, User
from core.services import db_cleanup


class AdminDbCleanupTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.super_admin = User.objects.create_user(
            username="cleanup_sa",
            password=self.pw,
            phone="9711111111",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.staff = User.objects.create_user(
            username="cleanup_staff",
            password=self.pw,
            phone="9722222222",
            name="Staff",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        self.role = Role.objects.create(
            name="Cleanup Test Role",
            permissions={"settings": True},
            status=Role.Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=self.staff,
            role=self.role,
            modules_access=["settings"],
            status=EmployeeProfile.Status.ACTIVE,
        )

    def _token(self, user: User) -> str:
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def test_registry_has_no_protected_models(self):
        for mod in db_cleanup._CLEANUP_MODULES_LIST:
            for model in mod.models:
                self.assertNotIn(model, db_cleanup._PROTECTED_MODELS, mod.id)

    def test_super_admin_can_list_modules(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get("/api/admin/system/cleanup-modules/")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertIn("modules", r.data)
        ids = {m["id"] for m in r.data["modules"]}
        self.assertIn("orders", ids)
        self.assertIn("products", ids)
        self.assertNotIn("users", ids)

    def test_staff_forbidden_cleanup_list(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.staff)}")
        r = self.client.get("/api/admin/system/cleanup-modules/")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN, r.content)

    def test_staff_forbidden_cleanup_execute(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.staff)}")
        r = self.client.post(
            "/api/admin/system/cleanup/",
            {"module_ids": ["audit_logs"]},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN, r.content)

    def test_cleanup_rejects_users_slug(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.post(
            "/api/admin/system/cleanup/",
            {"module_ids": ["users"]},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)

    def test_cleanup_rejects_unknown_module(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.post(
            "/api/admin/system/cleanup/",
            {"module_ids": ["not_a_real_module"]},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)

    def test_cleanup_requires_module_ids(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.post("/api/admin/system/cleanup/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)

    def test_cleanup_audit_logs_clears_rows(self):
        row = AuditLog.objects.create(
            action="cleanup_fixture_row",
            type=AuditLog.Type.SETTINGS,
            action_kind=AuditLog.ActionKind.READ,
        )
        fixture_pk = row.pk
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.post(
            "/api/admin/system/cleanup/",
            {"module_ids": ["audit_logs"]},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertTrue(r.data.get("ok"))
        # Admin audit middleware may write a new SETTINGS log for this POST.
        self.assertFalse(AuditLog.objects.filter(pk=fixture_pk).exists())
