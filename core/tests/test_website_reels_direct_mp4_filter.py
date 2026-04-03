"""Public reel lists honor only_direct_mp4 query param."""

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Reel, User, Vendor


class WebsiteReelsDirectMp4FilterTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.vendor_user = User.objects.create_user(
            username="reelu1",
            password="x",
            phone="9877777777",
            name="RV",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Reel Store",
            status=Vendor.Status.APPROVED,
        )
        self.mp4 = Reel.objects.create(
            vendor=self.vendor,
            video_url="https://example.com/a.mp4",
            platform=Reel.Platform.DIRECT_MP4,
            caption="mp4",
            status=Reel.Status.ACTIVE,
        )
        self.yt = Reel.objects.create(
            vendor=self.vendor,
            video_url="https://www.youtube.com/shorts/abc",
            platform=Reel.Platform.YOUTUBE_SHORTS,
            caption="yt",
            status=Reel.Status.ACTIVE,
        )

    def test_website_reels_without_filter_returns_both(self):
        r = self.client.get("/api/website/reels/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in r.data["results"]}
        self.assertIn(self.mp4.pk, ids)
        self.assertIn(self.yt.pk, ids)

    def test_website_reels_only_direct_mp4_excludes_embeds(self):
        r = self.client.get("/api/website/reels/?only_direct_mp4=true")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in r.data["results"]}
        self.assertIn(self.mp4.pk, ids)
        self.assertNotIn(self.yt.pk, ids)

    def test_reels_trending_only_direct_mp4(self):
        r = self.client.get("/api/reels/trending/?only_direct_mp4=1")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in r.data["results"]}
        self.assertIn(self.mp4.pk, ids)
        self.assertNotIn(self.yt.pk, ids)

    def test_reels_by_vendor_public_only_direct_mp4(self):
        r = self.client.get(
            f"/api/reels/vendor/{self.vendor.pk}/?only_direct_mp4=yes"
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = {row["id"] for row in r.data["results"]}
        self.assertIn(self.mp4.pk, ids)
        self.assertNotIn(self.yt.pk, ids)
