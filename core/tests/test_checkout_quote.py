"""POST /portal/orders/checkout-quote/ read-only pricing."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Category, Coupon, FlashDeal, FlashDealProduct, Product, User, Vendor


class CheckoutQuoteApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="quote_u",
            password="x",
            phone="9811111199",
            name="Quote User",
            role=User.Role.NORMAL,
        )
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

        self.cat = Category.objects.create(name="QC", slug="quote-cat")
        vu = User.objects.create_user(
            username="quote_v", password="x", phone="9811111188", name="QV"
        )
        self.vendor = Vendor.objects.create(
            user=vu,
            store_name="QStore",
            store_slug="quote-store",
            status=Vendor.Status.APPROVED,
        )
        self.p1 = Product.objects.create(
            name="PFlash",
            slug="qf1",
            sku="SKU-QF-1",
            price=Decimal("100.00"),
            category=self.cat,
            seller=self.vendor,
            stock=10,
            status=Product.Status.ACTIVE,
        )
        self.p2 = Product.objects.create(
            name="PNormal",
            slug="qf2",
            sku="SKU-QF-2",
            price=Decimal("40.00"),
            category=self.cat,
            seller=self.vendor,
            stock=10,
            status=Product.Status.ACTIVE,
        )
        now = timezone.now()
        deal = FlashDeal.objects.create(
            name="QD",
            discount_percent=Decimal("10.00"),
            start_at=now - timedelta(days=1),
            end_at=now + timedelta(days=1),
            status=FlashDeal.Status.ACTIVE,
            priority=1,
            vendor=None,
        )
        FlashDealProduct.objects.create(
            flash_deal=deal,
            product=self.p1,
            override_price=Decimal("80.00"),
        )
        Coupon.objects.create(
            code="QUOTE10",
            type=Coupon.Type.PERCENTAGE,
            value=Decimal("10.00"),
            min_order=Decimal("0.00"),
            status=Coupon.Status.ACTIVE,
        )

    def _body(self, **extra):
        b = {
            "items": [
                {"product_id": self.p1.pk, "quantity": 1},
                {"product_id": self.p2.pk, "quantity": 1},
            ],
            "want_delivery": False,
            **extra,
        }
        return b

    def test_quote_subtotal_and_flash_ids(self):
        r = self.client.post(
            "/api/portal/orders/checkout-quote/",
            self._body(),
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["subtotal"], 120.0)  # 80 + 40
        self.assertEqual(r.data["list_subtotal"], 140.0)
        self.assertEqual(r.data["savings_vs_list"], 20.0)
        self.assertEqual(r.data["savings_flash"], 20.0)
        self.assertIn(self.p1.pk, r.data["flash_product_ids"])
        self.assertEqual(r.data["delivery_fee"], 0.0)
        self.assertEqual(r.data["coupon_discount"], 0.0)
        self.assertIsNone(r.data["coupon_error"])
        self.assertIsNone(r.data["coupon_applied"])
        self.assertEqual(len(r.data["lines"]), 2)

    def test_quote_coupon_stacks_with_flash_lines(self):
        r = self.client.post(
            "/api/portal/orders/checkout-quote/",
            self._body(coupon_code="QUOTE10"),
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["coupon_discount"], 12.0)  # 10% of (80 + 40)
        self.assertEqual(r.data["eligible_subtotal"], 120.0)
        self.assertEqual(r.data["savings_flash"], 20.0)  # (100 list − 80 flash) × 1
        self.assertEqual(r.data["total"], 108.0)
        self.assertEqual(r.data["coupon_applied"]["type"], Coupon.Type.PERCENTAGE)
        self.assertEqual(r.data["coupon_applied"]["value"], 10.0)
        self.assertEqual(len(r.data["lines"]), 2)
        line_p1 = next(x for x in r.data["lines"] if x["product_id"] == self.p1.pk)
        self.assertEqual(line_p1["coupon_discount"], 8.0)
        self.assertEqual(line_p1["line_total"], 72.0)

    def test_quote_invalid_coupon_returns_error_not_400(self):
        r = self.client.post(
            "/api/portal/orders/checkout-quote/",
            self._body(coupon_code="NOSUCH"),
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertIsNotNone(r.data["coupon_error"])
        self.assertEqual(r.data["coupon_discount"], 0.0)
        self.assertIsNone(r.data["coupon_applied"])
        self.assertEqual(r.data["total"], 120.0)

    def test_quote_accepts_product_id_camel_case(self):
        r = self.client.post(
            "/api/portal/orders/checkout-quote/",
            {
                "items": [{"productId": self.p1.pk, "quantity": 1}],
                "want_delivery": False,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data["subtotal"], 80.0)

    def test_quote_stock_warning_without_blocking(self):
        # Keep product ACTIVE (stock=0 flips status to out_of_stock via sync).
        self.p2.stock = 1
        self.p2.save(update_fields=["stock"])
        r = self.client.post(
            "/api/portal/orders/checkout-quote/",
            {
                "items": [
                    {"product_id": self.p1.pk, "quantity": 1},
                    {"product_id": self.p2.pk, "quantity": 2},
                ],
                "want_delivery": False,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(len(r.data["stock_warnings"]), 1)
        self.assertEqual(r.data["stock_warnings"][0]["product_id"], self.p2.pk)
        self.assertEqual(r.data["stock_warnings"][0]["requested"], 2)
        self.assertEqual(r.data["stock_warnings"][0]["available"], 1)
