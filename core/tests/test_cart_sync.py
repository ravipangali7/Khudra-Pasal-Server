"""Website cart stays aligned with portal checkout / quote rules."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Cart, CartItem, Category, Product, User, Vendor


def _tiny_png() -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "p.png",
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0b\x13\x00\x00\x0b\x13\x01\x00\x9a\x9c\x18\x00\x00\x00\nIDATx\x9cc\xf8\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82",
        content_type="image/png",
    )


class CartSyncForCheckoutTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="cart_sync_u",
            password="x",
            phone="9811111101",
            name="Cart Sync",
            role=User.Role.NORMAL,
        )
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.cat = Category.objects.create(name="SyncCat", slug="sync-cat")
        vu = User.objects.create_user(
            username="cart_sync_v",
            password="x",
            phone="9811111102",
            name="SV",
        )
        self.vendor = Vendor.objects.create(
            user=vu,
            store_name="SyncStore",
            store_slug="sync-store",
            status=Vendor.Status.APPROVED,
        )
        img = _tiny_png()
        self.product = Product.objects.create(
            name="SyncProd",
            slug="sync-prod",
            sku="SKU-SYNC-1",
            price=Decimal("50.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=5,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )

    def test_cart_get_drops_inactive_product(self):
        cart, _ = Cart.objects.get_or_create(user=self.user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=2)
        # DRAFT: still not checkout-eligible; avoid OUT_OF_STOCK here because post_save
        # sync_stock_status() flips OOS back to ACTIVE when stock > 0.
        self.product.status = Product.Status.DRAFT
        self.product.save(update_fields=["status"])

        r = self.client.get("/api/website/cart/")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(len(r.data.get("items", [])), 0)
        self.assertFalse(CartItem.objects.filter(cart=cart).exists())

    def test_checkout_quote_ok_after_cart_get_pruned(self):
        cart, _ = Cart.objects.get_or_create(user=self.user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        self.product.status = Product.Status.DRAFT
        self.product.save(update_fields=["status"])

        cr = self.client.get("/api/website/cart/")
        self.assertEqual(cr.status_code, status.HTTP_200_OK)
        self.assertEqual(len(cr.data.get("items", [])), 0)

        good = Product.objects.create(
            name="GoodProd",
            slug="good-prod",
            sku="SKU-SYNC-2",
            price=Decimal("30.00"),
            category=self.cat,
            image=_tiny_png(),
            type=Product.Type.PHYSICAL,
            stock=3,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        cart2, _ = Cart.objects.get_or_create(user=self.user)
        CartItem.objects.create(cart=cart2, product=good, quantity=1)

        qr = self.client.post(
            "/api/portal/orders/checkout-quote/",
            {
                "items": [{"product_id": good.pk, "quantity": 1}],
                "want_delivery": False,
            },
            format="json",
        )
        self.assertEqual(qr.status_code, status.HTTP_200_OK, qr.data)
