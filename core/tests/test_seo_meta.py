"""SEO: sitemap, share landings, public settings (Part E4 / L)."""

from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import BlogPost, Brand, Category, CMSPage, SiteSettings


@override_settings(PUBLIC_SITE_URL="https://www.example.com", FRONTEND_URL="https://www.example.com")
class SeoMetaTests(TestCase):
    def setUp(self):
        SiteSettings.objects.update_or_create(
            pk=1,
            defaults={
                "site_name": "Khudra Pasal Test",
                "site_description": "Test marketplace description for SEO.",
            },
        )

    def test_sitemap_xml_200(self):
        r = self.client.get("/api/meta/sitemap.xml")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/xml", r["Content-Type"])
        self.assertIn("<urlset", r.content.decode())

    def test_sitemap_loc_absolute(self):
        r = self.client.get("/api/meta/sitemap.xml")
        body = r.content.decode()
        self.assertIn("<loc>https://www.example.com/", body)
        self.assertNotIn("<loc>/", body)

    def test_legacy_website_sitemap_still_works(self):
        r = self.client.get("/api/website/sitemap.xml")
        self.assertEqual(r.status_code, 200)

    def test_public_settings_camel_case(self):
        r = self.client.get("/api/settings/public/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["siteName"], "Khudra Pasal Test")
        self.assertIn("siteMetaDescription", data)

    def test_share_landing_og_title(self):
        post = BlogPost.objects.create(
            title="SEO Test Post",
            slug="seo-test-post",
            content="<p>Body</p>",
            excerpt="Short excerpt",
            status=BlogPost.Status.PUBLISHED,
            seo_title="Custom Meta Title",
            seo_description="Custom meta description for sharing.",
        )
        url = reverse("website-blog-post-share", kwargs={"slug": post.slug})
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('property="og:title"', html)
        self.assertIn("Custom Meta Title", html)
        self.assertIn('rel="canonical" href="https://www.example.com/blog/seo-test-post"', html)
        self.assertIn('property="og:url" content="https://www.example.com/blog/seo-test-post"', html)
        self.assertIn('property="og:type" content="article"', html)

    def test_cms_share_landing(self):
        page = CMSPage.objects.create(
            title="About Us",
            slug="about-seo",
            content="<p>About</p>",
            status=CMSPage.Status.PUBLISHED,
            seo_title="About Page",
        )
        url = reverse("website-cms-page-share", kwargs={"slug": page.slug})
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertIn('property="og:type" content="website"', r.content.decode())

    def test_category_share_canonical_and_og_url(self):
        cat = Category.objects.create(
            name="Snacks",
            slug="snacks-seo",
            status=Category.Status.ACTIVE,
            seo_title="Snacks Category",
        )
        url = reverse("website-category-share", kwargs={"slug": cat.slug})
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn('property="og:url" content="https://www.example.com/category/snacks-seo"', html)
        self.assertIn('property="og:title"', html)

    def test_brand_share_landing(self):
        brand = Brand.objects.create(name="Test Brand", status=Brand.Status.ACTIVE)
        url = reverse("website-brand-share", kwargs={"brand_id": brand.pk})
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn(f'https://www.example.com/brands/{brand.pk}', html)
        self.assertIn("Test Brand", html)
        self.assertIn('property="og:title"', html)
