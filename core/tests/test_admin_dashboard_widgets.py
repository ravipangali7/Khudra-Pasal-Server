"""Admin dashboard sales-series fill, wallet-series, low-stock endpoints."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Category, Order, Product, User, Wallet, WalletTransaction
from core.services import wallet_service


class AdminDashboardWidgetsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.super_admin = User.objects.create_user(
            username="dash_sa",
            password=self.pw,
            phone="9711111111",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.customer = User.objects.create_user(
            username="dash_cust",
            password=self.pw,
            phone="9722222222",
            name="Cust",
            role=User.Role.NORMAL,
        )
        self.cat = Category.objects.create(name="DashCat", slug="dash-cat-w")
        from django.core.files.uploadedfile import SimpleUploadedFile

        img = SimpleUploadedFile(
            "p.png",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0b\x13\x00\x00\x0b\x13\x01\x00\x9a\x9c\x18\x00\x00\x00\nIDATx\x9cc\xf8\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82",
            content_type="image/png",
        )
        self.product_low = Product.objects.create(
            name="LowSock",
            slug="low-sock-w",
            sku="SKU-LOW-W",
            price=Decimal("10.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=3,
            status=Product.Status.ACTIVE,
        )
        Product.objects.create(
            name="OkStock",
            slug="ok-sock-w",
            sku="SKU-OK-W",
            price=Decimal("10.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=50,
            status=Product.Status.ACTIVE,
        )

    def _token(self, user: User) -> str:
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def test_sales_series_filled_length(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get("/api/admin/dashboard/sales-series/", {"days": 7})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(len(r.data), 7)
        for row in r.data:
            self.assertIn("day", row)
            self.assertIn("sales", row)
            self.assertIn("orders", row)
            self.assertEqual(row["sales"], 0.0)
            self.assertEqual(row["orders"], 0)

    def test_wallet_series_shape_and_totals(self):
        platform = wallet_service.get_or_create_platform_commission_wallet()
        now = timezone.now()
        WalletTransaction.objects.create(
            txn_id="TX-DASH-001",
            wallet=platform,
            type=WalletTransaction.Type.TOPUP,
            amount=Decimal("100.00"),
            description="Test topup",
            status=WalletTransaction.Status.COMPLETED,
        )
        WalletTransaction.objects.filter(txn_id="TX-DASH-001").update(created_at=now)

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get("/api/admin/dashboard/wallet-series/", {"days": 7})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertIn("series", r.data)
        self.assertIn("totals", r.data)
        self.assertEqual(len(r.data["series"]), 7)
        self.assertIn("inflow", r.data["totals"])
        self.assertIn("outflow", r.data["totals"])
        self.assertGreaterEqual(r.data["totals"]["inflow"], 100.0)

    def test_recent_orders_days_filter(self):
        from decimal import Decimal

        now = timezone.now()
        old = now - timedelta(days=20)
        Order.objects.create(
            order_number="DASH-REC-NEW",
            customer=self.customer,
            seller=None,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
            want_delivery=False,
        )
        Order.objects.create(
            order_number="DASH-REC-OLD",
            customer=self.customer,
            seller=None,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("30.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("30.00"),
            want_delivery=False,
        )
        Order.objects.filter(order_number="DASH-REC-NEW").update(created_at=now)
        Order.objects.filter(order_number="DASH-REC-OLD").update(created_at=old)

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get("/api/admin/dashboard/recent-orders/", {"days": 7, "limit": 20})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        numbers = [row["order_number"] for row in r.data]
        self.assertIn("DASH-REC-NEW", numbers)
        self.assertNotIn("DASH-REC-OLD", numbers)

        r2 = self.client.get("/api/admin/dashboard/recent-orders/", {"limit": 20})
        self.assertEqual(r2.status_code, status.HTTP_200_OK, r2.content)
        numbers2 = [row["order_number"] for row in r2.data]
        self.assertIn("DASH-REC-NEW", numbers2)
        self.assertIn("DASH-REC-OLD", numbers2)

    def test_low_stock_respects_threshold(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get("/api/admin/dashboard/low-stock/", {"threshold": 15, "limit": 10})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(r.data["threshold"], 15)
        skus = {x["sku"] for x in r.data["results"]}
        self.assertIn("SKU-LOW-W", skus)
        self.assertNotIn("SKU-OK-W", skus)

    def test_unauthenticated_forbidden(self):
        self.client.credentials()
        for path in (
            "/api/admin/dashboard/sales-series/",
            "/api/admin/dashboard/wallet-series/",
            "/api/admin/dashboard/low-stock/",
        ):
            r = self.client.get(path)
            self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED, path)
