"""Wallet gateway top-up (PSP) behaviour."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import FamilyGroup, User
from core.tests.wallet_test_settings import relax_wallet_settings_for_tests
from core.services.family_portal_wallet_service import get_default_shared_wallet


class WalletGatewayTopupTests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.leader = User.objects.create_user(
            username="gw_parent",
            password=self.pw,
            phone="9899999999",
            name="GW Parent",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        login = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.leader.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        r = self.client.post("/api/portal/family/group/", {"name": "GW Fam"}, format="json")
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.leader.refresh_from_db()
        self.assertEqual(self.leader.role, User.Role.PARENT)

    def test_family_wallet_load_rejects_bank_method(self):
        r = self.client.post(
            "/api/portal/family/wallet/load/",
            {"amount": "10", "method": "bank"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_family_wallet_esewa_returns_redirect_without_crediting(self):
        group = FamilyGroup.objects.get(leader=self.leader)
        shared = get_default_shared_wallet(group)
        self.assertIsNotNone(shared)
        self.assertEqual(shared.balance, Decimal("0.00"))
        r = self.client.post(
            "/api/portal/family/wallet/load/",
            {"amount": "99", "method": "esewa"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data.get("flow"), "esewa_redirect")
        self.assertIn("action_url", r.data)
        shared.refresh_from_db()
        self.assertEqual(shared.balance, Decimal("0.00"))
