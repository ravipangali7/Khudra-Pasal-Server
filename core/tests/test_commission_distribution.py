"""Commission settlement on paid orders and multi-vendor checkout."""

from decimal import Decimal
from io import BytesIO
from uuid import uuid4

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import (
    Category,
    Order,
    OrderCommissionSettlement,
    PaymentTransaction,
    Product,
    User,
    Vendor,
    Wallet,
    WalletTransaction,
)
from core.services import commission_service, wallet_service
from core.services.base import get_or_create_personal_wallet
from core.services.vendor_service import ensure_vendor_wallet
from rest_framework.authtoken.models import Token


class CommissionSettlementTests(TestCase):
    def setUp(self):
        self.vendor_user = User.objects.create_user(
            username="vcomm1",
            password="x",
            phone="9810101010",
            name="Vendor",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Store A",
            store_slug="store-a-comm",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        ensure_vendor_wallet(self.vendor)
        self.customer = User.objects.create_user(
            username="ccomm1",
            password="x",
            phone="9820202020",
            name="Buyer",
            role=User.Role.NORMAL,
        )

    def test_paid_order_on_create_triggers_settlement(self):
        Order.objects.create(
            order_number="COMM-001",
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("100.00"),
            want_delivery=False,
        )
        self.assertEqual(OrderCommissionSettlement.objects.count(), 1)
        st = OrderCommissionSettlement.objects.first()
        self.assertEqual(st.commission_amount, Decimal("10.00"))
        self.assertEqual(st.vendor_amount, Decimal("90.00"))
        platform = Wallet.objects.get(type=Wallet.Type.PLATFORM)
        self.assertEqual(platform.balance, Decimal("10.00"))
        self.vendor.wallet.refresh_from_db()
        self.assertEqual(self.vendor.wallet.balance, Decimal("90.00"))

    def test_settlement_idempotent(self):
        order = Order.objects.create(
            order_number="COMM-002",
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
            want_delivery=False,
        )
        commission_service.settle_order_commission(order)
        commission_service.settle_order_commission(order)
        self.assertEqual(OrderCommissionSettlement.objects.filter(order=order).count(), 1)

    def test_skip_when_no_seller(self):
        Order.objects.create(
            order_number="COMM-003",
            customer=self.customer,
            seller=None,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("40.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("40.00"),
            want_delivery=False,
        )
        self.assertEqual(OrderCommissionSettlement.objects.count(), 0)
        self.assertFalse(Wallet.objects.filter(type=Wallet.Type.PLATFORM).exists())


def _tiny_png() -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "p.png",
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0b\x13\x00\x00\x0b\x13\x01\x00\x9a\x9c\x18\x00\x00\x00\nIDATx\x9cc\xf8\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82",
        content_type="image/png",
    )


class MultiVendorCheckoutCommissionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.cat = Category.objects.create(name="C1", slug="c1-comm")
        self.vu_a = User.objects.create_user(
            username="va", password=self.pw, phone="9830303030", name="VA"
        )
        self.vu_b = User.objects.create_user(
            username="vb", password=self.pw, phone="9840404040", name="VB"
        )
        self.va = Vendor.objects.create(
            user=self.vu_a,
            store_name="A",
            store_slug="v-a-comm",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.vb = Vendor.objects.create(
            user=self.vu_b,
            store_name="B",
            store_slug="v-b-comm",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("20.00"),
        )
        ensure_vendor_wallet(self.va)
        ensure_vendor_wallet(self.vb)
        img = _tiny_png()
        self.pa = Product.objects.create(
            name="PA",
            slug="pa-comm",
            sku="SKU-A-COMM",
            price=Decimal("100.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=10,
            seller=self.va,
            status=Product.Status.ACTIVE,
        )
        img2 = _tiny_png()
        self.pb = Product.objects.create(
            name="PB",
            slug="pb-comm",
            sku="SKU-B-COMM",
            price=Decimal("200.00"),
            category=self.cat,
            image=img2,
            type=Product.Type.PHYSICAL,
            stock=10,
            seller=self.vb,
            status=Product.Status.ACTIVE,
        )
        self.customer = User.objects.create_user(
            username="mcust",
            password=self.pw,
            phone="9850505050",
            name="MCust",
            role=User.Role.NORMAL,
        )
        Token.objects.get_or_create(user=self.customer)
        w = get_or_create_personal_wallet(self.customer)
        wallet_service.credit_wallet(
            w,
            Decimal("10000.00"),
            wtype=WalletTransaction.Type.CREDIT,
            description="test float",
            reference_type="test",
            reference_id="1",
            performed_by=self.customer,
        )

    def test_wallet_checkout_multi_vendor_two_settlements(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=self.customer).key}"
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            {
                "items": [
                    {"product_id": self.pa.pk, "quantity": 1},
                    {"product_id": self.pb.pk, "quantity": 1},
                ],
                "want_delivery": False,
                "payment_method": "wallet",
                "placed_portal": Order.PlacedPortal.PORTAL_MAIN,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(len(r.data["orders"]), 2)
        self.assertEqual(Order.objects.filter(customer=self.customer).count(), 2)
        self.assertEqual(OrderCommissionSettlement.objects.count(), 2)
        totals = {s.vendor_id: s for s in OrderCommissionSettlement.objects.all()}
        self.assertAlmostEqual(float(totals[self.va.pk].commission_amount), 10.0)
        self.assertAlmostEqual(float(totals[self.va.pk].vendor_amount), 90.0)
        self.assertAlmostEqual(float(totals[self.vb.pk].commission_amount), 40.0)
        self.assertAlmostEqual(float(totals[self.vb.pk].vendor_amount), 160.0)

    def test_wallet_checkout_insufficient_balance(self):
        """Grand total exceeds locked wallet balance: 400, no orders, message for UI."""
        w = get_or_create_personal_wallet(self.customer)
        Wallet.objects.filter(pk=w.pk).update(balance=Decimal("50.00"))
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=self.customer).key}"
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            {
                "items": [
                    {"product_id": self.pa.pk, "quantity": 1},
                    {"product_id": self.pb.pk, "quantity": 1},
                ],
                "want_delivery": False,
                "payment_method": "wallet",
                "placed_portal": Order.PlacedPortal.PORTAL_MAIN,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertEqual(r.data.get("detail"), "Insufficient balance")
        self.assertEqual(Order.objects.filter(customer=self.customer).count(), 0)


class InHousePortalCheckoutTests(TestCase):
    """In-house products (seller=NULL) can checkout; no vendor commission settlement."""

    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.cat = Category.objects.create(name="C-inhouse", slug="c-inhouse")
        self.vu = User.objects.create_user(
            username="vinhouse", password=self.pw, phone="9831313131", name="VIn"
        )
        self.vendor = Vendor.objects.create(
            user=self.vu,
            store_name="VendorMix",
            store_slug="v-inhouse-mix",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        ensure_vendor_wallet(self.vendor)
        img = _tiny_png()
        self.p_vendor = Product.objects.create(
            name="PVendorMix",
            slug="p-vendor-mix",
            sku="SKU-V-MIX",
            price=Decimal("50.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=10,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        img2 = _tiny_png()
        self.p_inhouse = Product.objects.create(
            name="PInHouse",
            slug="p-inhouse",
            sku="SKU-INHOUSE",
            price=Decimal("80.00"),
            category=self.cat,
            image=img2,
            type=Product.Type.PHYSICAL,
            stock=10,
            seller=None,
            status=Product.Status.ACTIVE,
        )
        self.customer = User.objects.create_user(
            username="cust_inhouse",
            password=self.pw,
            phone="9841414141",
            name="CustIn",
            role=User.Role.NORMAL,
        )
        Token.objects.get_or_create(user=self.customer)
        w = get_or_create_personal_wallet(self.customer)
        wallet_service.credit_wallet(
            w,
            Decimal("10000.00"),
            wtype=WalletTransaction.Type.CREDIT,
            description="test float",
            reference_type="test",
            reference_id="inhouse-1",
            performed_by=self.customer,
        )

    def test_wallet_checkout_in_house_only_no_commission_settlement(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=self.customer).key}"
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            {
                "items": [{"product_id": self.p_inhouse.pk, "quantity": 1}],
                "want_delivery": False,
                "payment_method": "wallet",
                "placed_portal": Order.PlacedPortal.PORTAL_MAIN,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(len(r.data["orders"]), 1)
        o = Order.objects.get(customer=self.customer)
        self.assertIsNone(o.seller_id)
        self.assertEqual(o.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(OrderCommissionSettlement.objects.count(), 0)

    def test_wallet_checkout_mixed_vendor_and_in_house_two_orders_one_settlement(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=self.customer).key}"
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            {
                "items": [
                    {"product_id": self.p_vendor.pk, "quantity": 1},
                    {"product_id": self.p_inhouse.pk, "quantity": 1},
                ],
                "want_delivery": False,
                "payment_method": "wallet",
                "placed_portal": Order.PlacedPortal.PORTAL_MAIN,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(len(r.data["orders"]), 2)
        orders = list(Order.objects.filter(customer=self.customer).order_by("seller_id"))
        self.assertEqual(len(orders), 2)
        by_seller = {o.seller_id: o for o in orders}
        self.assertIn(self.vendor.pk, by_seller)
        self.assertIn(None, by_seller)
        self.assertEqual(OrderCommissionSettlement.objects.count(), 1)
        st = OrderCommissionSettlement.objects.first()
        self.assertEqual(st.vendor_id, self.vendor.pk)


class GatewayPaymentCompleteTests(TestCase):
    """Gateway payment/complete flow (orders may be created outside storefront wallet-only checkout)."""

    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.cat = Category.objects.create(name="C2", slug="c2-gw")
        self.vu = User.objects.create_user(
            username="vgw", password=self.pw, phone="9860606060", name="VGW"
        )
        self.vendor = Vendor.objects.create(
            user=self.vu,
            store_name="GW Store",
            store_slug="gw-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        ensure_vendor_wallet(self.vendor)
        img = _tiny_png()
        self.product = Product.objects.create(
            name="PGW",
            slug="pgw",
            sku="SKU-GW",
            price=Decimal("100.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=10,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        self.customer = User.objects.create_user(
            username="cgw",
            password=self.pw,
            phone="9870707070",
            name="CGW",
            role=User.Role.NORMAL,
        )
        Token.objects.get_or_create(user=self.customer)

    def test_portal_checkout_rejects_non_wallet_payment_methods(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=self.customer).key}"
        )
        for method in ("cod", "esewa", "khalti", "ime_pay"):
            r = self.client.post(
                "/api/portal/orders/checkout/",
                {
                    "items": [{"product_id": self.product.pk, "quantity": 1}],
                    "want_delivery": False,
                    "payment_method": method,
                    "placed_portal": Order.PlacedPortal.PORTAL_MAIN,
                },
                format="json",
            )
            self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
            self.assertIn("payment_method", r.data)
        r_missing = self.client.post(
            "/api/portal/orders/checkout/",
            {
                "items": [{"product_id": self.product.pk, "quantity": 1}],
                "want_delivery": False,
                "placed_portal": Order.PlacedPortal.PORTAL_MAIN,
            },
            format="json",
        )
        self.assertEqual(r_missing.status_code, status.HTTP_400_BAD_REQUEST, r_missing.data)

    def _create_pending_gateway_order(self, *, method: str) -> str:
        onum = f"G{uuid4().hex[:15].upper()}"
        o = Order.objects.create(
            order_number=onum,
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=method,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("100.00"),
            want_delivery=False,
        )
        PaymentTransaction.objects.create(
            txn_ref=f"{onum}-{uuid4().hex[:16]}",
            order=o,
            customer=self.customer,
            amount=o.total,
            method=method,
            status=PaymentTransaction.Status.PENDING,
        )
        return onum

    def test_gateway_payment_complete_settles_pending_order(self):
        onum = self._create_pending_gateway_order(method=PaymentTransaction.Method.ESEWA)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=self.customer).key}"
        )
        r2 = self.client.post(
            "/api/portal/orders/payment/complete/",
            {"order_numbers": [onum]},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK, r2.data)
        self.assertIn(onum, r2.data["completed"])
        o = Order.objects.get(order_number=onum)
        self.assertEqual(o.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(OrderCommissionSettlement.objects.count(), 1)
        st = OrderCommissionSettlement.objects.get(order=o)
        self.assertEqual(st.commission_amount, Decimal("10.00"))
        self.assertEqual(st.vendor_amount, Decimal("90.00"))
        self.vendor.wallet.refresh_from_db()
        self.assertEqual(self.vendor.wallet.balance, Decimal("90.00"))

    def test_payment_complete_idempotent(self):
        onum = self._create_pending_gateway_order(method=PaymentTransaction.Method.KHALTI)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=self.customer).key}"
        )
        self.client.post(
            "/api/portal/orders/payment/complete/",
            {"order_numbers": [onum]},
            format="json",
        )
        r3 = self.client.post(
            "/api/portal/orders/payment/complete/",
            {"order_numbers": [onum]},
            format="json",
        )
        self.assertEqual(r3.status_code, status.HTTP_200_OK, r3.data)
        self.assertIn(onum, r3.data["already_paid"])
        self.assertEqual(OrderCommissionSettlement.objects.filter(order__order_number=onum).count(), 1)


class CommissionTotalBaseTests(TestCase):
    """Commission rate applies to the full paid order total (incl. delivery when present)."""

    def setUp(self):
        self.vendor_user = User.objects.create_user(
            username="vsub",
            password="x",
            phone="9880808080",
            name="VSub",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Sub Store",
            store_slug="sub-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        ensure_vendor_wallet(self.vendor)
        self.customer = User.objects.create_user(
            username="csub",
            password="x",
            phone="9890909090",
            name="CSub",
            role=User.Role.NORMAL,
        )

    def test_commission_on_total_includes_delivery(self):
        Order.objects.create(
            order_number="SUB-DEL-1",
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.ESEWA,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("100.00"),
            delivery_fee=Decimal("10.00"),
            discount_amount=Decimal("0"),
            total=Decimal("110.00"),
            want_delivery=True,
        )
        o = Order.objects.get(order_number="SUB-DEL-1")
        st = OrderCommissionSettlement.objects.get(order=o)
        self.assertEqual(st.commission_amount, Decimal("11.00"))
        self.assertEqual(st.vendor_amount, Decimal("99.00"))


class AdminMarkCodPaidTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.cat = Category.objects.create(name="C3", slug="c3-cod")
        self.vu = User.objects.create_user(
            username="vcod", password=self.pw, phone="9801111111", name="VCOD"
        )
        self.vendor = Vendor.objects.create(
            user=self.vu,
            store_name="COD Store",
            store_slug="cod-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("5.00"),
        )
        ensure_vendor_wallet(self.vendor)
        img = _tiny_png()
        self.product = Product.objects.create(
            name="PCOD",
            slug="pcod",
            sku="SKU-COD",
            price=Decimal("200.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=5,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        self.customer = User.objects.create_user(
            username="ccod",
            password=self.pw,
            phone="9802222222",
            name="CCOD",
            role=User.Role.NORMAL,
        )
        Token.objects.get_or_create(user=self.customer)
        self.admin = User.objects.create_user(
            username="adm_cod",
            password=self.pw,
            phone="9803333333",
            name="Admin",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        Token.objects.get_or_create(user=self.admin)

    def test_admin_patch_payment_paid_settles_cod_order(self):
        """COD order created outside wallet checkout; admin marks paid and commission settles."""
        onum = f"C{uuid4().hex[:15].upper()}"
        o = Order.objects.create(
            order_number=onum,
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("200.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("200.00"),
            want_delivery=False,
        )
        self.assertEqual(o.payment_status, Order.PaymentStatus.PENDING)
        self.assertEqual(PaymentTransaction.objects.filter(order=o).count(), 0)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=self.admin).key}"
        )
        pr = self.client.patch(
            f"/api/admin/orders/{o.pk}/",
            {"payment_status": "paid"},
            format="json",
        )
        self.assertEqual(pr.status_code, status.HTTP_200_OK, pr.data)
        o.refresh_from_db()
        self.assertEqual(o.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(OrderCommissionSettlement.objects.filter(order=o).count(), 1)
        st = OrderCommissionSettlement.objects.get(order=o)
        self.assertEqual(st.commission_amount, Decimal("10.00"))
        self.assertEqual(st.vendor_amount, Decimal("190.00"))
