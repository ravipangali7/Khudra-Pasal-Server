"""Admin POS checkout and merged PO Billing list."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Category, Product, PurchaseOrder, User, Vendor
from core.services.pos_order_service import create_pos_order
from core.views.vendor.common import get_or_create_pos_walkin_user


def _png() -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "p.png",
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0b\x13\x00\x00\x0b\x13\x01\x00\x9a\x9c\x18\x00\x00\x00\nIDATx\x9cc\xf8\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82",
        content_type="image/png",
    )


class AdminPosCheckoutStockTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "AdminPosTest123!"
        self.admin = User.objects.create_user(
            username="admin_pos_sa",
            password=self.pw,
            phone="9811111101",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.cat = Category.objects.create(name="AdminPosCat", slug="admin-pos-cat")
        self.product = Product.objects.create(
            name="Admin POS Widget",
            sku="SKU-ADMIN-POS-1",
            category=self.cat,
            image=_png(),
            price=Decimal("30.00"),
            type=Product.Type.PHYSICAL,
            stock=10,
            status=Product.Status.ACTIVE,
        )

    def _token(self) -> str:
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": self.admin.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def test_admin_pos_checkout_deducts_stock(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token()}")
        qty = 4
        r = self.client.post(
            "/api/admin/pos/checkout/",
            {
                "items": [
                    {
                        "product_id": self.product.pk,
                        "quantity": qty,
                        "unit_price": "30.00",
                    }
                ],
                "payment_method": "cash",
                "discount": 0,
                "tax_percent": 0,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 10 - qty)

    def test_admin_pos_checkout_deducts_digital_stock(self):
        digital = Product.objects.create(
            name="Admin POS Digital",
            sku="SKU-ADMIN-POS-DIG-1",
            category=self.cat,
            image=_png(),
            price=Decimal("12.00"),
            type=Product.Type.DIGITAL,
            stock=8,
            status=Product.Status.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token()}")
        qty = 2
        r = self.client.post(
            "/api/admin/pos/checkout/",
            {
                "items": [
                    {
                        "product_id": digital.pk,
                        "quantity": qty,
                        "unit_price": "12.00",
                    }
                ],
                "payment_method": "cash",
                "discount": 0,
                "tax_percent": 0,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        digital.refresh_from_db()
        self.assertEqual(digital.stock, 8 - qty)


class AdminPurchaseOrdersMergedListTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "MergedPoTest123!"
        self.admin = User.objects.create_user(
            username="merged_po_sa",
            password=self.pw,
            phone="9811111102",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.cat = Category.objects.create(name="MergedCat", slug="merged-cat")
        self.vendor_user = User.objects.create_user(
            username="merged_vend",
            password=self.pw,
            phone="9811111103",
            name="Vendor",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Merged Store",
            store_slug="merged-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.product = Product.objects.create(
            name="Merged Widget",
            sku="SKU-MERGED-1",
            category=self.cat,
            seller=self.vendor,
            image=_png(),
            price=Decimal("20.00"),
            type=Product.Type.PHYSICAL,
            stock=5,
            status=Product.Status.ACTIVE,
        )

    def _token(self) -> str:
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": self.admin.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def test_purchase_orders_list_includes_vendor_pos_row(self):
        order = create_pos_order(
            acting_vendor=self.vendor,
            customer=get_or_create_pos_walkin_user(),
            items=[{"product_id": self.product.pk, "quantity": 1}],
            payment_method="cash",
            tax_percent=Decimal("0"),
            discount=Decimal("0"),
            notes="",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token()}")
        r = self.client.get("/api/admin/purchase-orders/", {"page_size": 50})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        results = r.data["results"]
        pos_rows = [row for row in results if row.get("record_type") == "pos_order" and row.get("pk") == order.pk]
        self.assertEqual(len(pos_rows), 1)
        self.assertEqual(pos_rows[0]["seller"], self.vendor.store_name)
        self.assertEqual(pos_rows[0]["id"], order.order_number)

    def test_purchase_orders_list_includes_manual_po_and_pos(self):
        PurchaseOrder.objects.create(
            po_number="PO-TEST-MERGE-1",
            subtotal=Decimal("10.00"),
            tax=Decimal("0"),
            discount=Decimal("0"),
            delivery_fee=Decimal("0"),
            total=Decimal("10.00"),
            payment_method=PurchaseOrder.PaymentMethod.CASH,
            status=PurchaseOrder.Status.COMPLETED,
        )
        order = create_pos_order(
            acting_vendor=None,
            customer=get_or_create_pos_walkin_user(),
            items=[{"product_id": self.product.pk, "quantity": 1}],
            payment_method="cash",
            tax_percent=Decimal("0"),
            discount=Decimal("0"),
            notes="",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token()}")
        r = self.client.get("/api/admin/purchase-orders/", {"page_size": 50})
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        types_found = {row.get("record_type") for row in r.data["results"]}
        self.assertIn("purchase_order", types_found)
        self.assertIn("pos_order", types_found)
        admin_pos = [x for x in r.data["results"] if x.get("record_type") == "pos_order" and x.get("pk") == order.pk]
        self.assertEqual(len(admin_pos), 1)
        self.assertEqual(admin_pos[0]["seller"], "Admin")
