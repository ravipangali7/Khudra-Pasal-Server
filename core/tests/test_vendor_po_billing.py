from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Category, Product, PurchaseOrder, User, Vendor
from core.services.pos_order_service import create_pos_order
from core.views.vendor.common import get_or_create_pos_walkin_user


class VendorPoBillingEndpointsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.vendor_user = User.objects.create_user(
            username="vendor_po_owner",
            password="x",
            phone="9855555610",
            name="PO Owner",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="PO Owner Store",
            store_slug="po-owner-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.token = Token.objects.create(user=self.vendor_user)

        self.other_user = User.objects.create_user(
            username="vendor_po_other",
            password="x",
            phone="9855555611",
            name="PO Other",
            role=User.Role.NORMAL,
        )
        self.other_vendor = Vendor.objects.create(
            user=self.other_user,
            store_name="PO Other Store",
            store_slug="po-other-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )

        self.cat = Category.objects.create(name="VendorPOCat", slug="vendor-po-cat")
        self.product = Product.objects.create(
            name="Vendor PO Widget",
            sku="SKU-VENDOR-PO-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("25.00"),
            stock=20,
            status=Product.Status.ACTIVE,
        )
        self.other_product = Product.objects.create(
            name="Vendor PO Other Widget",
            sku="SKU-VENDOR-PO-2",
            category=self.cat,
            seller=self.other_vendor,
            price=Decimal("25.00"),
            stock=20,
            status=Product.Status.ACTIVE,
        )

    def test_vendor_purchase_orders_list_and_details_only_include_own_rows(self):
        own_po = PurchaseOrder.objects.create(
            po_number="PO-VEND-OWN-1",
            seller=self.vendor,
            subtotal=Decimal("50.00"),
            tax=Decimal("0"),
            discount=Decimal("0"),
            delivery_fee=Decimal("0"),
            total=Decimal("50.00"),
            payment_method=PurchaseOrder.PaymentMethod.CASH,
            status=PurchaseOrder.Status.COMPLETED,
        )
        other_po = PurchaseOrder.objects.create(
            po_number="PO-VEND-OTHER-1",
            seller=self.other_vendor,
            subtotal=Decimal("10.00"),
            tax=Decimal("0"),
            discount=Decimal("0"),
            delivery_fee=Decimal("0"),
            total=Decimal("10.00"),
            payment_method=PurchaseOrder.PaymentMethod.CASH,
            status=PurchaseOrder.Status.COMPLETED,
        )
        own_pos = create_pos_order(
            acting_vendor=self.vendor,
            customer=get_or_create_pos_walkin_user(),
            items=[{"product_id": self.product.pk, "quantity": 1}],
            payment_method="cash",
            tax_percent=Decimal("0"),
            discount=Decimal("0"),
            notes="",
        )
        other_pos = create_pos_order(
            acting_vendor=self.other_vendor,
            customer=get_or_create_pos_walkin_user(),
            items=[{"product_id": self.other_product.pk, "quantity": 1}],
            payment_method="cash",
            tax_percent=Decimal("0"),
            discount=Decimal("0"),
            notes="",
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        list_resp = self.client.get("/api/vendor/purchase-orders/", {"page_size": 50})
        self.assertEqual(list_resp.status_code, status.HTTP_200_OK, list_resp.content)
        ids = {row["id"] for row in list_resp.data["results"]}
        self.assertIn(own_po.po_number, ids)
        self.assertIn(own_pos.order_number, ids)
        self.assertNotIn(other_po.po_number, ids)
        self.assertNotIn(other_pos.order_number, ids)

        po_detail = self.client.get(f"/api/vendor/purchase-orders/{own_po.pk}/")
        self.assertEqual(po_detail.status_code, status.HTTP_200_OK, po_detail.content)
        self.assertEqual(po_detail.data["id"], own_po.po_number)

        pos_detail = self.client.get(f"/api/vendor/purchase-orders/pos-orders/{own_pos.pk}/")
        self.assertEqual(pos_detail.status_code, status.HTTP_200_OK, pos_detail.content)
        self.assertEqual(pos_detail.data["id"], own_pos.order_number)

        po_forbidden = self.client.get(f"/api/vendor/purchase-orders/{other_po.pk}/")
        self.assertEqual(po_forbidden.status_code, status.HTTP_404_NOT_FOUND)
        pos_forbidden = self.client.get(f"/api/vendor/purchase-orders/pos-orders/{other_pos.pk}/")
        self.assertEqual(pos_forbidden.status_code, status.HTTP_404_NOT_FOUND)
