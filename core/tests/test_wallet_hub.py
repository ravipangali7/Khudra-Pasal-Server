"""Wallet hub: transfer ID + cross-portal transfer API."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import User, WalletSettings, WalletTransferCode
from core.services.base import get_or_create_personal_wallet
from core.tests.wallet_test_settings import relax_wallet_settings_for_tests


class WalletHubAPITests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        ws = WalletSettings.load()
        ws.cross_portal_transfer_by_code_enabled = True
        ws.transaction_fee_type = WalletSettings.FeeType.FLAT
        ws.transaction_fee_value = Decimal("0")
        ws.save(
            update_fields=[
                "cross_portal_transfer_by_code_enabled",
                "transaction_fee_type",
                "transaction_fee_value",
            ]
        )
        self.client = APIClient()
        self.pw = "HubTestWallet123!"

    def _token(self, user):
        t, _ = Token.objects.get_or_create(user=user)
        return t.key

    def test_transfer_disabled_returns_error(self):
        ws = WalletSettings.load()
        ws.cross_portal_transfer_by_code_enabled = False
        ws.save(update_fields=["cross_portal_transfer_by_code_enabled"])

        sender = User.objects.create_user(
            username="hub_sd",
            password=self.pw,
            phone="9810300301",
            name="S",
            role=User.Role.NORMAL,
        )
        recv = User.objects.create_user(
            username="hub_rv",
            password=self.pw,
            phone="9810300302",
            name="R",
            role=User.Role.NORMAL,
        )
        WalletTransferCode.objects.create(user=recv, code="HUBDISABLE01")
        sw = get_or_create_personal_wallet(sender)
        sw.balance = Decimal("500")
        sw.save(update_fields=["balance"])

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(sender)}")
        r = self.client.post(
            "/api/wallet-hub/wallet/transfer/",
            {
                "transfer_id": "HUBDISABLE01",
                "amount": "10.00",
                "client_ref": "idem-disabled-1",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("disabled", (r.data.get("detail") or "").lower())

    def test_create_lookup_transfer_and_idempotency(self):
        sender = User.objects.create_user(
            username="hub_s1",
            password=self.pw,
            phone="9810300311",
            name="Sender",
            role=User.Role.NORMAL,
        )
        recv = User.objects.create_user(
            username="hub_r1",
            password=self.pw,
            phone="9810300312",
            name="Receiver",
            role=User.Role.NORMAL,
        )
        sw = get_or_create_personal_wallet(sender)
        sw.balance = Decimal("1000")
        sw.save(update_fields=["balance"])

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(recv)}")
        c = self.client.post("/api/wallet-hub/transfer-id/create/", {}, format="multipart")
        self.assertEqual(c.status_code, status.HTTP_201_CREATED)
        code = c.data["code"]
        self.assertEqual(len(code), 12)

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(sender)}")
        lu = self.client.get(f"/api/wallet-hub/transfer-id/{code}/")
        self.assertEqual(lu.status_code, status.HTTP_200_OK)
        self.assertIn("display_name", lu.data)

        idem = "idem-hub-test-unique-key-1"
        r1 = self.client.post(
            "/api/wallet-hub/wallet/transfer/",
            {"transfer_id": code, "amount": "25.00", "client_ref": idem},
            format="json",
        )
        self.assertEqual(r1.status_code, status.HTTP_200_OK)
        self.assertTrue(r1.data.get("ok"))
        self.assertIn("outbound_txn_id", r1.data)
        sw.refresh_from_db()
        self.assertEqual(sw.balance, Decimal("975"))

        r2 = self.client.post(
            "/api/wallet-hub/wallet/transfer/",
            {"transfer_id": code, "amount": "25.00", "client_ref": idem},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r1.data["outbound_txn_id"], r2.data["outbound_txn_id"])
        sw.refresh_from_db()
        self.assertEqual(sw.balance, Decimal("975"))

    def test_self_transfer_rejected(self):
        u = User.objects.create_user(
            username="hub_self",
            password=self.pw,
            phone="9810300321",
            name="Self",
            role=User.Role.NORMAL,
        )
        WalletTransferCode.objects.create(user=u, code="HUBSELF00001")
        w = get_or_create_personal_wallet(u)
        w.balance = Decimal("100")
        w.save(update_fields=["balance"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(u)}")
        r = self.client.post(
            "/api/wallet-hub/wallet/transfer/",
            {
                "transfer_id": "HUBSELF00001",
                "amount": "5.00",
                "client_ref": "idem-self-transfer-xx",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_insufficient_balance(self):
        sender = User.objects.create_user(
            username="hub_sb",
            password=self.pw,
            phone="9810300331",
            name="Poor",
            role=User.Role.NORMAL,
        )
        recv = User.objects.create_user(
            username="hub_rb",
            password=self.pw,
            phone="9810300332",
            name="Rich",
            role=User.Role.NORMAL,
        )
        WalletTransferCode.objects.create(user=recv, code="HUBPOOR00001")
        sw = get_or_create_personal_wallet(sender)
        sw.balance = Decimal("3")
        sw.save(update_fields=["balance"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(sender)}")
        r = self.client.post(
            "/api/wallet-hub/wallet/transfer/",
            {
                "transfer_id": "HUBPOOR00001",
                "amount": "50.00",
                "client_ref": "idem-poor-1",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("balance", (r.data.get("detail") or "").lower())
