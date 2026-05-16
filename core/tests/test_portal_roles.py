"""Portal login role gates (TASK-admin-portal-login-roles)."""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import FamilyGroup, FamilyMember, User


class PortalRoleLoginTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"

        self.u_normal = User.objects.create_user(
            username="cust1",
            password=self.pw,
            phone="9811111111",
            name="Customer",
            role=User.Role.NORMAL,
        )
        self.u_parent = User.objects.create_user(
            username="par1",
            password=self.pw,
            phone="9822222222",
            name="Parent",
            role=User.Role.PARENT,
        )
        self.u_child = User.objects.create_user(
            username="ch1",
            password=self.pw,
            phone="9833333333",
            name="Child",
            role=User.Role.CHILD,
        )
        self.u_super = User.objects.create_user(
            username="sa1",
            password=self.pw,
            phone="9844444444",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.u_inactive = User.objects.create_user(
            username="inact",
            password=self.pw,
            phone="9855555555",
            name="Inactive",
            role=User.Role.NORMAL,
            is_active=False,
        )

    def test_portal_login_allows_normal_only(self):
        r = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.u_normal.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("token", r.data)

        r2 = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.u_parent.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_403_FORBIDDEN)

    def test_family_portal_login_allows_parent(self):
        FamilyGroup.objects.create(
            name="Parent led",
            leader=self.u_parent,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        r = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": self.u_parent.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        r2 = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": self.u_normal.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_403_FORBIDDEN)

    def test_family_portal_login_allows_leader_without_parent_role(self):
        """Leader of an active family group may still be User.role normal (e.g. admin-created)."""
        leader = User.objects.create_user(
            username="lead_norm",
            password=self.pw,
            phone="9877777777",
            name="LeaderNorm",
            role=User.Role.NORMAL,
        )
        FamilyGroup.objects.create(
            name="Admin Fam",
            leader=leader,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        r = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": leader.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("token", r.data)

    def test_family_portal_login_allows_spouse_membership(self):
        spouse = User.objects.create_user(
            username="spouse1",
            password=self.pw,
            phone="9866666666",
            name="Spouse",
            role=User.Role.NORMAL,
        )
        group = FamilyGroup.objects.create(
            name="Test Fam",
            leader=self.u_parent,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        FamilyMember.objects.create(
            group=group,
            user=spouse,
            role=FamilyMember.Role.SPOUSE,
            status=FamilyMember.Status.ACTIVE,
        )
        r = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": spouse.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("token", r.data)

    def test_child_portal_login_allows_child(self):
        r = self.client.post(
            "/api/child-portal/auth/login/",
            {"phone": self.u_child.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_family_portal_login_rejects_child_role(self):
        r = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": self.u_child.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_login_super_admin(self):
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": self.u_super.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_admin_login_super_admin_by_email(self):
        self.u_super.email = "super@example.com"
        self.u_super.save(update_fields=["email"])
        r = self.client.post(
            "/api/admin/auth/login/",
            {"email": "super@example.com", "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("token", r.data)
        r2 = self.client.post(
            "/api/admin/auth/login/",
            {"phone": self.u_normal.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_403_FORBIDDEN)

    def test_unified_login_requires_portal(self):
        r = self.client.post(
            "/api/auth/login/",
            {"phone": self.u_normal.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unified_login_wrong_role(self):
        r = self.client.post(
            "/api/auth/login/",
            {"phone": self.u_normal.phone, "password": self.pw, "portal": "admin"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_inactive_cannot_login_portal(self):
        r = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.u_inactive.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
