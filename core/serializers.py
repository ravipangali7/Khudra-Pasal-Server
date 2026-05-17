from decimal import Decimal

from django.core.validators import MinValueValidator
from django.utils import timezone
from rest_framework import serializers

from core.services.product_pricing import effective_unit_price, storefront_unit_price
from core.services.storefront_product_visibility import product_is_storefront_purchasable

from core.models import (
    AutoApprovalRule,
    Banner,
    Brand,
    BlogPost,
    Cart,
    CartItem,
    Category,
    FlashDeal,
    FamilyJoinRequest,
    FamilyMember,
    FamilyWalletCategory,
    Order,
    Product,
    ProductImage,
    ProductRestriction,
    ProductWishlist,
    Reel,
    ReelComment,
    ReelInteraction,
    User,
    Vendor,
)


def _category_ancestor_slugs(category) -> list[str]:
    out: list[str] = []
    cur = category
    while cur is not None:
        out.append(cur.slug)
        cur = cur.parent
    return out


class VendorMiniSerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Vendor
        fields = ["id", "store_name", "store_slug", "rating", "is_verified", "logo_url"]

    def get_logo_url(self, obj):
        if not obj.logo:
            return ""
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.logo.url)
        return obj.logo.url


class CategoryTreeSerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "icon", "image_url", "sort_order", "children"]

    def get_image_url(self, obj):
        if not obj.image:
            return ""
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url

    def get_children(self, obj):
        queryset = obj.children.filter(status=Category.Status.ACTIVE).order_by("sort_order", "name")
        return CategoryTreeSerializer(queryset, many=True, context=self.context).data


class CatalogCategorySerializer(CategoryTreeSerializer):
    """Root category tree row with capped `products` injected via context `catalog_products_by_root_id`."""

    products = serializers.SerializerMethodField()

    class Meta(CategoryTreeSerializer.Meta):
        fields = [*CategoryTreeSerializer.Meta.fields, "products"]

    def get_products(self, obj):
        m = self.context.get("catalog_products_by_root_id") or {}
        rows = m.get(obj.id) or []
        return ProductSerializer(rows, many=True, context=self.context).data


class BrandSerializer(serializers.ModelSerializer):
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Brand
        fields = ["id", "name", "logo_url"]

    def get_logo_url(self, obj):
        request = self.context.get("request")
        if not obj.logo:
            return ""
        if request:
            return request.build_absolute_uri(obj.logo.url)
        return obj.logo.url


class ProductImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ProductImage
        fields = ["id", "image_url", "sort_order"]

    def get_image_url(self, obj):
        request = self.context.get("request")
        if not obj.image:
            return ""
        if request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url


class ProductSerializer(serializers.ModelSerializer):
    """Storefront product: `price` is effective (matches cart); `original_price` is list price when on sale."""

    category_slug = serializers.CharField(source="category.slug", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    category_id = serializers.IntegerField(source="category.id", read_only=True)
    parent_category_slug = serializers.SerializerMethodField()
    parent_category_name = serializers.SerializerMethodField()
    category_ancestor_slugs = serializers.SerializerMethodField()
    list_price = serializers.DecimalField(source="price", max_digits=10, decimal_places=2, read_only=True)
    price = serializers.SerializerMethodField()
    original_price = serializers.SerializerMethodField()
    flash_deal_id = serializers.SerializerMethodField()
    coupon_hints = serializers.SerializerMethodField()
    unit_short_name = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    seller = VendorMiniSerializer(read_only=True)

    class Meta:
        model = Product
        fields = [
            "id",
            "name",
            "slug",
            "description",
            "short_description",
            "price",
            "list_price",
            "original_price",
            "flash_deal_id",
            "coupon_hints",
            "discount_type",
            "discount",
            "category_id",
            "category_slug",
            "category_name",
            "parent_category_slug",
            "parent_category_name",
            "category_ancestor_slugs",
            "unit_short_name",
            "stock",
            "rating",
            "review_count",
            "status",
            "is_featured",
            "is_bestseller",
            "image_url",
            "images",
            "seller",
            "seo_title",
            "seo_description",
            "seo_keywords",
            "created_at",
        ]

    def get_price(self, obj):
        fo = self.context.get("flash_overrides")
        return str(storefront_unit_price(obj, flash_overrides=fo))

    def get_original_price(self, obj):
        fo = self.context.get("flash_overrides")
        store = storefront_unit_price(obj, flash_overrides=fo)
        eff = effective_unit_price(obj)
        list_p = obj.price
        if store < list_p:
            return str(list_p)
        if eff < list_p:
            return str(list_p)
        return str(store)

    def get_flash_deal_id(self, obj):
        m = self.context.get("flash_deal_ids") or {}
        return m.get(obj.pk)

    def get_coupon_hints(self, obj):
        m = self.context.get("coupon_hints_by_product_id") or {}
        return m.get(obj.pk) or []

    def get_parent_category_slug(self, obj):
        p = obj.category.parent
        return p.slug if p else None

    def get_parent_category_name(self, obj):
        p = obj.category.parent
        return p.name if p else None

    def get_category_ancestor_slugs(self, obj):
        return _category_ancestor_slugs(obj.category)

    def get_unit_short_name(self, obj):
        return obj.unit.short_name if obj.unit_id else ""

    def get_image_url(self, obj):
        from core.views.admin.admin_write_utils import product_primary_image_url

        request = self.context.get("request")
        return product_primary_image_url(request, obj)

    def get_images(self, obj):
        if hasattr(obj, "_prefetched_objects_cache") and "images" in obj._prefetched_objects_cache:
            imgs = sorted(obj.images.all(), key=lambda x: (x.sort_order, x.id))
        else:
            imgs = list(obj.images.order_by("sort_order", "id"))
        return ProductImageSerializer(imgs, many=True, context=self.context).data


class BannerSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    category_slug = serializers.CharField(source="category.slug", read_only=True)

    class Meta:
        model = Banner
        fields = [
            "id",
            "title",
            "subtitle",
            "placement",
            "image_url",
            "click_url",
            "category_slug",
            "gradient",
            "status",
            "sort_order",
            "card_variant",
            "cta_text",
            "badge_text",
        ]

    def get_image_url(self, obj):
        request = self.context.get("request")
        if not obj.image:
            return ""
        if request:
            return request.build_absolute_uri(obj.image.url)
        return obj.image.url


class FlashDealProductSerializer(serializers.Serializer):
    product = ProductSerializer()
    override_price = serializers.DecimalField(max_digits=10, decimal_places=2, allow_null=True)


class FlashDealSerializer(serializers.ModelSerializer):
    products = serializers.SerializerMethodField()

    class Meta:
        model = FlashDeal
        fields = ["id", "name", "discount_percent", "start_at", "end_at", "status", "priority", "products"]

    def get_products(self, obj):
        deal_products = obj.deal_products.select_related(
            "product",
            "product__category",
            "product__seller",
            "product__brand",
            "product__unit",
        ).filter(product__status=Product.Status.ACTIVE)
        data = []
        for deal_product in deal_products:
            product_data = ProductSerializer(deal_product.product, context=self.context).data
            data.append(
                {
                    "product": product_data,
                    "override_price": deal_product.override_price,
                }
            )
        return data


class AdminUserSerializer(serializers.ModelSerializer):
    customer_document = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    order_count = serializers.SerializerMethodField()
    total_spent = serializers.SerializerMethodField()
    wallet_balance = serializers.SerializerMethodField()
    app_promo_status = serializers.SerializerMethodField()
    app_promo_discount_percent = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "name",
            "phone",
            "email",
            "role",
            "kyc_status",
            "is_active",
            "is_staff",
            "created_at",
            "customer_document",
            "avatar",
            "order_count",
            "total_spent",
            "wallet_balance",
            "app_promo_status",
            "app_promo_discount_percent",
        ]

    def get_customer_document(self, obj):
        from core.views.admin.admin_write_utils import absolute_media_url

        request = self.context.get("request")
        if not request or not getattr(obj, "customer_document", None):
            return ""
        return absolute_media_url(request, obj.customer_document)

    def get_avatar(self, obj):
        from core.views.admin.admin_write_utils import user_public_avatar_url

        request = self.context.get("request")
        if not request:
            return ""
        return user_public_avatar_url(request, obj) or ""

    def get_order_count(self, obj):
        return int(getattr(obj, "admin_order_count", 0) or 0)

    def get_total_spent(self, obj):
        v = getattr(obj, "admin_total_spent", None)
        return float(v or 0)

    def get_wallet_balance(self, obj):
        v = getattr(obj, "admin_wallet_balance", None)
        return float(v or 0)

    def get_app_promo_status(self, obj):
        from core.models import AppPromotionAttribution

        try:
            return str(obj.app_promotion_attribution.status or "")
        except AppPromotionAttribution.DoesNotExist:
            return ""

    def get_app_promo_discount_percent(self, obj):
        from core.models import AppPromotionAttribution

        try:
            return float(obj.app_promotion_attribution.discount_percent or 0)
        except AppPromotionAttribution.DoesNotExist:
            return 0.0


class RecentOrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    seller_name = serializers.CharField(source="seller.store_name", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "order_number",
            "customer_name",
            "seller_name",
            "status",
            "payment_method",
            "payment_status",
            "total",
            "created_at",
        ]


class ReelPublicSerializer(serializers.ModelSerializer):
    thumbnail_url = serializers.SerializerMethodField()
    vendor = serializers.SerializerMethodField()
    product = serializers.SerializerMethodField()
    comments_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    is_bookmarked = serializers.SerializerMethodField()
    has_added_to_cart = serializers.SerializerMethodField()
    is_sponsored = serializers.SerializerMethodField()

    class Meta:
        model = Reel
        fields = [
            "id",
            "video_url",
            "platform",
            "caption",
            "tags",
            "status",
            "views",
            "likes",
            "shares",
            "bookmarks",
            "cart_adds",
            "comments_count",
            "created_at",
            "thumbnail_url",
            "vendor",
            "product",
            "is_liked",
            "is_bookmarked",
            "has_added_to_cart",
            "is_sponsored",
            "boost_expires_at",
            "boost_expected_views",
            "boost_tier",
            "boost_daily_budget_npr",
        ]

    def _abs_url(self, file_field):
        if not file_field:
            return ""
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(file_field.url)
        return file_field.url

    def get_thumbnail_url(self, obj):
        return self._abs_url(obj.thumbnail)

    def get_vendor(self, obj):
        v = obj.vendor
        return {
            "id": v.id,
            "store_name": v.store_name,
            "store_slug": v.store_slug,
            "is_verified": v.is_verified,
            "logo_url": self._abs_url(v.logo),
        }

    def get_product(self, obj):
        if not obj.product_id:
            return None
        p = obj.product
        eff = effective_unit_price(p)
        orig = p.price
        disc = 0
        if orig and eff and orig > eff:
            disc = int(round((orig - eff) / orig * 100))
        cat = p.category
        parent = cat.parent if cat.parent_id else None
        return {
            "id": p.id,
            "name": p.name,
            "price": float(eff),
            "original_price": float(orig),
            "discount": disc,
            "image_url": self._abs_url(p.image),
            "in_stock": p.stock > 0,
            "rating": float(p.rating or 0),
            "reviews": p.review_count,
            "category_slug": cat.slug,
            "parent_category_slug": parent.slug if parent else None,
            "category_ancestor_slugs": _category_ancestor_slugs(cat),
            "purchasable": product_is_storefront_purchasable(p),
        }

    def get_comments_count(self, obj):
        # Do not use getattr(..., obj.comments.count()) — the default is evaluated
        # before getattr runs, causing a query on every row even when annotated.
        if hasattr(obj, "comments_count"):
            return obj.comments_count
        return obj.comments.count()

    def _has_interaction(self, obj, interaction_type: str) -> bool:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return ReelInteraction.objects.filter(
            reel_id=obj.pk,
            user=request.user,
            type=interaction_type,
        ).exists()

    def get_is_liked(self, obj):
        return self._has_interaction(obj, ReelInteraction.Type.LIKE)

    def get_is_bookmarked(self, obj):
        return self._has_interaction(obj, ReelInteraction.Type.BOOKMARK)

    def get_has_added_to_cart(self, obj):
        return self._has_interaction(obj, ReelInteraction.Type.CART_ADD)

    def get_is_sponsored(self, obj):
        if not obj.is_sponsored:
            return False
        exp = obj.boost_expires_at
        if exp is None:
            return True
        return exp > timezone.now()


class ReelCommentSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = ReelComment
        fields = [
            "id",
            "reel",
            "user",
            "user_name",
            "body",
            "parent",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "reel", "user", "user_name", "created_at", "updated_at"]

    def get_user_name(self, obj):
        return getattr(obj.user, "name", "") or getattr(obj.user, "phone", "User")


class CartItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)
    subtotal = serializers.SerializerMethodField()

    class Meta:
        model = CartItem
        fields = ["id", "product", "quantity", "subtotal", "created_at", "updated_at"]

    def get_subtotal(self, obj):
        fo = self.context.get("flash_overrides")
        unit = storefront_unit_price(obj.product, flash_overrides=fo)
        return float(unit * obj.quantity)


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    total_items = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ["id", "items", "total_items", "total_amount", "created_at", "updated_at"]

    def get_total_items(self, obj):
        return sum(i.quantity for i in obj.items.all())

    def get_total_amount(self, obj):
        fo = self.context.get("flash_overrides")
        total = 0
        for item in obj.items.select_related("product").all():
            unit = storefront_unit_price(item.product, flash_overrides=fo)
            total += float(unit * item.quantity)
        return total


class ProductWishlistSerializer(serializers.ModelSerializer):
    product = ProductSerializer(read_only=True)

    class Meta:
        model = ProductWishlist
        fields = ["id", "product", "created_at"]


class FamilyWalletCategorySerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = FamilyWalletCategory
        fields = [
            "id",
            "name",
            "sort_order",
            "allowed_member_roles",
            "created_at",
            "image_url",
        ]

    def get_image_url(self, obj: FamilyWalletCategory) -> str:
        request = self.context.get("request")
        if not obj.image or not request:
            return ""
        from core.views.admin.admin_write_utils import absolute_media_url

        return absolute_media_url(request, obj.image)


class FamilyJoinRequestReadSerializer(serializers.ModelSerializer):
    requested_by_name = serializers.SerializerMethodField()

    class Meta:
        model = FamilyJoinRequest
        fields = [
            "id",
            "invite_id",
            "join_link_id",
            "source",
            "requested_by_name",
            "name",
            "email",
            "phone",
            "role",
            "age",
            "status",
            "applicant_note",
            "created_at",
            "reviewed_at",
        ]

    def get_requested_by_name(self, obj: FamilyJoinRequest) -> str:
        if obj.requested_by_id:
            return (obj.requested_by.name or "").strip() or ""
        return ""


class PublicFamilyPortalJoinSubmitSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=15)
    applicant_note = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=2000
    )


class PortalProductRestrictionReadSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    category_slug = serializers.CharField(source="category.slug", read_only=True)

    class Meta:
        model = ProductRestriction
        fields = [
            "id",
            "category_id",
            "category_name",
            "category_slug",
            "is_blocked",
            "requires_approval",
            "max_price",
        ]


class PortalProductRestrictionRuleItemSerializer(serializers.Serializer):
    category_id = serializers.IntegerField()
    is_blocked = serializers.BooleanField(default=False)
    requires_approval = serializers.BooleanField(default=False)
    max_price = serializers.DecimalField(
        max_digits=8, decimal_places=2, allow_null=True, required=False
    )


class PortalProductRestrictionsReplaceSerializer(serializers.Serializer):
    rules = serializers.ListField(child=PortalProductRestrictionRuleItemSerializer())


class PortalProductRestrictionUpsertSerializer(serializers.Serializer):
    category_id = serializers.IntegerField()
    is_blocked = serializers.BooleanField(default=False)
    requires_approval = serializers.BooleanField(default=False)
    max_price = serializers.DecimalField(
        max_digits=8, decimal_places=2, allow_null=True, required=False
    )


class PortalFamilyAddMemberSerializer(serializers.Serializer):
    """Single member invite (join request + invite) for the caller's primary family group."""

    name = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=15)
    role = serializers.ChoiceField(
        choices=FamilyJoinRequest.Role.choices,
        required=False,
        default=FamilyJoinRequest.Role.CHILD,
    )
    age = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=120)
    invite_method = serializers.ChoiceField(choices=["link", "phone"], default="phone")
    spending_limit = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0
    )
    initial_balance = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0
    )


class PortalFamilyAddMembersBatchSerializer(serializers.Serializer):
    """Batch invites with shared wallet defaults."""

    members = serializers.ListField(
        child=serializers.DictField(),
        min_length=1,
        max_length=50,
    )
    invite_method = serializers.ChoiceField(choices=["link", "phone"], default="phone")
    spending_limit = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0
    )
    initial_balance = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0
    )

    def validate_members(self, value):
        child = PortalFamilyAddMemberSerializer(many=True, data=value)
        child.is_valid(raise_exception=True)
        return child.validated_data


class PortalFamilyMemberRolePatchSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=FamilyMember.Role.choices)


class PortalFamilyMemberPatchSerializer(serializers.Serializer):
    """Partial update for portal family member (at least one field required)."""

    role = serializers.ChoiceField(choices=FamilyMember.Role.choices, required=False)
    spending_limit_daily = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        required=False,
        validators=[MinValueValidator(Decimal("0"))],
    )
    spending_limit_weekly = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        required=False,
        validators=[MinValueValidator(Decimal("0"))],
    )
    spending_limit_monthly = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        required=False,
        validators=[MinValueValidator(Decimal("0"))],
    )
    status = serializers.ChoiceField(choices=["active", "frozen"], required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field is required.")
        keys = (
            "spending_limit_daily",
            "spending_limit_weekly",
            "spending_limit_monthly",
        )
        if all(k in attrs for k in keys):
            d, w, m = (
                attrs["spending_limit_daily"],
                attrs["spending_limit_weekly"],
                attrs["spending_limit_monthly"],
            )
            if d > w or w > m or d > m:
                raise serializers.ValidationError(
                    "Spending limits must satisfy daily ≤ weekly ≤ monthly."
                )
        return attrs


class PortalAutoApprovalRuleReadSerializer(serializers.ModelSerializer):
    category_id = serializers.SerializerMethodField()
    category_name = serializers.SerializerMethodField()

    class Meta:
        model = AutoApprovalRule
        fields = [
            "id",
            "name",
            "description",
            "category_id",
            "category_name",
            "max_amount",
            "is_enabled",
        ]

    def get_category_id(self, obj):
        return obj.category_id

    def get_category_name(self, obj):
        if obj.category_id:
            return obj.category.name
        return None


class PortalAutoApprovalRuleCreateSerializer(serializers.ModelSerializer):
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), allow_null=True, required=False
    )

    class Meta:
        model = AutoApprovalRule
        fields = ["name", "description", "category", "max_amount", "is_enabled"]


class PortalAutoApprovalRulePatchSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    description = serializers.CharField(allow_blank=True, required=False)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), allow_null=True, required=False
    )
    max_amount = serializers.DecimalField(
        max_digits=8, decimal_places=2, allow_null=True, required=False
    )
    is_enabled = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("At least one field is required.")
        return attrs


class PortalFamilyJoinRequestPatchSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["approve", "reject"])


class PortalFamilyJoinShareLinkCreateSerializer(serializers.Serializer):
    title = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    welcome_message = serializers.CharField(
        required=False, allow_blank=True, max_length=2000, default=""
    )
    default_role = serializers.ChoiceField(
        choices=["child", "spouse", "guest", "manager"],
        required=False,
        default="child",
    )
    expires_in_days = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=90
    )


class BlogPostListSerializer(serializers.ModelSerializer):
    cover_image_url = serializers.SerializerMethodField()
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = BlogPost
        fields = [
            "id",
            "title",
            "slug",
            "excerpt",
            "cover_image_url",
            "author_name",
            "published_at",
            "seo_title",
            "seo_description",
        ]

    def get_cover_image_url(self, obj):
        if not obj.cover_image:
            return ""
        request = self.context.get("request")
        if request:
            return request.build_absolute_uri(obj.cover_image.url)
        return obj.cover_image.url

    def get_author_name(self, obj):
        if obj.author_id and obj.author:
            return getattr(obj.author, "name", "") or ""
        return ""


class BlogPostDetailSerializer(BlogPostListSerializer):
    class Meta(BlogPostListSerializer.Meta):
        fields = [
            "id",
            "title",
            "slug",
            "excerpt",
            "content",
            "cover_image_url",
            "author_name",
            "published_at",
            "seo_title",
            "seo_description",
        ]

