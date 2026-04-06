"""Admin dashboard reports snapshot API (filters, KPIs, series, RBAC)."""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Category, Order, OrderItem, Product, User, Vendor
from core.services.vendor_service import ensure_vendor_wallet


class AdminDashboardReportsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.super_admin = User.objects.create_user(
            username="rep_sa",
            password=self.pw,
            phone="9611111111",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.customer = User.objects.create_user(
            username="rep_cust",
            password=self.pw,
            phone="9622222222",
            name="Cust",
            role=User.Role.NORMAL,
        )
        self.vu = User.objects.create_user(
            username="rep_vu",
            password=self.pw,
            phone="9633333333",
            name="VU",
        )
        self.vendor = Vendor.objects.create(
            user=self.vu,
            store_name="Rep Store",
            store_slug="rep-store-rpt",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        ensure_vendor_wallet(self.vendor)
        self.cat = Category.objects.create(name="RepCat", slug="rep-cat-rpt")
        from django.core.files.uploadedfile import SimpleUploadedFile

        img = SimpleUploadedFile(
            "p.png",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0b\x13\x00\x00\x0b\x13\x01\x00\x9a\x9c\x18\x00\x00\x00\nIDATx\x9cc\xf8\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82",
            content_type="image/png",
        )
        self.product = Product.objects.create(
            name="RepProd",
            slug="rep-prod-rpt",
            sku="SKU-REP-RPT",
            price=Decimal("50.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=5,
            seller=self.vendor,
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

    def test_requires_dates(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get("/api/admin/dashboard/reports/")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_range_structure(self):
        today = timezone.localdate()
        start = today - timedelta(days=2)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get(
            "/api/admin/dashboard/reports/",
            {"date_from": start.isoformat(), "date_to": today.isoformat()},
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertIn("kpis", r.data)
        self.assertIn("series", r.data)
        self.assertEqual(len(r.data["series"]), 3)
        self.assertEqual(r.data["kpis"]["total_sales"], 0.0)
        self.assertEqual(r.data["kpis"]["total_orders"], 0)
        self.assertIsNone(r.data["kpis"]["sales_growth_pct"])

    def test_kpis_and_vendor_filter(self):
        today = timezone.localdate()
        o = Order.objects.create(
            order_number="RPT-001",
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("100.00"),
            want_delivery=False,
        )
        OrderItem.objects.create(
            order=o,
            product=self.product,
            quantity=2,
            unit_price=Decimal("50.00"),
            total_price=Decimal("100.00"),
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get(
            "/api/admin/dashboard/reports/",
            {"date_from": today.isoformat(), "date_to": today.isoformat()},
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(r.data["kpis"]["total_orders"], 1)
        self.assertEqual(r.data["kpis"]["total_sales"], 100.0)
        self.assertEqual(len(r.data["category_breakdown"]), 1)
        self.assertEqual(r.data["category_breakdown"][0]["name"], "RepCat")

        r2 = self.client.get(
            "/api/admin/dashboard/reports/",
            {
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
                "vendor_id": 99999,
            },
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r2.data["kpis"]["total_orders"], 0)

    def test_orders_list_date_and_category_filter(self):
        today = date.today()
        o = Order.objects.create(
            order_number="RPT-ORD-LIST",
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("10.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("10.00"),
            want_delivery=False,
        )
        OrderItem.objects.create(
            order=o,
            product=self.product,
            quantity=1,
            unit_price=Decimal("10.00"),
            total_price=Decimal("10.00"),
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        r = self.client.get(
            "/api/admin/orders/",
            {
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
                "category_id": self.cat.pk,
                "page_size": 10,
            },
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertGreaterEqual(len(r.data.get("results", [])), 1)
