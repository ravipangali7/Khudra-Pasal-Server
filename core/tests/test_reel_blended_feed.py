"""Blended reel feed slot allocation and API wiring."""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Reel, User, Vendor
from core.services.reel_feed_service import allocate_slot_counts, build_blended_feed_page
from core.services.reels_site_settings import get_reels_feed_mix
from core.views.website.home_views import _public_reels_base_queryset


class ReelFeedMixTests(TestCase):
    def test_default_mix_sums_to_one(self):
        mix = get_reels_feed_mix("customer")
        self.assertAlmostEqual(sum(mix.values()), 1.0, places=5)

    def test_allocate_slot_counts_for_page_20(self):
        mix = get_reels_feed_mix("customer")
        slots = allocate_slot_counts(20, mix)
        self.assertEqual(sum(slots.values()), 20)
        self.assertEqual(slots["personalized"], 10)
        self.assertEqual(slots["boosted"], 4)
        self.assertEqual(slots["trending"], 3)
        self.assertEqual(slots["categoryFollow"], 2)
        self.assertEqual(slots["experimental"], 1)


class ReelBlendedFeedApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.vendor_user = User.objects.create_user(
            username="blendu1",
            password="x",
            phone="9866666661",
            name="BV",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Blend Store",
            status=Vendor.Status.APPROVED,
        )
        for i in range(5):
            Reel.objects.create(
                vendor=self.vendor,
                video_url=f"https://example.com/{i}.mp4",
                platform=Reel.Platform.DIRECT_MP4,
                caption=f"r{i}",
                status=Reel.Status.ACTIVE,
                likes=i * 10,
            )

    def test_blended_feed_query_param_returns_results(self):
        r = self.client.get("/api/website/reels/?feed=blended&page_size=5")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(r.data["results"]), 1)
        self.assertEqual(r.data.get("feed_meta", {}).get("mode"), "blended")

    def test_build_blended_page_dedupes(self):
        qs = _public_reels_base_queryset()
        reels, _ = build_blended_feed_page(
            qs,
            user=None,
            audience="customer",
            page=1,
            page_size=5,
        )
        ids = [r.pk for r in reels]
        self.assertEqual(len(ids), len(set(ids)))
