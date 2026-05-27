"""Product SKU auto-generation and preview."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Category, Product, User, Vendor
from core.views.admin.resource_views import (
    _generate_unique_product_sku,
    _product_sku_exists,
)


class ProductSkuGenerationTests(TestCase):
    def setUp(self):
        self.cat = Category.objects.create(name="SkuCat", slug="sku-cat-gen")

    def test_generate_sequential_when_no_hint(self):
        Product.objects.create(
            name="Existing",
            sku="KP-000010",
            category=self.cat,
            price=Decimal("1.00"),
            stock=5,
            status=Product.Status.ACTIVE,
            type=Product.Type.PHYSICAL,
        )
        sku = _generate_unique_product_sku()
        self.assertEqual(sku, "KP-000011")
        self.assertFalse(_product_sku_exists(sku))

    def test_generate_from_name_hint(self):
        Product.objects.create(
            name="Rice Bag",
            sku="RICEBAG",
            category=self.cat,
            price=Decimal("1.00"),
            stock=5,
            status=Product.Status.ACTIVE,
            type=Product.Type.PHYSICAL,
        )
        sku = _generate_unique_product_sku(hint="Rice Bag")
        self.assertEqual(sku, "RICEBAG-002")

    def test_vendor_sku_preview_endpoint(self):
        client = APIClient()
        user = User.objects.create_user(
            username="vskuprev",
            password="x",
            phone="9855555701",
            name="Vendor",
            role=User.Role.NORMAL,
        )
        Vendor.objects.create(
            user=user,
            store_name="Sku Preview Store",
            store_slug="sku-preview-store",
            status=Vendor.Status.APPROVED,
        )
        token = Token.objects.create(user=user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        r = client.get("/api/vendor/products/sku-preview/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.data["sku"])
        self.assertFalse(_product_sku_exists(r.data["sku"]))

        r2 = client.get("/api/vendor/products/sku-preview/", {"name": "Organic Honey"})
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertTrue(r2.data["sku"].upper().startswith("ORGANICHONEY"))
