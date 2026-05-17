from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import AppPromotionAttribution, SiteSettings, User
from core.tests.wallet_test_settings import relax_wallet_settings_for_tests


class AppPromotionAttributionFlowTests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        self.client = APIClient()
        self.site = SiteSettings.load()
        extras = dict(self.site.admin_extras or {})
        extras["app_promotion_banner"] = {
            "headline": "Get the app",
            "discount_percent": "20",
        }
        self.site.admin_extras = extras
        self.site.save(update_fields=["admin_extras"])
        self.user = User.objects.create_user(
            username="promo_u",
            password="x",
            phone="9812345678",
            name="Promo User",
            role=User.Role.NORMAL,
        )
        tok, _ = Token.objects.get_or_create(user=self.user)
        self.auth = f"Token {tok.key}"

    def test_banner_click_creates_attribution(self):
        r = self.client.post("/api/website/app-promotion-banner/click/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.data.get("ok"))
        token = r.data.get("visit_token")
        self.assertTrue(token)
        self.assertEqual(
            AppPromotionAttribution.objects.filter(visit_token=token).count(), 1
        )

    def test_claim_install_and_store_info_percent(self):
        attr = AppPromotionAttribution.objects.create(
            user=self.user,
            visit_token="tok123",
            status=AppPromotionAttribution.Status.CLICKED,
            discount_percent=Decimal("20"),
            banner_headline="Get the app",
        )
        self.client.credentials(HTTP_AUTHORIZATION=self.auth)
        r = self.client.post(
            "/api/auth/app-promotion-banner/claim-install/",
            {"visit_token": attr.visit_token},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        attr.refresh_from_db()
        self.assertEqual(attr.status, AppPromotionAttribution.Status.INSTALLED)
        info = self.client.get("/api/website/store-info/")
        banner = info.data.get("app_promotion_banner")
        self.assertEqual(banner.get("discount_percent"), "20.00")
