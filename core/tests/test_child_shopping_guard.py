"""Cart and checkout reject child purchases that violate family product rules."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    Cart,
    CartItem,
    Category,
    FamilyGroup,
    FamilyGroupPermission,
    FamilyMember,
    Order,
    Product,
    ProductRestriction,
    User,
    Vendor,
    Wallet,
)
def _tiny_png() -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "p.png",
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\tpHYs\x00\x00\x0b\x13\x00\x00\x0b\x13\x01\x00\x9a\x9c\x18\x00\x00\x00\nIDATx\x9cc\xf8\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00IEND\xaeB`\x82",
        content_type="image/png",
    )


class ChildShoppingGuardCartTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.cat = Category.objects.create(name="GuardCat", slug="guard-cat-slug")
        self.vendor_user = User.objects.create_user(
            username="gv1",
            password="x",
            phone="9810101011",
            name="V",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="GStore",
            store_slug="g-store-slug",
            status=Vendor.Status.APPROVED,
        )
        img = _tiny_png()
        self.product = Product.objects.create(
            name="GuardProd",
            slug="guard-prod-slug",
            sku="SKU-GUARD-1",
            price=Decimal("100.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=50,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        self.group = FamilyGroup.objects.create(
            name="GuardFam",
            leader=self.vendor_user,
            status=FamilyGroup.Status.ACTIVE,
        )
        self.child = User.objects.create_user(
            username="gchild",
            password="x",
            phone="9820202022",
            name="GChild",
            role=User.Role.CHILD,
        )
        FamilyMember.objects.create(
            group=self.group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        Wallet.objects.create(
            owner=self.child,
            type=Wallet.Type.CHILD,
            family_group=self.group,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("5000.00"),
        )
        self.child_token = Token.objects.create(user=self.child)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.child_token.key}")

    def test_child_cart_add_blocked_category(self):
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.cat,
            is_blocked=True,
        )
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("blocked", r.data["detail"].lower())

    def test_child_cart_add_requires_approval(self):
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.cat,
            requires_approval=True,
        )
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("approval", r.data["detail"].lower())

    def test_child_cart_add_over_max_price(self):
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.cat,
            max_price=Decimal("50.00"),
        )
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("maximum price", r.data["detail"].lower())

    def test_child_cart_add_purchases_off(self):
        perm, _ = FamilyGroupPermission.objects.get_or_create(group=self.group)
        perm.allow_online_purchases = False
        perm.save()
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("online purchases", r.data["detail"].lower())

    def test_child_cart_patch_revalidates(self):
        cart, _ = Cart.objects.get_or_create(user=self.child)
        item = CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.cat,
            is_blocked=True,
        )
        r = self.client.patch(
            f"/api/website/cart/items/{item.pk}/",
            {"quantity": 2},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_normal_user_cart_add_allowed_with_same_restriction(self):
        normal = User.objects.create_user(
            username="gnorm",
            password="x",
            phone="9830303033",
            name="Norm",
            role=User.Role.NORMAL,
        )
        FamilyMember.objects.create(
            group=self.group,
            user=normal,
            role=FamilyMember.Role.PARENT,
            status=FamilyMember.Status.ACTIVE,
        )
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.cat,
            is_blocked=True,
        )
        tok = Token.objects.create(user=normal)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)


class ChildShoppingGuardCheckoutTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.cat = Category.objects.create(name="ChkCat", slug="chk-cat-slug")
        self.vendor_user = User.objects.create_user(
            username="cv1",
            password="x",
            phone="9810101014",
            name="CV",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="CStore",
            store_slug="c-store-slug",
            status=Vendor.Status.APPROVED,
        )
        img = _tiny_png()
        self.product = Product.objects.create(
            name="ChkProd",
            slug="chk-prod-slug",
            sku="SKU-CHK-1",
            price=Decimal("80.00"),
            category=self.cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=50,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        self.group = FamilyGroup.objects.create(
            name="ChkFam",
            leader=self.vendor_user,
            status=FamilyGroup.Status.ACTIVE,
        )
        self.child = User.objects.create_user(
            username="cchild",
            password="x",
            phone="9820202025",
            name="CChild",
            role=User.Role.CHILD,
        )
        FamilyMember.objects.create(
            group=self.group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        Wallet.objects.create(
            owner=self.child,
            type=Wallet.Type.CHILD,
            family_group=self.group,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("5000.00"),
        )
        self.child_token = Token.objects.create(user=self.child)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.child_token.key}")

    def test_child_checkout_blocked(self):
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.cat,
            is_blocked=True,
        )
        r = self.client.post(
            "/api/portal/orders/checkout/",
            {
                "items": [{"product_id": self.product.pk, "quantity": 1}],
                "want_delivery": False,
                "payment_method": "wallet",
                "placed_portal": Order.PlacedPortal.PORTAL_CHILD,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("blocked", r.data["detail"].lower())
