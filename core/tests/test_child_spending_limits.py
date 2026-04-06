"""Child spending limits: non-personal wallet orders aggregate; checkout enforcement."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    Category,
    FamilyGroup,
    FamilyMember,
    Order,
    Product,
    User,
    Vendor,
    Wallet,
)
from core.services import family_portal_wallet_service
from core.services.family_service import ensure_family_wallets_for_member
from core.services.vendor_service import ensure_vendor_wallet


def _tiny_png() -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "p.png",
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0b\x13\x00\x00\x0b\x13\x01\x00\x9a\x9c\x18\x00\x00\x00\nIDATx\x9cc\xf8\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82",
        content_type="image/png",
    )


class ChildSpendingLimitCheckoutTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.cat = Category.objects.create(name="SpendLimCat", slug="spend-lim-cat")
        self.vendor_user = User.objects.create_user(
            username="sl_v",
            password="x",
            phone="9810101050",
            name="SLV",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="SLStore",
            store_slug="sl-store",
            status=Vendor.Status.APPROVED,
        )
        ensure_vendor_wallet(self.vendor)
        img = _tiny_png()
        self.product = Product.objects.create(
            name="SLProd",
            slug="sl-prod",
            sku="SKU-SL-1",
            price=Decimal("25.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=50,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        self.group = FamilyGroup.objects.create(
            name="SLFam",
            leader=self.vendor_user,
            status=FamilyGroup.Status.ACTIVE,
        )
        self.child = User.objects.create_user(
            username="sl_child",
            password="x",
            phone="9820202050",
            name="SLChild",
            role=User.Role.CHILD,
        )
        self.fm = FamilyMember.objects.create(
            group=self.group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
            spending_limit_monthly=Decimal("100.00"),
            spending_limit_weekly=Decimal("0.00"),
            spending_limit_daily=Decimal("0.00"),
        )
        ensure_family_wallets_for_member(
            self.group, self.child, FamilyMember.Role.CHILD
        )
        self.shared = family_portal_wallet_service.get_default_shared_wallet(
            self.group
        )
        if self.shared is None:
            self.shared = Wallet.objects.create(
                owner=self.vendor_user,
                type=Wallet.Type.SHARED,
                label="Family wallet",
                family_group=self.group,
                status=Wallet.Status.ACTIVE,
                balance=Decimal("10000.00"),
            )
        else:
            Wallet.objects.filter(pk=self.shared.pk).update(
                balance=Decimal("10000.00")
            )
        self.mw = family_portal_wallet_service.get_member_family_wallet(
            self.group, self.child
        )
        self.assertIsNotNone(self.mw)
        Wallet.objects.filter(pk=self.mw.pk).update(balance=Decimal("10000.00"))
        # Explicit PERSONAL wallet (checkout "Personal" uses type=PERSONAL to skip limits).
        self.true_personal = Wallet.objects.create(
            owner=self.child,
            type=Wallet.Type.PERSONAL,
            label="Personal",
            status=Wallet.Status.ACTIVE,
            balance=Decimal("10000.00"),
        )
        self.child_token = Token.objects.create(user=self.child)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.child_token.key}")

    def _checkout_payload(self, pay_wallet_id: int | None = None):
        body = {
            "items": [{"product_id": self.product.pk, "quantity": 1}],
            "want_delivery": False,
            "payment_method": "wallet",
            "placed_portal": Order.PlacedPortal.PORTAL_CHILD,
        }
        if pay_wallet_id is not None:
            body["pay_wallet_id"] = str(pay_wallet_id)
        return body

    def test_shared_wallet_checkout_blocked_when_monthly_limit_exceeded(self):
        Order.objects.create(
            order_number="SL-PRE-1",
            customer=self.child,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("80.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("80.00"),
            want_delivery=False,
            payment_wallet=self.shared,
            placed_portal=Order.PlacedPortal.PORTAL_CHILD,
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            self._checkout_payload(pay_wallet_id=self.shared.pk),
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertIn("monthly", r.data["detail"].lower())

    def test_personal_wallet_checkout_skips_member_limits(self):
        Order.objects.create(
            order_number="SL-PRE-2",
            customer=self.child,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("80.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("80.00"),
            want_delivery=False,
            payment_wallet=self.shared,
            placed_portal=Order.PlacedPortal.PORTAL_CHILD,
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            self._checkout_payload(pay_wallet_id=self.true_personal.pk),
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)

    def test_refunded_orders_do_not_count_toward_limit(self):
        Order.objects.create(
            order_number="SL-PRE-3",
            customer=self.child,
            seller=self.vendor,
            status=Order.Status.CANCELLED,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.REFUNDED,
            subtotal=Decimal("95.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("95.00"),
            want_delivery=False,
            payment_wallet=self.shared,
            placed_portal=Order.PlacedPortal.PORTAL_CHILD,
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            self._checkout_payload(pay_wallet_id=self.shared.pk),
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)

    def test_monthly_spent_combines_child_and_shared_payment_wallets(self):
        Order.objects.create(
            order_number="SL-PRE-4a",
            customer=self.child,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("40.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("40.00"),
            want_delivery=False,
            payment_wallet=self.shared,
            placed_portal=Order.PlacedPortal.PORTAL_CHILD,
        )
        Order.objects.create(
            order_number="SL-PRE-4b",
            customer=self.child,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("40.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("40.00"),
            want_delivery=False,
            payment_wallet=self.mw,
            placed_portal=Order.PlacedPortal.PORTAL_CHILD,
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            self._checkout_payload(pay_wallet_id=self.mw.pk),
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertIn("monthly", r.data["detail"].lower())

    def test_daily_limit_enforced(self):
        self.fm.spending_limit_monthly = Decimal("0.00")
        self.fm.spending_limit_daily = Decimal("50.00")
        self.fm.save(
            update_fields=["spending_limit_monthly", "spending_limit_daily"]
        )
        Order.objects.create(
            order_number="SL-PRE-5",
            customer=self.child,
            seller=self.vendor,
            status=Order.Status.PENDING,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("40.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("40.00"),
            want_delivery=False,
            payment_wallet=self.shared,
            placed_portal=Order.PlacedPortal.PORTAL_CHILD,
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            self._checkout_payload(pay_wallet_id=self.shared.pk),
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.data)
        self.assertIn("daily", r.data["detail"].lower())
