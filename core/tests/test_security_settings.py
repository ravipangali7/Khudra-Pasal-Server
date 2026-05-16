"""SecuritySettings toggles, flag resolution audit, and security module RBAC."""

import os
from unittest.mock import Mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from core.models import (
    AuditLog,
    EmployeeProfile,
    FamilyGroup,
    FamilyJoinRequest,
    FlaggedActivity,
    Role,
    SecuritySettings,
    User,
)
from core.services import wallet_service
from core.services.family_portal_join_link_service import (
    create_or_rotate_link,
    submit_join_application,
)
from core.throttles import OtpSendThrottle


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "security-settings-tests",
        }
    }
)
class SecuritySettingsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.pw = "TestPass123!"
        ss = SecuritySettings.load()
        ss.otp_sensitive_crud = False
        ss.rbac_enforced = True
        ss.duplicate_prevention = True
        ss.auto_lock_failed_logins = True
        ss.ip_rate_limiting = True
        ss.save()
        self.super_admin = User.objects.create_user(
            username="sec_sa",
            password=self.pw,
            phone="+977-9811111111",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.staff_sec = User.objects.create_user(
            username="sec_staff",
            password=self.pw,
            phone="+977-9822222222",
            name="StaffSec",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        self.role_sec = Role.objects.create(
            name="Security Only",
            permissions={"security": True},
            status=Role.Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=self.staff_sec,
            role=self.role_sec,
            modules_access=["dashboard", "security"],
            status=EmployeeProfile.Status.ACTIVE,
        )
        self.staff_settings_only = User.objects.create_user(
            username="sec_setonly",
            password=self.pw,
            phone="+977-9833333333",
            name="StaffSet",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        self.role_settings = Role.objects.create(
            name="Settings Only",
            permissions={"settings": True},
            status=Role.Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=self.staff_settings_only,
            role=self.role_settings,
            modules_access=["dashboard", "settings"],
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

    def test_flag_resolve_logs_security_audit_once(self):
        flag = FlaggedActivity.objects.create(
            activity_type="Test flag",
            detail="d",
            severity=FlaggedActivity.Severity.LOW,
            status=FlaggedActivity.Status.OPEN,
        )
        before = AuditLog.objects.filter(
            type=AuditLog.Type.SECURITY,
            object_type="FlaggedActivity",
            object_id=str(flag.pk),
        ).count()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.patch(
            f"/api/admin/flagged/{flag.pk}/",
            {"status": "resolved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        after = AuditLog.objects.filter(
            type=AuditLog.Type.SECURITY,
            object_type="FlaggedActivity",
            object_id=str(flag.pk),
        ).count()
        self.assertEqual(after - before, 1)

    def test_flag_resolve_high_requires_resolution_note(self):
        flag = FlaggedActivity.objects.create(
            activity_type="High risk",
            detail="suspicious",
            severity=FlaggedActivity.Severity.HIGH,
            status=FlaggedActivity.Status.OPEN,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.patch(
            f"/api/admin/flagged/{flag.pk}/",
            {"status": "resolved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)
        r = self.client.patch(
            f"/api/admin/flagged/{flag.pk}/",
            {
                "status": "resolved",
                "resolution_note": "Investigated duplicate login attempts; cleared.",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        flag.refresh_from_db()
        self.assertEqual(flag.status, FlaggedActivity.Status.RESOLVED)
        self.assertIn("Investigated", flag.detail)

    def test_security_module_rbac_employee_with_security_can_list_flagged(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.staff_sec)}")
        r = self.client.get("/api/admin/flagged/", {"page_size": 5})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_security_module_rbac_settings_only_forbidden_flagged(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.staff_settings_only)}")
        r = self.client.get("/api/admin/flagged/", {"page_size": 5})
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN, r.content)

    def test_rbac_enforced_off_allows_settings_only_flagged(self):
        ss = SecuritySettings.load()
        ss.rbac_enforced = False
        ss.save()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.staff_settings_only)}")
        r = self.client.get("/api/admin/flagged/", {"page_size": 5})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_wallet_adjust_succeeds_without_sensitive_otp(self):
        ss = SecuritySettings.load()
        ss.otp_sensitive_crud = True
        ss.save()
        w = wallet_service.get_or_create_personal_wallet(self.super_admin)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.post(
            "/api/admin/wallets/adjust/",
            {
                "wallet_id": str(w.pk),
                "amount": "1",
                "direction": "credit",
                "reason": "test",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_admin_login_auto_lock_after_failures(self):
        victim = User.objects.create_user(
            username="lock_victim",
            password=self.pw,
            phone="+977-9844444444",
            name="LockMe",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        for _ in range(int(os.environ.get("ADMIN_LOGIN_FAIL_MAX", "5"))):
            r = self.client.post(
                "/api/admin/auth/login/",
                {"phone": victim.phone, "password": "wrong-password"},
                format="json",
            )
            self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        victim.refresh_from_db()
        self.assertFalse(victim.is_active)

    def test_ip_rate_limiting_off_bypasses_otp_throttle_allow(self):
        ss = SecuritySettings.load()
        ss.ip_rate_limiting = False
        ss.save()
        throttle = OtpSendThrottle()
        self.assertTrue(throttle.allow_request(Mock(), None))

    def test_duplicate_prevention_off_allows_second_pending_join_request(self):
        ss = SecuritySettings.load()
        ss.duplicate_prevention = False
        ss.save()
        leader = User.objects.create_user(
            username="dup_lead",
            password=self.pw,
            phone="+977-9866666666",
            name="L",
            role=User.Role.PARENT,
            is_staff=False,
        )
        group = FamilyGroup.objects.create(
            name="G1",
            leader=leader,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        link = create_or_rotate_link(creator=leader, group=group)
        applicant = User.objects.create_user(
            username="dup_app",
            password=self.pw,
            phone="+977-9877777777",
            name="A",
            role=User.Role.NORMAL,
        )
        submit_join_application(
            link=link,
            applicant_user=applicant,
            name="A",
            email="",
            phone="9877777777",
        )
        submit_join_application(
            link=link,
            applicant_user=applicant,
            name="A2",
            email="",
            phone="9877777777",
        )
        n = FamilyJoinRequest.objects.filter(
            group=group,
            phone="9877777777",
            status=FamilyJoinRequest.Status.PENDING,
        ).count()
        self.assertEqual(n, 2)
