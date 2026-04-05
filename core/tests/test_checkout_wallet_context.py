"""GET /portal/orders/checkout-wallet/ — default and payable wallets for checkout."""

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from decimal import Decimal

from core.models import FamilyGroup, FamilyMember, User, Wallet
from core.services import family_service
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

    def test_child_checkout_default_uses_adopted_family_wallet(self):
        leader = User.objects.create_user(
            username="chklead",
            password="x",
            phone="9810000001",
            name="L",
            role=User.Role.NORMAL,
        )
        child = User.objects.create_user(
            username="chkchild",
            password="x",
            phone="9810000002",
            name="C",
            role=User.Role.NORMAL,
        )
        group = FamilyGroup.objects.create(
            name="ChkG",
            leader=leader,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        pw = get_or_create_personal_wallet(child)
        Wallet.objects.filter(pk=pw.pk).update(balance=Decimal("750.00"))
        User.objects.filter(pk=child.pk).update(role=User.Role.CHILD)
        FamilyMember.objects.create(
            group=group,
            user=child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        family_service.ensure_family_wallets_for_member(
            group, child, FamilyMember.Role.CHILD
        )

        token = Token.objects.create(user=child)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        r = self.client.get("/api/portal/orders/checkout-wallet/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(r.data["default"])
        self.assertEqual(r.data["default"]["id"], pw.pk)
        self.assertEqual(float(r.data["default"]["balance"]), 750.0)
