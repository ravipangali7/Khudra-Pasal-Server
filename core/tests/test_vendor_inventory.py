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
