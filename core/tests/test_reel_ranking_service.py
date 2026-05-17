"""Unit tests for reels feed ranking scores and rules."""

from django.test import TestCase
from django.utils import timezone

from core.models import Reel, User, Vendor
from core.services import reel_ranking_service as ranking
from core.services.reel_feed_service import allocate_slot_counts
from core.services.reels_site_settings import get_reels_feed_mix


class ReelRankingScoreTests(TestCase):
    def setUp(self):
        self.vendor_user = User.objects.create_user(
            username="ranku1",
            password="x",
            phone="9866666601",
            name="Rank",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Rank Store",
            status=Vendor.Status.APPROVED,
        )

    def _reel(self, **kwargs) -> Reel:
        defaults = dict(
            vendor=self.vendor,
            video_url="https://example.com/v.mp4",
            platform=Reel.Platform.DIRECT_MP4,
            status=Reel.Status.ACTIVE,
            views=100,
            likes=10,
            shares=5,
            cart_adds=2,
        )
        defaults.update(kwargs)
        return Reel.objects.create(**defaults)

    def test_engagement_score_positive(self):
        r = self._reel()
        self.assertGreater(ranking.engagement_score(r), 0)

    def test_final_score_category_bonus(self):
        r = self._reel()
        ctx = ranking.UserFeedContext(
            user=self.vendor_user,
            category_ids=[99],
        )
        base = ranking.final_score(r, ctx)
        r.product = None
        low = ranking.final_score(r, ctx)
        ctx.category_ids = []
        self.assertGreaterEqual(base, low)

    def test_inject_boosted_every_four_organic(self):
        organic = [self._reel(caption=f"o{i}") for i in range(8)]
        boosted = [self._reel(caption=f"b{i}", is_sponsored=True) for i in range(3)]
        feed = ranking.inject_boosted_reels(organic, boosted, page_size=10)
        self.assertEqual(len(feed), 10)
        boosted_positions = [i for i, r in enumerate(feed) if r.is_sponsored]
        self.assertGreaterEqual(len(boosted_positions), 2)

    def test_diversity_vendor_cap(self):
        reels = [self._reel() for _ in range(5)]
        ctx = ranking.UserFeedContext(user=None)
        out = ranking.apply_diversity(reels, ctx)
        self.assertEqual(len(out), 2)

    def test_default_mix_ratios(self):
        mix = get_reels_feed_mix("customer")
        self.assertAlmostEqual(sum(mix.values()), 1.0)
        slots = allocate_slot_counts(20, mix)
        self.assertEqual(sum(slots.values()), 20)
        self.assertEqual(slots["personalized"], 10)
        self.assertEqual(slots["boosted"], 4)
        self.assertEqual(slots["trending"], 4)
        self.assertEqual(slots["random"], 2)
