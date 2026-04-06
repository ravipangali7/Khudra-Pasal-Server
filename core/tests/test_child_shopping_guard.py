"""Cart and checkout reject child purchases that violate family product rules."""

from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
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
    Notification,
    Order,
    Product,
    ProductRestriction,
    PurchaseApprovalRequest,
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


class ChildShoppingGuardAncestorCategoryTests(TestCase):
    """Rules on parent category apply to products in descendant categories."""

    def setUp(self):
        self.client = APIClient()
        self.parent_cat = Category.objects.create(name="ParentCat", slug="parent-guard-slug")
        self.child_cat = Category.objects.create(
            name="ChildCat",
            slug="child-guard-slug",
            parent=self.parent_cat,
        )
        self.vendor_user = User.objects.create_user(
            username="ancestor_v",
            password="x",
            phone="9810101016",
            name="AV",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="AStore",
            store_slug="a-store-slug",
            status=Vendor.Status.APPROVED,
        )
        img = _tiny_png()
        self.product = Product.objects.create(
            name="AncestorProd",
            slug="ancestor-prod-slug",
            sku="SKU-ANC-1",
            price=Decimal("100.00"),
            category=self.child_cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=50,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        self.group = FamilyGroup.objects.create(
            name="AncestorFam",
            leader=self.vendor_user,
            status=FamilyGroup.Status.ACTIVE,
        )
        self.child = User.objects.create_user(
            username="ancestor_child",
            password="x",
            phone="9820202026",
            name="AChild",
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

    def test_parent_requires_approval_applies_to_child_category_product(self):
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.parent_cat,
            requires_approval=True,
        )
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("approval", r.data["detail"].lower())

    def test_approved_purchase_request_allows_cart_for_child_category(self):
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.parent_cat,
            requires_approval=True,
        )
        PurchaseApprovalRequest.objects.create(
            child=self.child,
            parent=self.vendor_user,
            product=self.product,
            amount=Decimal("100.00"),
            status=PurchaseApprovalRequest.Status.APPROVED,
            responded_at=timezone.now(),
        )
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)

    def test_merged_max_price_uses_strictest_cap(self):
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.parent_cat,
            max_price=Decimal("200.00"),
        )
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.child_cat,
            max_price=Decimal("50.00"),
        )
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("maximum price", r.data["detail"].lower())

    def test_parent_blocked_blocks_descendant_category_product(self):
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.parent_cat,
            is_blocked=True,
        )
        r = self.client.post(
            "/api/website/cart/items/",
            {"product_id": self.product.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("blocked", r.data["detail"].lower())


class PurchaseApprovalPortalApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.parent_cat = Category.objects.create(name="PAPR", slug="papr-parent-slug")
        self.leaf_cat = Category.objects.create(
            name="PAPRLeaf",
            slug="papr-leaf-slug",
            parent=self.parent_cat,
        )
        self.leader = User.objects.create_user(
            username="papr_leader",
            password="x",
            phone="9817171717",
            name="Leader",
        )
        self.vendor = Vendor.objects.create(
            user=self.leader,
            store_name="PAPRStore",
            store_slug="papr-store-slug",
            status=Vendor.Status.APPROVED,
        )
        img = _tiny_png()
        self.product = Product.objects.create(
            name="PAPRProd",
            slug="papr-prod-slug",
            sku="SKU-PAPR-1",
            price=Decimal("120.00"),
            category=self.leaf_cat,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=40,
            seller=self.vendor,
            status=Product.Status.ACTIVE,
        )
        self.group = FamilyGroup.objects.create(
            name="PAPRG",
            leader=self.leader,
            status=FamilyGroup.Status.ACTIVE,
        )
        self.child = User.objects.create_user(
            username="papr_child",
            password="x",
            phone="9827272727",
            name="PChild",
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
            balance=Decimal("3000.00"),
        )
        ProductRestriction.objects.create(
            group=self.group,
            family_member=None,
            category=self.parent_cat,
            requires_approval=True,
        )
        self.child_token = Token.objects.create(user=self.child)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.child_token.key}")

    def test_child_creates_purchase_approval_via_portal(self):
        parent_n_before = Notification.objects.filter(recipient=self.leader).count()
        r = self.client.post(
            "/api/portal/child/purchase-approval-requests/",
            {"product_id": self.product.pk},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        self.assertEqual(PurchaseApprovalRequest.objects.count(), 1)
        self.assertEqual(
            Notification.objects.filter(recipient=self.leader).count(),
            parent_n_before + 1,
        )
        n = (
            Notification.objects.filter(recipient=self.leader)
            .order_by("-created_at")
            .first()
        )
        self.assertEqual(n.type, Notification.Type.FAMILY)
        self.assertIn("approval", n.title.lower())

    def test_leader_approves_via_portal(self):
        par = PurchaseApprovalRequest.objects.create(
            child=self.child,
            parent=self.leader,
            product=self.product,
            amount=Decimal("120.00"),
            status=PurchaseApprovalRequest.Status.PENDING,
        )
        leader_tok = Token.objects.create(user=self.leader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {leader_tok.key}")
        child_n_before = Notification.objects.filter(recipient=self.child).count()
        r = self.client.patch(
            f"/api/portal/family/purchase-approval-requests/{par.pk}/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        par.refresh_from_db()
        self.assertEqual(par.status, PurchaseApprovalRequest.Status.APPROVED)
        self.assertEqual(
            Notification.objects.filter(recipient=self.child).count(),
            child_n_before + 1,
        )
        cn = (
            Notification.objects.filter(recipient=self.child)
            .order_by("-created_at")
            .first()
        )
        self.assertEqual(cn.type, Notification.Type.FAMILY)
        self.assertIn("approved", cn.title.lower())

    def test_leader_reject_notifies_child(self):
        par = PurchaseApprovalRequest.objects.create(
            child=self.child,
            parent=self.leader,
            product=self.product,
            amount=Decimal("120.00"),
            status=PurchaseApprovalRequest.Status.PENDING,
        )
        leader_tok = Token.objects.create(user=self.leader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {leader_tok.key}")
        child_n_before = Notification.objects.filter(recipient=self.child).count()
        r = self.client.patch(
            f"/api/portal/family/purchase-approval-requests/{par.pk}/",
            {"status": "rejected", "parent_note": "Not now"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        par.refresh_from_db()
        self.assertEqual(par.status, PurchaseApprovalRequest.Status.REJECTED)
        self.assertEqual(
            Notification.objects.filter(recipient=self.child).count(),
            child_n_before + 1,
        )
        cn = (
            Notification.objects.filter(recipient=self.child)
            .order_by("-created_at")
            .first()
        )
        self.assertEqual(cn.type, Notification.Type.FAMILY)
        self.assertIn("declined", cn.title.lower())

    def test_toggle_requires_approval_clears_unconsumed_approval(self):
        par = PurchaseApprovalRequest.objects.create(
            child=self.child,
            parent=self.leader,
            product=self.product,
            amount=Decimal("120.00"),
            status=PurchaseApprovalRequest.Status.APPROVED,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.child_token.key}")
        r = self.client.get("/api/portal/child/rules/")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertIn(self.product.pk, r.data["approved_purchase_product_ids"])

        leader_tok = Token.objects.create(user=self.leader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {leader_tok.key}")
        r2 = self.client.patch(
            "/api/portal/family/product-restrictions/",
            {
                "category_id": self.parent_cat.pk,
                "is_blocked": False,
                "requires_approval": False,
            },
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK, r2.data)
        par.refresh_from_db()
        self.assertIsNotNone(par.consumed_at)

        r3 = self.client.patch(
            "/api/portal/family/product-restrictions/",
            {
                "category_id": self.parent_cat.pk,
                "is_blocked": False,
                "requires_approval": True,
            },
            format="json",
        )
        self.assertEqual(r3.status_code, status.HTTP_200_OK, r3.data)

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.child_token.key}")
        r4 = self.client.get("/api/portal/child/rules/")
        self.assertEqual(r4.status_code, status.HTTP_200_OK, r4.data)
        self.assertNotIn(self.product.pk, r4.data["approved_purchase_product_ids"])

        r5 = self.client.post(
            "/api/portal/child/purchase-approval-requests/",
            {"product_id": self.product.pk},
            format="json",
        )
        self.assertEqual(r5.status_code, status.HTTP_201_CREATED, r5.data)

    def test_toggle_requires_approval_rejects_pending_requests(self):
        par = PurchaseApprovalRequest.objects.create(
            child=self.child,
            parent=self.leader,
            product=self.product,
            amount=Decimal("120.00"),
            status=PurchaseApprovalRequest.Status.PENDING,
        )
        leader_tok = Token.objects.create(user=self.leader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {leader_tok.key}")
        r = self.client.patch(
            "/api/portal/family/product-restrictions/",
            {
                "category_id": self.parent_cat.pk,
                "is_blocked": False,
                "requires_approval": False,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        par.refresh_from_db()
        self.assertEqual(par.status, PurchaseApprovalRequest.Status.REJECTED)
        self.assertIsNotNone(par.responded_at)


class ProductSerializerCategoryAncestorsTests(TestCase):
    def test_category_ancestor_slugs_order_leaf_to_root(self):
        from core.serializers import ProductSerializer

        root = Category.objects.create(name="RootC", slug="root-c-slug")
        mid = Category.objects.create(name="MidC", slug="mid-c-slug", parent=root)
        leaf = Category.objects.create(name="LeafC", slug="leaf-c-slug", parent=mid)
        vu = User.objects.create_user(
            username="psa_vu",
            password="x",
            phone="9838383838",
            name="V",
        )
        vendor = Vendor.objects.create(
            user=vu,
            store_name="PSAStore",
            store_slug="psa-store-slug",
            status=Vendor.Status.APPROVED,
        )
        img = _tiny_png()
        product = Product.objects.create(
            name="PSAProd",
            slug="psa-prod-slug",
            sku="SKU-PSA-1",
            price=Decimal("10.00"),
            category=leaf,
            image=img,
            type=Product.Type.PHYSICAL,
            stock=5,
            seller=vendor,
            status=Product.Status.ACTIVE,
        )
        data = ProductSerializer(product).data
        self.assertEqual(
            data["category_ancestor_slugs"],
            ["leaf-c-slug", "mid-c-slug", "root-c-slug"],
        )
