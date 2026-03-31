"""GET /portal/orders/checkout-wallet/ — default and payable wallets for checkout."""

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import User
from core.services.base import get_or_create_personal_wallet


class CheckoutWalletContextTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="chkctx1",
            password="x",
            phone="9819191919",
            name="CheckoutCtx",
            role=User.Role.NORMAL,
        )
        token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    def test_returns_default_personal_wallet(self):
        w = get_or_create_personal_wallet(self.user)
        r = self.client.get("/api/portal/orders/checkout-wallet/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(r.data["default"])
        self.assertEqual(r.data["default"]["id"], w.pk)
        self.assertEqual(r.data["default"]["fund_source"], "Personal wallet")
        self.assertIn("payable_wallets", r.data)
        self.assertTrue(any(x["id"] == w.pk for x in r.data["payable_wallets"]))
        self.assertTrue(any(x.get("is_default") for x in r.data["payable_wallets"]))
