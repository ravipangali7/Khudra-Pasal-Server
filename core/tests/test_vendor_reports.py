"""Vendor portal reports summary and CSV export."""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Category, Order, OrderItem, Product, User, Vendor
from core.views.vendor.vendor_resources import _gen_order_number


class VendorReportsApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = User.objects.create_user(
            username="vrepcust",
            password="x",
            phone="9855555501",
            name="Buyer",
            role=User.Role.NORMAL,
        )
        self.vendor_user = User.objects.create_user(
            username="vrepvend",
            password="x",
            phone="9855555502",
            name="Seller",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Rep Store",
            store_slug="rep-store-rpt",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.vendor_token = Token.objects.create(user=self.vendor_user)
        self.cat = Category.objects.create(name="RCat", slug="r-cat-rpt")
        self.product = Product.objects.create(
            name="RProduct",
            sku="SKU-RPT-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("50.00"),
            stock=100,
            status=Product.Status.ACTIVE,
        )
        self.order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("100.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            is_pos_order=False,
        )
        OrderItem.objects.create(
            order=self.order,
            product=self.product,
            quantity=2,
            unit_price=Decimal("50.00"),
            total_price=Decimal("100.00"),
        )

    def test_summary_includes_relational_breakdowns(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.vendor_token.key}")
        r = self.client.get("/api/vendor/reports/summary/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        body = r.data
        self.assertIn("summary_counts", body)
        self.assertIn("by_placed_portal", body)
        self.assertIn("by_channel", body)
        self.assertIn("by_payment_method", body)
        self.assertIn("top_products", body)
        sc = body["summary_counts"]
        self.assertEqual(sc["order_count"], 1)
        self.assertEqual(sc["items_sold"], 2)
        self.assertAlmostEqual(sc["avg_order_value"], 100.0, places=2)
        portals = body["by_placed_portal"]
        self.assertTrue(any(p["key"] == "portal_main" for p in portals))
        channels = {c["channel"]: c for c in body["by_channel"]}
        self.assertIn("online", channels)
        self.assertEqual(len(body["top_products"]), 1)
        self.assertEqual(body["top_products"][0]["name"], "RProduct")

    def test_export_csv_includes_portal_pos_payment_columns(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.vendor_token.key}")
        r = self.client.get("/api/vendor/reports/export.csv")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        text = r.content.decode("utf-8")
        lines = text.strip().splitlines()
        header = lines[0]
        self.assertIn("order_number", header)
        self.assertIn("placed_portal", header)
        self.assertIn("is_pos_order", header)
        self.assertIn("payment_method", header)

    def test_summary_403_without_vendor_profile(self):
        lone = User.objects.create_user(
            username="vrepnorv",
            password="x",
            phone="9855555503",
            name="No Vendor",
            role=User.Role.NORMAL,
        )
        tok = Token.objects.create(user=lone)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.get("/api/vendor/reports/summary/")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
