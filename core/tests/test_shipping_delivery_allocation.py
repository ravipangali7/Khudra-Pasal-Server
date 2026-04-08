"""Shipping method + weight resolution in compute_delivery_allocation."""

from decimal import Decimal

from django.test import TestCase

from core.models import (
    Category,
    Product,
    ShippingMethod,
    ShippingSettings,
    ShippingZone,
    WeightRule,
)
from core.services.portal_checkout_pricing import (
    cart_weight_kg_from_groups,
    checkout_items_weight_kg,
    compute_delivery_allocation,
)


class ShippingDeliveryAllocationTests(TestCase):
    def setUp(self):
        self.zone = ShippingZone.objects.create(
            name="AllocZone",
            areas="Test",
            flat_rate=Decimal("10.00"),
            status=ShippingZone.Status.ACTIVE,
        )
        WeightRule.objects.create(
            zone=self.zone,
            min_weight=Decimal("0.000"),
            max_weight=Decimal("500.000"),
            rate_per_kg=Decimal("5.00"),
        )
        sh = ShippingSettings.load()
        sh.default_zone = self.zone
        sh.seller_pays_shipping = False
        sh.free_shipping_global = False
        sh.default_checkout_weight_kg = Decimal("1.000")
        sh.save()

        self.cat = Category.objects.create(name="ShipCat", slug="ship-cat")
        self.p_heavy = Product.objects.create(
            name="Heavy",
            slug="heavy-a",
            sku="SKU-HVY-1",
            price=Decimal("50.00"),
            category=self.cat,
            image="products/x.jpg",
            type=Product.Type.PHYSICAL,
            stock=5,
            status=Product.Status.ACTIVE,
            attributes={"weight_kg": 2.5},
        )

    def _group_line(self, product: Product, qty: int, line_total: Decimal = Decimal("50.00")):
        return {None: [(product, qty, Decimal("50.00"), line_total)]}

    def test_zone_only_uses_cart_weight_from_attributes(self):
        groups = self._group_line(self.p_heavy, 2, Decimal("100.00"))
        self.assertEqual(cart_weight_kg_from_groups(groups), 5.0)

        fee, _alloc, zone, err, wkg, mid = compute_delivery_allocation(
            {"shipping_zone_id": str(self.zone.pk)},
            True,
            Decimal("100.00"),
            groups,
        )
        self.assertIsNone(err)
        self.assertEqual(zone.pk, self.zone.pk)
        self.assertEqual(wkg, 5.0)
        self.assertIsNone(mid)
        # flat 10 + 5 * 5 = 35
        self.assertEqual(fee, Decimal("35.00"))

    def test_explicit_weight_kg_overrides_cart(self):
        groups = self._group_line(self.p_heavy, 1, Decimal("50.00"))
        fee, _, _, err, wkg, _ = compute_delivery_allocation(
            {
                "shipping_zone_id": str(self.zone.pk),
                "weight_kg": 1.0,
            },
            True,
            Decimal("50.00"),
            groups,
        )
        self.assertIsNone(err)
        self.assertEqual(wkg, 1.0)
        # 10 + 1*5 = 15
        self.assertEqual(fee, Decimal("15.00"))

    def test_flat_shipping_method_overrides_zone_weight_math(self):
        groups = self._group_line(self.p_heavy, 10, Decimal("500.00"))
        m = ShippingMethod.objects.create(
            name="FlatShip",
            type=ShippingMethod.Type.FLAT,
            cost=Decimal("25.00"),
            status=ShippingMethod.Status.ACTIVE,
        )
        fee, _, _, err, wkg, mid = compute_delivery_allocation(
            {
                "shipping_zone_id": str(self.zone.pk),
                "shipping_method_id": str(m.pk),
            },
            True,
            Decimal("500.00"),
            groups,
        )
        self.assertIsNone(err)
        self.assertEqual(mid, str(m.pk))
        self.assertEqual(wkg, 25.0)  # 2.5 * 10 from attributes
        self.assertEqual(fee, Decimal("25.00"))

    def test_invalid_shipping_method_returns_error(self):
        groups = self._group_line(self.p_heavy, 1, Decimal("50.00"))
        _fee, _alloc, zone, err, _wkg, _mid = compute_delivery_allocation(
            {
                "shipping_zone_id": str(self.zone.pk),
                "shipping_method_id": "999999",
            },
            True,
            Decimal("50.00"),
            groups,
        )
        self.assertIsNotNone(err)
        self.assertIn("shipping_method", err.lower())
        self.assertIsNone(zone)

    def test_checkout_items_weight_kg_sums_products(self):
        w = checkout_items_weight_kg(
            [
                {"product_id": self.p_heavy.pk, "quantity": 2},
            ]
        )
        self.assertEqual(w, 5.0)
