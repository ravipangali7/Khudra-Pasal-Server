"""Admin product PATCH gallery_images and GET detail (product image persistence)."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import Category, Product, ProductImage, User


class AdminProductGalleryTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.admin = User.objects.create_user(
            username="adm_gal",
            password=self.pw,
            phone="9766666666",
            name="Admin",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.category = Category.objects.create(
            name="CatGal",
            slug="cat-gal-test",
            status=Category.Status.ACTIVE,
        )
        self.primary = SimpleUploadedFile(
            "primary.jpg",
            b"\xff\xd8\xff\xe0\x00\x10JFIF",
            content_type="image/jpeg",
        )
        self.product = Product.objects.create(
            name="Gallery Test Product",
            slug="gallery-test-product",
            sku="SKU-GAL-1",
            category=self.category,
            image=self.primary,
            price=99,
            type=Product.Type.PHYSICAL,
            status=Product.Status.ACTIVE,
        )

    def _admin_token(self) -> str:
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": self.admin.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def test_patch_gallery_images_creates_product_images_and_get_lists_them(self):
        tok = self._admin_token()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")

        g1 = SimpleUploadedFile(
            "g1.jpg",
            b"\xff\xd8\xff\xe0\x00\x10JFIF",
            content_type="image/jpeg",
        )
        g2 = SimpleUploadedFile(
            "g2.jpg",
            b"\xff\xd8\xff\xe0\x00\x11JFIF",
            content_type="image/jpeg",
        )

        r = self.client.patch(
            f"/api/admin/products/{self.product.pk}/",
            {"gallery_images": [g1, g2]},
            format="multipart",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

        self.assertEqual(ProductImage.objects.filter(product=self.product).count(), 2)
        rows = list(
            ProductImage.objects.filter(product=self.product).order_by("sort_order", "id")
        )
        self.assertTrue(all(r.image.name for r in rows))

        r2 = self.client.get(f"/api/admin/products/{self.product.pk}/")
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertIn("images", r2.data)
        self.assertEqual(len(r2.data["images"]), 2)
        for im in r2.data["images"]:
            self.assertIn("image_url", im)
            self.assertTrue(im["image_url"])
