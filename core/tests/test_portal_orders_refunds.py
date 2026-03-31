"""Portal-scoped orders list and refund request / execute flows."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    Category,
    FamilyGroup,
    Order,
    Product,
    Refund,
    User,
    Vendor,
    Wallet,
)
from core.services.base import get_or_create_personal_wallet
from core.services.order_service import pay_with_wallet
from core.views.vendor.vendor_resources import _gen_order_number


class PortalOrdersSurfaceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="surfu1",
            password="x",
            phone="9811111111",
            name="Surf User",
            role=User.Role.NORMAL,
        )
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

        self.vendor_user = User.objects.create_user(
            username="surfvend",
            password="x",
            phone="9822222222",
            name="V",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Surf Store",
            status=Vendor.Status.APPROVED,
        )
        self.cat = Category.objects.create(name="C1", slug="c1-surf")
        self.product = Product.objects.create(
            name="P1",
            sku="SKU-SURF-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("100.00"),
            stock=50,
            status=Product.Status.ACTIVE,
        )

    def _create_order(self, *, placed_portal: str | None, total: Decimal = Decimal("100.00")):
        return Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=total,
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=total,
            placed_portal=placed_portal,
        )

    def test_main_list_includes_legacy_null_portal(self):
        self._create_order(placed_portal=None)
        r = self.client.get("/api/portal/orders/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["results"]), 1)

    def test_main_list_excludes_family_only_orders(self):
        self._create_order(placed_portal=Order.PlacedPortal.PORTAL_FAMILY)
        r = self.client.get("/api/portal/orders/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["results"]), 0)

    def test_family_list_only_family_orders(self):
        self._create_order(placed_portal=Order.PlacedPortal.PORTAL_FAMILY)
        self._create_order(placed_portal=None)
        self.user.role = User.Role.PARENT
        self.user.save()
        FamilyGroup.objects.create(
            name="G1",
            leader=self.user,
            status=FamilyGroup.Status.ACTIVE,
        )
        r = self.client.get("/api/family-portal/orders/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["results"]), 1)


class RefundExecuteWalletTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="refwu1",
            password="x",
            phone="9833333333",
            name="Ref Wallet",
            role=User.Role.NORMAL,
        )
        self.wallet = get_or_create_personal_wallet(self.user)
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("500.00"))
        self.vendor_user = User.objects.create_user(
            username="refwv",
            password="x",
            phone="9844444444",
            name="V2",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Ref Store",
            status=Vendor.Status.APPROVED,
        )
        self.cat = Category.objects.create(name="C2", slug="c2-ref")
        self.product = Product.objects.create(
            name="P2",
            sku="SKU-REF-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("80.00"),
            stock=10,
            status=Product.Status.ACTIVE,
        )

    def test_partial_approved_refund_does_not_mark_order_refunded(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("80.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("80.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            payment_wallet=self.wallet,
        )
        pay_with_wallet(order, self.wallet, fund_source="Personal wallet")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)

        rf = Refund.objects.create(
            refund_number="RF-TEST-PARTIAL-1",
            order=order,
            customer=self.user,
            amount=Decimal("30.00"),
            reason="partial",
            status=Refund.Status.PENDING,
        )
        rf.status = Refund.Status.APPROVED
        rf.save()
        order.refresh_from_db()
        self.assertNotEqual(order.status, Order.Status.REFUNDED)
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        bal = Wallet.objects.get(pk=self.wallet.pk).balance
        self.assertEqual(bal, Decimal("450.00"))
