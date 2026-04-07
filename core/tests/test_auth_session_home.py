"""GET /api/auth/session-home/ and primary_spa_redirect ordering."""

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import FamilyGroup, User, Vendor
from core.portal_roles import primary_spa_redirect


class PrimarySpaRedirectTests(TestCase):
    def setUp(self):
        self.pw = "TestPass123!"

    def test_vendor_before_family_when_user_has_both(self):
        """NORMAL seller with vendor profile + active family leadership → vendor wins."""
        u = User.objects.create_user(
            username="venfam",
            password=self.pw,
            phone="9810101010",
            name="VenFam",
            role=User.Role.NORMAL,
        )
        Vendor.objects.create(
            user=u,
            store_name="VF Store",
            store_slug="vf-store",
        )
        FamilyGroup.objects.create(
            name="VF Fam",
            leader=u,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        self.assertEqual(primary_spa_redirect(u), "/vendor")

    def test_family_before_child_portal_for_parent_not_child(self):
        """Parent with family access maps to family-portal, not child."""
        u = User.objects.create_user(
            username="paronly",
            password=self.pw,
            phone="9820202020",
            name="ParentOnly",
            role=User.Role.PARENT,
        )
        FamilyGroup.objects.create(
            name="P Fam",
            leader=u,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        self.assertEqual(primary_spa_redirect(u), "/family-portal")

    def test_child_maps_to_child_portal(self):
        u = User.objects.create_user(
            username="chonly",
            password=self.pw,
            phone="9830303030",
            name="ChildOnly",
            role=User.Role.CHILD,
        )
        self.assertEqual(primary_spa_redirect(u), "/child-portal")


class AuthSessionHomeApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.normal = User.objects.create_user(
            username="sh_norm",
            password=self.pw,
            phone="9840404040",
            name="Normal",
            role=User.Role.NORMAL,
        )
        self.vendor_user = User.objects.create_user(
            username="sh_ven",
            password=self.pw,
            phone="9850505050",
            name="VendorU",
            role=User.Role.NORMAL,
        )
        Vendor.objects.create(
            user=self.vendor_user,
            store_name="SH Store",
            store_slug="sh-store",
        )

    def test_session_home_requires_authentication(self):
        r = self.client.get("/api/auth/session-home/")
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_session_home_normal_customer(self):
        token, _ = Token.objects.get_or_create(user=self.normal)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        r = self.client.get("/api/auth/session-home/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data.get("redirect"), "/portal")

    def test_session_home_vendor_even_if_role_normal(self):
        token, _ = Token.objects.get_or_create(user=self.vendor_user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        r = self.client.get("/api/auth/session-home/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data.get("redirect"), "/vendor")
