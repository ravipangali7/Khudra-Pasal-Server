"""Flash storefront pricing and portal coupon validation."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from core.models import Category, Coupon, FlashDeal, FlashDealProduct, Product, Vendor
from core.services.coupon_validation import (
    line_eligible_for_coupon,
    validate_and_compute_coupon,
)
from core.services.product_pricing import (
    effective_unit_price,
    flash_override_prices_for_products,
    storefront_unit_price,
)


class StorefrontFlashOverrideTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.cat = Category.objects.create(name="FlashCat", slug="flash-cat")
        u = User.objects.create_user(
            username="flash_vu", password="x", phone="9811111101", name="FV"
        )
        self.vendor = Vendor.objects.create(
            user=u,
            store_name="V",
            store_slug="flash-v",
            status=Vendor.Status.APPROVED,
        )
        self.product = Product.objects.create(
            name="FlashProd",
            slug="flash-prod",
            sku="SKU-FL-1",
            price=Decimal("100.00"),
            category=self.cat,
            seller=self.vendor,
            stock=10,
            status=Product.Status.ACTIVE,
            discount_type=Product.DiscountType.PERCENTAGE,
            discount=Decimal("10.00"),
        )
        now = timezone.now()
        self.deal = FlashDeal.objects.create(
            name="Deal",
            discount_percent=Decimal("5.00"),
            start_at=now - timedelta(days=1),
            end_at=now + timedelta(days=1),
            status=FlashDeal.Status.ACTIVE,
            priority=1,
            vendor=None,
        )
        self.dp = FlashDealProduct.objects.create(
            flash_deal=self.deal,
            product=self.product,
            override_price=Decimal("50.00"),
        )

    def test_effective_unit_price_ignores_flash(self):
        self.assertEqual(effective_unit_price(self.product), Decimal("90.00"))

    def test_storefront_unit_price_uses_flash_override(self):
        now = timezone.now()
        m = flash_override_prices_for_products([self.product.pk], now)
        self.assertEqual(m[self.product.pk], Decimal("50.00"))
        self.assertEqual(
            storefront_unit_price(self.product, flash_overrides=m),
            Decimal("50.00"),
        )


class CouponEligibleSubtotalTests(TestCase):
    def setUp(self):
        self.cat = Category.objects.create(name="CoupCat", slug="coup-cat")
        from django.contrib.auth import get_user_model

        User = get_user_model()
        u = User.objects.create_user(
            username="cp_vu", password="x", phone="9811111102", name="CV"
        )
        self.vendor = Vendor.objects.create(
            user=u,
            store_name="CoupV",
            store_slug="coup-v",
            status=Vendor.Status.APPROVED,
        )
        self.p1 = Product.objects.create(
            name="P1",
            slug="cp1",
            sku="SKU-CP-1",
            price=Decimal("100.00"),
            category=self.cat,
            seller=self.vendor,
            stock=10,
            status=Product.Status.ACTIVE,
        )
        self.p2 = Product.objects.create(
            name="P2",
            slug="cp2",
            sku="SKU-CP-2",
            price=Decimal("40.00"),
            category=self.cat,
            seller=self.vendor,
            stock=10,
            status=Product.Status.ACTIVE,
        )
        now = timezone.now()
        self.deal = FlashDeal.objects.create(
            name="D",
            discount_percent=Decimal("10.00"),
            start_at=now - timedelta(days=1),
            end_at=now + timedelta(days=1),
            status=FlashDeal.Status.ACTIVE,
            priority=1,
            vendor=None,
        )
        FlashDealProduct.objects.create(
            flash_deal=self.deal,
            product=self.p1,
            override_price=Decimal("80.00"),
        )
        self.flash_map = flash_override_prices_for_products(
            [self.p1.pk, self.p2.pk], now
        )
        self.coupon = Coupon.objects.create(
            code="SAVE10",
            type=Coupon.Type.PERCENTAGE,
            value=Decimal("10.00"),
            min_order=Decimal("0.00"),
            status=Coupon.Status.ACTIVE,
        )

    def test_line_eligible_false_for_flash_override(self):
        self.assertFalse(
            line_eligible_for_coupon(self.coupon, self.p1, self.flash_map),
        )
        self.assertTrue(
            line_eligible_for_coupon(self.coupon, self.p2, self.flash_map),
        )

    def test_coupon_discount_only_on_non_flash_lines(self):
        u1 = storefront_unit_price(self.p1, flash_overrides=self.flash_map)
        u2 = storefront_unit_price(self.p2, flash_overrides=self.flash_map)
        lines = [(self.p1, 1, u1), (self.p2, 1, u2)]
        c, disc, err = validate_and_compute_coupon(
            "SAVE10",
            lines=lines,
            flash_overrides=self.flash_map,
        )
        self.assertIsNone(err)
        self.assertIsNotNone(c)
        self.assertEqual(disc, Decimal("4.00"))

