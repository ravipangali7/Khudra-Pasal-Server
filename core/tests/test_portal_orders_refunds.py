"""Portal-scoped orders list and refund request / execute flows."""

from decimal import Decimal
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    Category,
    EmployeeProfile,
    FamilyGroup,
    Order,
    OrderSettings,
    Product,
    Refund,
    Role,
    User,
    Vendor,
    Wallet,
    WalletSettings,
)
from core.services.base import get_or_create_personal_wallet
from core.services.order_service import pay_with_wallet
from core.services.refund_service import execute_refund, refund_financials
from core.views.vendor.vendor_resources import _gen_order_number


def _wallet_refund_fee_percentage(pct: Decimal) -> None:
    ws = WalletSettings.load()
    ws.transaction_fee_type = WalletSettings.FeeType.PERCENTAGE
    ws.transaction_fee_value = pct
    ws.save(update_fields=["transaction_fee_type", "transaction_fee_value"])


class PortalOrdersSurfaceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="surfu1",
            password="x",
            phone="9811111111",
            name="Surf User",
            role=User.Role.NORMAL,
        )
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

        self.vendor_user = User.objects.create_user(
            username="surfvend",
            password="x",
            phone="9822222222",
            name="V",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Surf Store",
            status=Vendor.Status.APPROVED,
        )
        self.cat = Category.objects.create(name="C1", slug="c1-surf")
        self.product = Product.objects.create(
            name="P1",
            sku="SKU-SURF-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("100.00"),
            stock=50,
            status=Product.Status.ACTIVE,
        )

    def _create_order(self, *, placed_portal: str | None, total: Decimal = Decimal("100.00")):
        return Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=total,
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=total,
            placed_portal=placed_portal,
        )

    def test_main_list_includes_legacy_null_portal(self):
        self._create_order(placed_portal=None)
        r = self.client.get("/api/portal/orders/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["results"]), 1)

    def test_main_list_excludes_family_only_orders(self):
        self._create_order(placed_portal=Order.PlacedPortal.PORTAL_FAMILY)
        r = self.client.get("/api/portal/orders/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["results"]), 0)

    def test_main_order_detail_matches_list_shape(self):
        order = self._create_order(placed_portal=None)
        r = self.client.get(f"/api/portal/orders/{order.pk}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["pk"], order.pk)
        self.assertEqual(r.data["id"], order.order_number)
        self.assertIn("lines", r.data)

    def test_main_order_detail_404_for_family_surface_order(self):
        order = self._create_order(placed_portal=Order.PlacedPortal.PORTAL_FAMILY)
        r = self.client.get(f"/api/portal/orders/{order.pk}/")
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

    def test_family_list_only_family_orders(self):
        self._create_order(placed_portal=Order.PlacedPortal.PORTAL_FAMILY)
        self._create_order(placed_portal=None)
        self.user.role = User.Role.PARENT
        self.user.save()
        FamilyGroup.objects.create(
            name="G1",
            leader=self.user,
            status=FamilyGroup.Status.ACTIVE,
        )
        r = self.client.get("/api/family-portal/orders/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["results"]), 1)

    def test_orders_list_hides_refund_when_refund_validity_expired(self):
        order = self._create_order(
            placed_portal=Order.PlacedPortal.PORTAL_MAIN, total=Decimal("120.00")
        )
        order.payment_status = Order.PaymentStatus.PAID
        order.payment_method = Order.PaymentMethod.WALLET
        order.created_at = timezone.now() - timedelta(days=10)
        order.save(update_fields=["payment_status", "payment_method", "created_at"])
        settings = OrderSettings.load()
        settings.refund_validity_days = 7
        settings.save()

        r = self.client.get("/api/portal/orders/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r.data["results"]), 1)
        row = r.data["results"][0]
        self.assertFalse(row.get("refund_allowed"))
        self.assertIsNone(row.get("refund_estimate"))


class RefundExecuteWalletTests(TestCase):
    def setUp(self):
        _wallet_refund_fee_percentage(Decimal("3.00"))
        self.user = User.objects.create_user(
            username="refwu1",
            password="x",
            phone="9833333333",
            name="Ref Wallet",
            role=User.Role.NORMAL,
        )
        self.wallet = get_or_create_personal_wallet(self.user)
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("500.00"))
        self.vendor_user = User.objects.create_user(
            username="refwv",
            password="x",
            phone="9844444444",
            name="V2",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Ref Store",
            status=Vendor.Status.APPROVED,
        )
        self.cat = Category.objects.create(name="C2", slug="c2-ref")
        self.product = Product.objects.create(
            name="P2",
            sku="SKU-REF-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("80.00"),
            stock=10,
            status=Product.Status.ACTIVE,
        )

    def test_partial_approved_refund_does_not_mark_order_refunded(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("80.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("80.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            payment_wallet=self.wallet,
        )
        pay_with_wallet(order, self.wallet, fund_source="Personal wallet")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)

        gross = Decimal("30.00")
        fin = refund_financials(order, gross, persist_settlement=True)
        self.assertEqual(fin.fee_retained, Decimal("0.09"))
        self.assertEqual(fin.customer_credit, Decimal("29.91"))

        rf = Refund.objects.create(
            refund_number="RF-TEST-PARTIAL-1",
            order=order,
            customer=self.user,
            amount=gross,
            platform_fee_amount=fin.fee_retained,
            net_credit_amount=fin.customer_credit,
            reason="partial",
            status=Refund.Status.APPROVED,
        )
        execute_refund(rf)
        order.refresh_from_db()
        self.assertNotEqual(order.status, Order.Status.REFUNDED)
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        bal = Wallet.objects.get(pk=self.wallet.pk).balance
        self.assertEqual(bal, Decimal("449.91"))

    def test_execute_refund_idempotent(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("80.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("80.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            payment_wallet=self.wallet,
        )
        pay_with_wallet(order, self.wallet, fund_source="Personal wallet")
        gross = Decimal("10.00")
        fin = refund_financials(order, gross, persist_settlement=True)
        rf = Refund.objects.create(
            refund_number="RF-TEST-IDEM",
            order=order,
            customer=self.user,
            amount=gross,
            platform_fee_amount=fin.fee_retained,
            net_credit_amount=fin.customer_credit,
            reason="idem",
            status=Refund.Status.APPROVED,
        )
        execute_refund(rf)
        b1 = Wallet.objects.get(pk=self.wallet.pk).balance
        execute_refund(rf)
        b2 = Wallet.objects.get(pk=self.wallet.pk).balance
        self.assertEqual(b1, b2)

    def test_execute_refund_insufficient_vendor_balance_raises(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("80.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("80.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            payment_wallet=self.wallet,
        )
        pay_with_wallet(order, self.wallet, fund_source="Personal wallet")
        from core.models import Wallet as W

        vw = W.objects.get(vendor=self.vendor)
        W.objects.filter(pk=vw.pk).update(balance=Decimal("0.00"))

        gross = Decimal("80.00")
        fin = refund_financials(order, gross, persist_settlement=True)
        rf = Refund.objects.create(
            refund_number="RF-TEST-INSUF",
            order=order,
            customer=self.user,
            amount=gross,
            platform_fee_amount=fin.fee_retained,
            net_credit_amount=fin.customer_credit,
            reason="full",
            status=Refund.Status.APPROVED,
        )
        with self.assertRaises(ValueError) as ctx:
            execute_refund(rf)
        self.assertIn("vendor", str(ctx.exception).lower())


class RefundCommissionExample200Tests(TestCase):
    """Doc example: order 200, vendor 195, commission 5, customer receives 199.85."""

    def setUp(self):
        _wallet_refund_fee_percentage(Decimal("3.00"))
        self.user = User.objects.create_user(
            username="ref200u",
            password="x",
            phone="9810101010",
            name="C200",
            role=User.Role.NORMAL,
        )
        self.wallet = get_or_create_personal_wallet(self.user)
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("1000.00"))
        self.vendor_user = User.objects.create_user(
            username="ref200v",
            password="x",
            phone="9820202020",
            name="V200",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Ex Store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("2.50"),
        )
        self.cat = Category.objects.create(name="C200", slug="c200")
        self.product = Product.objects.create(
            name="P200",
            sku="SKU-200",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("200.00"),
            stock=5,
            status=Product.Status.ACTIVE,
        )

    def test_full_refund_customer_receives_199_85(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("200.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("200.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            payment_wallet=self.wallet,
        )
        pay_with_wallet(order, self.wallet, fund_source="Personal wallet")
        order.refresh_from_db()
        gross = Decimal("200.00")
        fin = refund_financials(order, gross, persist_settlement=True)
        self.assertEqual(fin.fee_retained, Decimal("0.15"))
        self.assertEqual(fin.customer_credit, Decimal("199.85"))
        self.assertEqual(fin.vendor_claw, Decimal("195.00"))
        self.assertEqual(fin.platform_debit, Decimal("4.85"))

        rf = Refund.objects.create(
            refund_number="RF-TEST-200",
            order=order,
            customer=self.user,
            amount=gross,
            platform_fee_amount=fin.fee_retained,
            net_credit_amount=fin.customer_credit,
            reason="full",
            status=Refund.Status.APPROVED,
        )
        execute_refund(rf)
        bal = Wallet.objects.get(pk=self.wallet.pk).balance
        self.assertEqual(bal, Decimal("1000.00") - Decimal("200.00") + Decimal("199.85"))


class RefundRetentionOnCommissionSliceOnlyTests(TestCase):
    """Wallet retention % applies only to the proportional platform commission slice (not order gross)."""

    def setUp(self):
        _wallet_refund_fee_percentage(Decimal("5.00"))

    def test_full_refund_170_ten_percent_commission_five_percent_retention(self):
        user = User.objects.create_user(
            username="ref170u",
            password="x",
            phone="9817171717",
            name="C170",
            role=User.Role.NORMAL,
        )
        wallet = get_or_create_personal_wallet(user)
        Wallet.objects.filter(pk=wallet.pk).update(balance=Decimal("500.00"))
        vendor_user = User.objects.create_user(
            username="ref170v",
            password="x",
            phone="9827272727",
            name="V170",
            role=User.Role.NORMAL,
        )
        vendor = Vendor.objects.create(
            user=vendor_user,
            store_name="Store170",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        cat = Category.objects.create(name="C170", slug="c170")
        Product.objects.create(
            name="P170",
            sku="SKU-170",
            category=cat,
            seller=vendor,
            price=Decimal("170.00"),
            stock=5,
            status=Product.Status.ACTIVE,
        )
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=user,
            seller=vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("170.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("170.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            payment_wallet=wallet,
        )
        pay_with_wallet(order, wallet, fund_source="Personal wallet")
        order.refresh_from_db()
        fin = refund_financials(order, Decimal("170.00"), persist_settlement=True)
        # 10% of 170 = 17 commission slice; 5% retention on that slice only = 0.85
        self.assertEqual(fin.fee_retained, Decimal("0.85"))
        self.assertEqual(fin.platform_debit, Decimal("16.15"))
        self.assertEqual(fin.vendor_claw, Decimal("153.00"))
        self.assertEqual(fin.customer_credit, Decimal("169.15"))


class RefundSuperAdminPatchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.super_admin = User.objects.create_user(
            username="refund_sa",
            password=self.pw,
            phone="9855555555",
            name="SA",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.staff = User.objects.create_user(
            username="refund_staff",
            password=self.pw,
            phone="9866666666",
            name="St",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        self.emp_role = Role.objects.create(
            name="Refund Test Emp Role",
            permissions={"settings": True},
            status=Role.Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=self.staff,
            role=self.emp_role,
            modules_access=["settings"],
            status=EmployeeProfile.Status.ACTIVE,
        )

        self.customer = User.objects.create_user(
            username="refund_cust",
            password="x",
            phone="9877777777",
            name="C",
            role=User.Role.NORMAL,
        )
        self.vendor_user = User.objects.create_user(
            username="refund_vu",
            password="x",
            phone="9888888888",
            name="VU",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Refund SA Store",
            status=Vendor.Status.APPROVED,
        )
        self.cat = Category.objects.create(name="CRS", slug="crs-ref")
        self.product = Product.objects.create(
            name="PR",
            sku="SKU-RF-SA",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("50.00"),
            stock=10,
            status=Product.Status.ACTIVE,
        )
        self.wallet = get_or_create_personal_wallet(self.customer)
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("200.00"))
        self.order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.customer,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            payment_wallet=self.wallet,
        )
        pay_with_wallet(self.order, self.wallet, fund_source="Personal wallet")
        gross = Decimal("50.00")
        fin = refund_financials(self.order, gross, persist_settlement=True)
        self.rf = Refund.objects.create(
            refund_number="RF-SA-PATCH-1",
            order=self.order,
            customer=self.customer,
            amount=gross,
            platform_fee_amount=fin.fee_retained,
            net_credit_amount=fin.customer_credit,
            reason="test",
            status=Refund.Status.PENDING,
        )

    def _admin_token(self, user: User) -> str:
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def test_staff_cannot_patch_refund_approve(self):
        tok = self._admin_token(self.staff)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.patch(
            f"/api/admin/refunds/{self.rf.refund_number}/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN, r.content)

    def test_super_admin_can_patch_refund_approve(self):
        tok = self._admin_token(self.super_admin)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.patch(
            f"/api/admin/refunds/{self.rf.refund_number}/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        self.rf.refresh_from_db()
        self.assertEqual(self.rf.status, Refund.Status.APPROVED)
        self.assertIsNotNone(self.rf.processed_at)


class RefundCommissionSliceRespectsWalletFlatFeeTests(TestCase):
    """Retention on the commission slice follows WalletSettings (flat), not a hardcoded rate."""

    def test_full_refund_uses_wallet_flat_on_commission_slice(self):
        ws = WalletSettings.load()
        ws.transaction_fee_type = WalletSettings.FeeType.FLAT
        ws.transaction_fee_value = Decimal("2.00")
        ws.save(update_fields=["transaction_fee_type", "transaction_fee_value"])

        user = User.objects.create_user(
            username="refflat_u",
            password="x",
            phone="9870101010",
            name="Flat U",
            role=User.Role.NORMAL,
        )
        wallet = get_or_create_personal_wallet(user)
        Wallet.objects.filter(pk=wallet.pk).update(balance=Decimal("1000.00"))
        vendor_user = User.objects.create_user(
            username="refflat_v",
            password="x",
            phone="9870202020",
            name="Flat V",
            role=User.Role.NORMAL,
        )
        vendor = Vendor.objects.create(
            user=vendor_user,
            store_name="Flat Store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("2.50"),
        )
        cat = Category.objects.create(name="CFlat", slug="cflat")
        Product.objects.create(
            name="PFlat",
            sku="SKU-FLAT",
            category=cat,
            seller=vendor,
            price=Decimal("200.00"),
            stock=5,
            status=Product.Status.ACTIVE,
        )
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=user,
            seller=vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("200.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("200.00"),
            placed_portal=Order.PlacedPortal.PORTAL_MAIN,
            payment_wallet=wallet,
        )
        pay_with_wallet(order, wallet, fund_source="Personal wallet")
        order.refresh_from_db()
        gross = Decimal("200.00")
        fin = refund_financials(order, gross, persist_settlement=True)
        self.assertEqual(fin.fee_retained, Decimal("2.00"))
        self.assertEqual(fin.platform_debit, Decimal("3.00"))
        self.assertEqual(fin.customer_credit, Decimal("198.00"))
