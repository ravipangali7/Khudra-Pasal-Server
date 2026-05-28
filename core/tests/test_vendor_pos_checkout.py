"""Vendor POS checkout: single stock deduction per line (OrderItem signal)."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Category, Product, User, Vendor


class VendorPosCheckoutStockTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.vendor_user = User.objects.create_user(
            username="posvend",
            password="x",
            phone="9855555601",
            name="POS Seller",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="POS Store",
            store_slug="pos-store-chk",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.token = Token.objects.create(user=self.vendor_user)
        self.cat = Category.objects.create(name="PosCat", slug="pos-cat-chk")
        self.product = Product.objects.create(
            name="POS Widget",
            sku="SKU-POS-CHK-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("25.00"),
            stock=10,
            status=Product.Status.ACTIVE,
        )
        self.parent_customer = User.objects.create_user(
            username="posvend_parent",
            password="x",
            phone="9855555603",
            name="Vendor POS Parent",
            role=User.Role.PARENT,
        )
        self.child_customer = User.objects.create_user(
            username="posvend_child",
            password="x",
            phone="9855555604",
            name="Vendor POS Child",
            role=User.Role.CHILD,
        )

    def test_pos_checkout_deducts_stock_once(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        initial = self.product.stock
        qty = 3
        r = self.client.post(
            "/api/vendor/pos/checkout/",
            {
                "items": [{"product_id": self.product.pk, "quantity": qty}],
                "payment_method": "cash",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, initial - qty)

    def test_pos_checkout_deducts_digital_stock(self):
        digital = Product.objects.create(
            name="POS Digital",
            sku="SKU-POS-DIG-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("15.00"),
            stock=20,
            type=Product.Type.DIGITAL,
            status=Product.Status.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        qty = 4
        r = self.client.post(
            "/api/vendor/pos/checkout/",
            {
                "items": [{"product_id": digital.pk, "quantity": qty}],
                "payment_method": "cash",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        digital.refresh_from_db()
        self.assertEqual(digital.stock, 20 - qty)

    def test_pos_checkout_deducts_stock_for_parent_and_child_accounts(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        parent_qty = 2
        child_qty = 3
        parent_resp = self.client.post(
            "/api/vendor/pos/checkout/",
            {
                "customer_id": str(self.parent_customer.pk),
                "items": [{"product_id": self.product.pk, "quantity": parent_qty}],
                "payment_method": "cash",
            },
            format="json",
        )
        self.assertEqual(parent_resp.status_code, status.HTTP_201_CREATED, parent_resp.content)
        child_resp = self.client.post(
            "/api/vendor/pos/checkout/",
            {
                "customer_id": str(self.child_customer.pk),
                "items": [{"product_id": self.product.pk, "quantity": child_qty}],
                "payment_method": "cash",
            },
            format="json",
        )
        self.assertEqual(child_resp.status_code, status.HTTP_201_CREATED, child_resp.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10 - parent_qty - child_qty)


class VendorProductPatchStatusTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.vendor_user = User.objects.create_user(
            username="patchvend",
            password="x",
            phone="9855555602",
            name="Patch Seller",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Patch Store",
            store_slug="patch-store-st",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.token = Token.objects.create(user=self.vendor_user)
        self.cat = Category.objects.create(name="PatchCat", slug="patch-cat-st")
        self.product = Product.objects.create(
            name="Draft Widget",
            sku="SKU-PATCH-ST-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("10.00"),
            stock=5,
            status=Product.Status.DRAFT,
        )

    def test_vendor_cannot_patch_status_to_active(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        r = self.client.patch(
            f"/api/vendor/products/{self.product.pk}/",
            {"status": Product.Status.ACTIVE},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.product.refresh_from_db()
        self.assertEqual(self.product.status, Product.Status.DRAFT)
