"""Blended reel feed slot allocation and API wiring."""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Reel, ReelView, User, Vendor
from core.services import reel_ranking_service as ranking
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
        self.assertEqual(slots["trending"], 4)
        self.assertEqual(slots["random"], 2)


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

    def test_blended_feed_after_views_still_returns_reels(self):
        customer = User.objects.create_user(
            username="blendcust",
            password="x",
            phone="9866666662",
            name="Cust",
            role=User.Role.NORMAL,
        )
        qs = _public_reels_base_queryset()
        all_reels = list(qs.order_by("pk")[:2])
        self.assertEqual(len(all_reels), 2)
        for reel in all_reels:
            ReelView.objects.create(reel=reel, user=customer)
            ranking.record_feed_impression(customer.id, reel.pk)
            ranking.record_feed_impression(customer.id, reel.pk)

        reels, _ = build_blended_feed_page(
            qs,
            user=customer,
            audience="customer",
            page=1,
            page_size=5,
            feed_seed="session-test",
        )
        self.assertGreaterEqual(len(reels), 1)

    def test_feed_seed_changes_order(self):
        qs = _public_reels_base_queryset()
        a, _ = build_blended_feed_page(
            qs,
            user=None,
            audience="customer",
            page=1,
            page_size=5,
            feed_seed="seed-a",
        )
        b, _ = build_blended_feed_page(
            qs,
            user=None,
            audience="customer",
            page=1,
            page_size=5,
            feed_seed="seed-b",
        )
        self.assertGreaterEqual(len(a), 2)
        self.assertGreaterEqual(len(b), 2)
        self.assertNotEqual([r.pk for r in a], [r.pk for r in b])
