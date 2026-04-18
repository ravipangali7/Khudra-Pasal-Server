"""Supplier, stock purchase posting, and vendor ledger."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    Category,
    Order,
    OrderCommissionSettlement,
    Product,
    Supplier,
    User,
    Vendor,
    VendorLedgerEntry,
    VendorStockPurchase,
    VendorStockPurchaseLine,
)
from core.services.commission_service import settle_order_commission
from core.views.vendor.vendor_resources import _gen_order_number


class AdminVendorStockPurchaseApiTests(TestCase):
    """Super-admin stock purchase APIs mirror vendor behavior for a chosen vendor."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="admstk",
            password="x",
            phone="9855555699",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.admin_token = Token.objects.create(user=self.admin)
        self.vendor_user = User.objects.create_user(
            username="vadmstk",
            password="x",
            phone="9855555698",
            name="Seller",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Adm Stk Store",
            store_slug="adm-stk-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.cat = Category.objects.create(name="AdmCat", slug="adm-cat-stk")
        self.product = Product.objects.create(
            name="AdmStkProd",
            sku="SKU-ADM-STK",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("100.00"),
            stock=50,
            status=Product.Status.ACTIVE,
            type=Product.Type.PHYSICAL,
        )
        self.supplier = Supplier.objects.create(vendor=self.vendor, name="Adm Supplier")

    def test_admin_post_purchase_increases_stock(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.admin_token.key}")
        base = f"/api/admin/vendors/{self.vendor.pk}/stock-purchases"
        r = self.client.post(
            f"{base}/",
            {
                "supplier_id": self.supplier.pk,
                "tax": "0",
                "lines": [
                    {"product_id": self.product.pk, "quantity": 2, "unit_cost": "15.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        pid = r.data["id"]
        before = Product.objects.get(pk=self.product.pk).stock
        r2 = self.client.post(f"{base}/{pid}/post/", {}, format="json")
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(Product.objects.get(pk=self.product.pk).stock, before + 2)


class VendorInventoryApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.vendor_user = User.objects.create_user(
            username="vinvend",
            password="x",
            phone="9855555601",
            name="Seller",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Inv Store",
            store_slug="inv-store-x",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.token = Token.objects.create(user=self.vendor_user)
        self.cat = Category.objects.create(name="ICat", slug="i-cat-x")
        self.product = Product.objects.create(
            name="InvProd",
            sku="SKU-INV-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("100.00"),
            stock=50,
            status=Product.Status.ACTIVE,
            type=Product.Type.PHYSICAL,
        )
        self.supplier = Supplier.objects.create(vendor=self.vendor, name="Wholesale Co")

    def test_cannot_add_other_vendor_product_line(self):
        other = User.objects.create_user(
            username="otherv",
            password="x",
            phone="9855555602",
            name="O",
            role=User.Role.NORMAL,
        )
        v2 = Vendor.objects.create(
            user=other,
            store_name="Other",
            store_slug="other-inv-x",
            status=Vendor.Status.APPROVED,
        )
        other_p = Product.objects.create(
            name="OtherP",
            sku="SKU-OTH",
            category=self.cat,
            seller=v2,
            price=Decimal("1.00"),
            stock=1,
            status=Product.Status.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        r = self.client.post(
            "/api/vendor/stock-purchases/",
            {
                "supplier_id": self.supplier.pk,
                "lines": [
                    {"product_id": other_p.pk, "quantity": 1, "unit_cost": "10.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_post_purchase_increases_stock_and_ledger(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        r = self.client.post(
            "/api/vendor/stock-purchases/",
            {
                "supplier_id": self.supplier.pk,
                "tax": "0",
                "lines": [
                    {"product_id": self.product.pk, "quantity": 3, "unit_cost": "20.00"},
                ],
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        pid = r.data["id"]
        before = Product.objects.get(pk=self.product.pk).stock
        r2 = self.client.post(f"/api/vendor/stock-purchases/{pid}/post/", {}, format="json")
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(Product.objects.get(pk=self.product.pk).stock, before + 3)
        le = VendorLedgerEntry.objects.filter(
            vendor=self.vendor,
            entry_type=VendorLedgerEntry.EntryType.PURCHASE_COST,
            reference_type="VendorStockPurchase",
            reference_id=str(pid),
        )
        self.assertEqual(le.count(), 1)
        self.assertEqual(le.first().amount, Decimal("-60.00"))

    def test_ledger_sale_idempotent_with_settlement(self):
        customer = User.objects.create_user(
            username="custinv",
            password="x",
            phone="9855555603",
            name="C",
            role=User.Role.NORMAL,
        )
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("100.00"),
            is_pos_order=False,
        )
        settle_order_commission(order)
        self.assertTrue(OrderCommissionSettlement.objects.filter(order=order).exists())
        from core.services.ledger_service import record_sale_on_payment

        record_sale_on_payment(order)
        record_sale_on_payment(order)
        le = VendorLedgerEntry.objects.filter(
            vendor=self.vendor,
            entry_type=VendorLedgerEntry.EntryType.SALE_SETTLEMENT,
            reference_type="Order",
            reference_id=str(order.pk),
        )
        self.assertEqual(le.count(), 1)

    def test_vendor_ledger_manual_adjustment_post(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        r = self.client.post(
            "/api/vendor/ledger/",
            {"amount": "25.50", "description": "Opening balance fix", "direction": "credit"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data["entry_type"], "adjustment")
        self.assertEqual(float(r.data["amount"]), 25.5)
        le = VendorLedgerEntry.objects.filter(
            vendor=self.vendor,
            entry_type=VendorLedgerEntry.EntryType.ADJUSTMENT,
        )
        self.assertEqual(le.count(), 1)

        r2 = self.client.get("/api/vendor/ledger/", format="json")
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(r2.data.get("results", r2.data)), 1)

    def test_vendor_ledger_post_debit_negative_amount(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        r = self.client.post(
            "/api/vendor/ledger/",
            {"amount": "10", "description": "Fee", "direction": "debit"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(float(r.data["amount"]), -10.0)

    def test_vendor_ledger_get_includes_totals(self):
        VendorLedgerEntry.objects.create(
            vendor=self.vendor,
            entry_type=VendorLedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("100.00"),
            reference_type="",
            reference_id="",
            description="Credit adj",
        )
        VendorLedgerEntry.objects.create(
            vendor=self.vendor,
            entry_type=VendorLedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("-30.00"),
            reference_type="",
            reference_id="",
            description="Debit adj",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        r = self.client.get("/api/vendor/ledger/", {"page_size": 10}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("ledger_totals", r.data)
        self.assertEqual(r.data["ledger_totals"]["credit"], 100.0)
        self.assertEqual(r.data["ledger_totals"]["debit"], 30.0)
        self.assertEqual(r.data["ledger_totals"]["balance"], 70.0)


class AdminVendorLedgerAllApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="admledger",
            password="x",
            phone="9855555611",
            name="Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.admin_token = Token.objects.create(user=self.admin)
        self.vendor_user = User.objects.create_user(
            username="vledgerall",
            password="x",
            phone="9855555612",
            name="Seller",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Ledger All Store",
            store_slug="ledger-all-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )

    def test_admin_vendor_ledger_all_totals_and_store_name(self):
        VendorLedgerEntry.objects.create(
            vendor=self.vendor,
            entry_type=VendorLedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("100.00"),
            reference_type="",
            reference_id="",
            description="Credit adj",
        )
        VendorLedgerEntry.objects.create(
            vendor=self.vendor,
            entry_type=VendorLedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("-30.00"),
            reference_type="",
            reference_id="",
            description="Debit adj",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.admin_token.key}")
        r = self.client.get("/api/admin/vendors/all/ledger/", {"page_size": 10}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("ledger_totals", r.data)
        self.assertEqual(r.data["ledger_totals"]["credit"], 100.0)
        self.assertEqual(r.data["ledger_totals"]["debit"], 30.0)
        self.assertEqual(r.data["ledger_totals"]["balance"], 70.0)
        rows = r.data.get("results", [])
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["vendor_name"], "Ledger All Store")
