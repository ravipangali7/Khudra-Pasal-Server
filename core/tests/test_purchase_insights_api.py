"""Purchase insights APIs (vendor-scoped vs admin marketplace-wide)."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Category, Order, OrderItem, Product, User, Vendor


def _tiny_png() -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "p.png",
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0b\x13\x00\x00\x0b\x13\x01\x00\x9a\x9c\x18\x00\x00\x00\nIDATx\x9cc\xf8\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82",
        content_type="image/png",
    )


class PurchaseInsightsApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.cat = Category.objects.create(name="PI Cat", slug="pi-cat")

        self.vendor_user = User.objects.create_user(
            username="pi_vendor",
            password=self.pw,
            phone="9766666666",
            name="PI Vendor",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="PI Store",
            store_slug="pi-store",
            status=Vendor.Status.APPROVED,
        )

        self.other_vendor_user = User.objects.create_user(
            username="pi_other",
            password=self.pw,
            phone="9777777777",
            name="Other",
            role=User.Role.NORMAL,
        )
        self.other_vendor = Vendor.objects.create(
            user=self.other_vendor_user,
            store_name="Other Store",
            store_slug="pi-other",
            status=Vendor.Status.APPROVED,
        )

        self.customer = User.objects.create_user(
            username="pi_cust",
            password=self.pw,
            phone="9788888888",
            name="Buyer",
            role=User.Role.NORMAL,
        )

        self.admin = User.objects.create_user(
            username="pi_admin",
            password=self.pw,
            phone="9799999999",
            name="Admin",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )

        img = _tiny_png()
        self.product = Product.objects.create(
            name="PI Product",
            slug="pi-product",
            sku="PI-SKU",
            price=Decimal("50.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=20,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )

        self.order_a = Order.objects.create(
            order_number="PI-ORDER-A",
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
            want_delivery=False,
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            is_pos_order=False,
        )
        OrderItem.objects.create(
            order=self.order_a,
            product=self.product,
            quantity=2,
            unit_price=Decimal("25.00"),
            total_price=Decimal("50.00"),
        )

        self.order_b = Order.objects.create(
            order_number="PI-ORDER-B",
            customer=self.customer,
            seller=self.other_vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("30.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("30.00"),
            want_delivery=False,
        )

    def _vendor_token(self) -> str:
        r = self.client.post(
            "/api/vendor/auth/login/",
            {"phone": self.vendor_user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def _admin_token(self) -> str:
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": self.admin.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def test_vendor_purchase_insights_scoped_to_seller(self):
        tok = self._vendor_token()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        today = timezone.localdate().isoformat()
        r = self.client.get(f"/api/vendor/purchase-insights/?from={today}&to={today}")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(r.data.get("role"), "vendor")
        kpis = r.data.get("kpis") or {}
        self.assertEqual(kpis.get("order_count"), 1)
        self.assertEqual(kpis.get("gross_sales"), 50.0)
        self.assertEqual(kpis.get("items_sold"), 2)
        recent = r.data.get("recent_orders") or []
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].get("order_number"), "PI-ORDER-A")
        self.assertNotIn("id", recent[0])

    def test_admin_purchase_insights_marketplace_wide(self):
        tok = self._admin_token()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        today = timezone.localdate().isoformat()
        r = self.client.get(f"/api/admin/purchase-insights/?from={today}&to={today}")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.assertEqual(r.data.get("role"), "admin")
        kpis = r.data.get("kpis") or {}
        self.assertEqual(kpis.get("order_count"), 2)
        self.assertEqual(kpis.get("gross_sales"), 80.0)
        recent = r.data.get("recent_orders") or []
        self.assertTrue(any(row.get("id") == self.order_b.pk for row in recent))
