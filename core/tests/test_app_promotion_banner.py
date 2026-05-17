from django.test import TestCase

from core.models import SiteSettings
from core.services.app_promotion_banner import (
    normalize_app_promotion_banner,
    public_app_promotion_banner_from_site,
)


class AppPromotionBannerServiceTests(TestCase):
    def test_public_none_without_headline(self):
        site = SiteSettings.load()
        site.admin_extras = {
            "app_promotion_banner": {"subline": "Only subline", "cta_label": "Go"},
        }
        site.save(update_fields=["admin_extras"])
        self.assertIsNone(public_app_promotion_banner_from_site(site))

    def test_public_includes_headline_and_defaults_cta(self):
        site = SiteSettings.load()
        site.admin_extras = {
            "app_promotion_banner": {"headline": "  Get the app  ", "subline": "Save 20%"},
        }
        site.save(update_fields=["admin_extras"])
        out = public_app_promotion_banner_from_site(site)
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["headline"], "Get the app")
        self.assertEqual(out["subline"], "Save 20%")
        self.assertEqual(out["cta_label"], "Get app")

    def test_normalize_strips_strings(self):
        raw = normalize_app_promotion_banner(
            {"headline": " Hi ", "cta_label": "", "store_url": " https://example.com "}
        )
        self.assertEqual(raw["headline"], "Hi")
        self.assertEqual(raw["cta_label"], "Get app")
        self.assertEqual(raw["store_url"], "https://example.com")
