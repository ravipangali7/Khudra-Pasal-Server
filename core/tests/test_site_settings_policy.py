"""SiteSettings toggles: checkout gate, registrations, POS, public store_info."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    Cart,
    CartItem,
    Category,
    Product,
    ShippingMethod,
    ShippingSettings,
    ShippingZone,
    SiteSettings,
    User,
    Vendor,
)
from core.tests.wallet_test_settings import relax_wallet_settings_for_tests


class SiteSettingsPolicyTests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        self.client = APIClient()
        self.site = SiteSettings.load()
        self.site.maintenance_mode = False
        self.site.temporary_shop_close = False
        self.site.new_registrations = True
        self.site.pos_enabled = True
        self.site.save()

        self.vendor_user = User.objects.create_user(
            username="ss_vendor_u",
            password="x",
            phone="9811111111",
            name="V",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="SS Store",
            store_slug="ss-store",
            status=Vendor.Status.APPROVED,
            commission_rate=Decimal("10.00"),
        )
        self.cat = Category.objects.create(name="SSCat", slug="ss-cat")
        self.product = Product.objects.create(
            seller=self.vendor,
            category=self.cat,
            name="SS Widget",
            sku="SSW1",
            price=Decimal("100"),
            stock=50,
            status=Product.Status.ACTIVE,
        )

        self.zone = ShippingZone.objects.create(
            name="Z1", areas="KTM", status=ShippingZone.Status.ACTIVE
        )
        ShippingMethod.objects.create(name="Std", type="flat", status=ShippingMethod.Status.ACTIVE)
        sh = ShippingSettings.load()
        sh.default_zone_id = str(self.zone.pk)
        sh.save()

    def test_store_info_exposes_flags(self):
        r = self.client.get("/api/website/store-info/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("maintenance_mode", r.data)
        self.assertIn("pos_enabled", r.data)
        self.assertIn("chabot_script", r.data)

    def test_store_info_chabot_script_from_admin_extras(self):
        extras = dict(self.site.admin_extras or {})
        extras["chatbot"] = {"chabot_script": '<script>window.__kp_test=1</script>'}
        self.site.admin_extras = extras
        self.site.save(update_fields=["admin_extras"])
        r = self.client.get("/api/website/store-info/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIn("window.__kp_test=1", r.data["chabot_script"])

    def test_store_info_app_promotion_banner_null_without_headline(self):
        r = self.client.get("/api/website/store-info/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIsNone(r.data.get("app_promotion_banner"))

    def test_store_info_app_promotion_banner_when_configured(self):
        extras = dict(self.site.admin_extras or {})
        extras["app_promotion_banner"] = {
            "headline": "Download the app and get 20% discount offer",
            "subline": "Exclusive deals on Android",
            "cta_label": "Get app",
            "store_url": "https://play.google.com/store/apps/details?id=test",
            "gradient_from": "#ff6600",
            "gradient_to": "#6d28d9",
        }
        self.site.admin_extras = extras
        self.site.save(update_fields=["admin_extras"])
        r = self.client.get("/api/website/store-info/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        banner = r.data.get("app_promotion_banner")
        self.assertIsInstance(banner, dict)
        self.assertEqual(banner["headline"], extras["app_promotion_banner"]["headline"])
        self.assertEqual(banner["subline"], "Exclusive deals on Android")
        self.assertEqual(banner["cta_label"], "Get app")

    def test_shipping_quote_blocked_when_maintenance(self):
        self.site.maintenance_mode = True
        self.site.save(update_fields=["maintenance_mode"])
        r = self.client.post(
            "/api/website/shipping-quote/",
            {"zone_id": str(self.zone.pk), "order_total": "100"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(r.data.get("code"), "maintenance_mode")

    def test_otp_signup_send_blocked_when_registrations_closed(self):
        self.site.new_registrations = False
        self.site.save(update_fields=["new_registrations"])
        r = self.client.post(
            "/api/auth/otp/send/",
            {"phone": "9800000001", "purpose": "signup", "name": "N"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "registrations_closed")

    def test_vendor_pos_blocked_when_disabled(self):
        self.site.pos_enabled = False
        self.site.save(update_fields=["pos_enabled"])
        tok, _ = Token.objects.get_or_create(user=self.vendor_user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.post(
            "/api/vendor/pos/checkout/",
            {
                "items": [{"product_id": self.product.pk, "quantity": 1}],
                "payment_method": "cash",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "pos_disabled")

    def test_vendor_pos_blocked_when_vendor_pos_disabled(self):
        self.vendor.pos_enabled = False
        self.vendor.save(update_fields=["pos_enabled"])
        tok, _ = Token.objects.get_or_create(user=self.vendor_user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.post(
            "/api/vendor/pos/checkout/",
            {
                "items": [{"product_id": self.product.pk, "quantity": 1}],
                "payment_method": "cash",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "vendor_pos_disabled")

    def test_vendor_me_pos_enabled_reflects_vendor_flag(self):
        self.vendor.pos_enabled = False
        self.vendor.save(update_fields=["pos_enabled"])
        tok, _ = Token.objects.get_or_create(user=self.vendor_user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.get("/api/vendor/me/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.data.get("site_pos_enabled"))
        self.assertFalse(r.data.get("pos_enabled"))
        self.assertFalse(r.data.get("vendor_pos_enabled"))

    def test_cart_add_blocked_when_shop_closed(self):
        cust = User.objects.create_user(
            username="ss_cart",
            password="x",
            phone="9822222222",
            name="C",
            role=User.Role.NORMAL,
        )
        tok, _ = Token.objects.get_or_create(user=cust)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        self.site.temporary_shop_close = True
        self.site.save(update_fields=["temporary_shop_close"])
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "temporary_shop_close")

    def test_cart_delete_allowed_when_shop_closed(self):
        cust = User.objects.create_user(
            username="ss_cart2",
            password="x",
            phone="9833333333",
            name="C2",
            role=User.Role.NORMAL,
        )
        cart, _ = Cart.objects.get_or_create(user=cust)
        item = CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        tok, _ = Token.objects.get_or_create(user=cust)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        self.site.temporary_shop_close = True
        self.site.save(update_fields=["temporary_shop_close"])
        r = self.client.delete(f"/api/website/cart/items/{item.pk}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
