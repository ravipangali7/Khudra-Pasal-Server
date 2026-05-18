from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.db.models import (
    Case,
    Count,
    DecimalField,
    ExpressionWrapper,
    F,
    FloatField,
    Prefetch,
    Q,
    Value,
    When,
)
from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from xml.sax.saxutils import escape
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from core.models import (
    Banner,
    Brand,
    BlogPost,
    Cart,
    CartItem,
    Category,
    CMSPage,
    FamilyInvite,
    FlashDeal,
    Order,
    OrderItem,
    Product,
    ProductImage,
    ProductReview,
    ProductWishlist,
    Reel,
    ReelComment,
    ReelInteraction,
    ShippingMethod,
    ShippingSettings,
    ShippingZone,
    SiteSettings,
    Vendor,
)
from core.services import reel_service
from core.services import reel_feed_service
from core.services.reels_site_settings import get_reels_feed_mix, get_reels_site_config
from core.services.product_pricing import (
    flash_deal_ids_for_products,
    flash_override_prices_for_products,
    product_effective_price_case,
)
from core.services.storefront_coupon_hints import coupon_hints_for_product_ids
from core.services.child_shopping_guard import validate_child_may_purchase_product
from core.services.storefront_product_visibility import (
    product_is_storefront_purchasable,
    storefront_active_product_q,
)
from core.services.portal_checkout_pricing import checkout_items_weight_kg
from core.services.site_settings_policy import storefront_orders_gate_response
from core.services.shipping_quote import compute_shipping_fee
from core.views.admin.admin_write_utils import absolute_media_url
from core.serializers import (
    BannerSerializer,
    BlogPostDetailSerializer,
    BlogPostListSerializer,
    BrandSerializer,
    CartSerializer,
    CatalogCategorySerializer,
    CategoryTreeSerializer,
    FlashDealSerializer,
    ProductSerializer,
    ProductWishlistSerializer,
    ReelCommentSerializer,
    ReelPublicSerializer,
)
from core.views.vendor.common import vendor_or_error


def _storefront_product_list_context(request, product_ids: list[int]) -> dict:
    now = timezone.now()
    ids = sorted({int(x) for x in product_ids if x is not None})
    if not ids:
        return {
            "request": request,
            "flash_overrides": {},
            "flash_deal_ids": {},
            "coupon_hints_by_product_id": {},
        }
    fo = flash_override_prices_for_products(ids, now)
    fd = flash_deal_ids_for_products(ids, now)
    hints = coupon_hints_for_product_ids(ids)
    return {
        "request": request,
        "flash_overrides": fo,
        "flash_deal_ids": fd,
        "coupon_hints_by_product_id": hints,
    }


def _cart_serializer_context(request, cart: Cart) -> dict:
    ids = list(cart.items.values_list("product_id", flat=True).distinct())
    ctx = _storefront_product_list_context(request, ids)
    return ctx


class ProductPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200


def _product_image_prefetch(lookup: str = "images"):
    """Prefetch ProductImage rows. Use `lookup="product__images"` from ProductWishlist querysets."""
    return Prefetch(
        lookup,
        queryset=ProductImage.objects.order_by("sort_order", "id"),
    )


def _active_products_queryset():
    """Active products visible on the storefront and purchasable at checkout.

    Marketplace lines require an approved vendor; in-house products have no seller.
    This matches ``resolve_checkout_lines`` in ``portal_checkout_pricing``.
    """
    return (
        Product.objects.filter(storefront_active_product_q())
        .select_related("category", "category__parent", "seller", "brand", "unit")
        .prefetch_related(_product_image_prefetch())
    )


def _sync_cart_items_for_checkout(cart: Cart, user) -> None:
    """Remove lines the customer cannot price or purchase (mirrors portal checkout / quote).

    Prevents a stale website cart from causing repeated 400s on
    ``POST /api/portal/orders/checkout-quote/`` when products, vendors, or child rules change.
    """
    product_ids = list(cart.items.values_list("product_id", flat=True).distinct())
    if not product_ids:
        return
    active_ids = set(
        _active_products_queryset()
        .filter(pk__in=product_ids)
        .values_list("pk", flat=True)
    )
    for item in list(
        cart.items.select_related(
            "product",
            "product__category",
            "product__category__parent",
            "product__seller",
        ).all()
    ):
        if item.product_id not in active_ids:
            item.delete()
            continue
        try:
            validate_child_may_purchase_product(user, item.product)
        except ValueError:
            item.delete()


def _subtree_category_ids(root_id: int, children_map: dict[int, list[int]]) -> set[int]:
    out = {root_id}
    stack = [root_id]
    while stack:
        pid = stack.pop()
        for cid in children_map.get(pid, ()):
            if cid not in out:
                out.add(cid)
                stack.append(cid)
    return out


def _active_category_children_map() -> dict[int, list[int]]:
    rows = Category.objects.filter(status=Category.Status.ACTIVE).values_list("id", "parent_id")
    children_map: dict[int, list[int]] = defaultdict(list)
    for cid, pid in rows:
        if pid:
            children_map[pid].append(cid)
    return children_map


def _active_subtree_ids_for_slug(slug: str) -> set[int]:
    """
    Category PKs for the active category with this slug and all its active descendants.
    Empty set when no active category matches (product list should be empty).
    """
    s = (slug or "").strip()
    if not s:
        return set()
    row = Category.objects.filter(slug=s, status=Category.Status.ACTIVE).values("id").first()
    if not row:
        return set()
    children_map = _active_category_children_map()
    return _subtree_category_ids(row["id"], children_map)


def _apply_product_list_filters(queryset, request):
    category = request.query_params.get("category")
    search = request.query_params.get("search")
    featured = request.query_params.get("featured")
    bestseller = request.query_params.get("bestseller")
    has_discount = (request.query_params.get("has_discount") or "").strip().lower()
    trending = (request.query_params.get("trending") or "").strip().lower()
    brand_raw = (request.query_params.get("brand") or "").strip()
    vendor_slug = (request.query_params.get("vendor_slug") or "").strip()

    if vendor_slug:
        queryset = queryset.filter(seller__store_slug=vendor_slug)

    if brand_raw:
        try:
            brand_id = int(brand_raw)
            if brand_id > 0:
                queryset = queryset.filter(brand_id=brand_id)
            else:
                queryset = queryset.none()
        except ValueError:
            queryset = queryset.none()

    if category:
        subtree_ids = _active_subtree_ids_for_slug(category)
        if not subtree_ids:
            queryset = queryset.none()
        else:
            queryset = queryset.filter(category_id__in=subtree_ids)
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(description__icontains=search)
            | Q(short_description__icontains=search)
        )
    if featured == "true":
        queryset = queryset.filter(is_featured=True)
    if bestseller == "true":
        queryset = queryset.filter(is_bestseller=True)
    if has_discount == "true":
        queryset = queryset.annotate(_eff=product_effective_price_case()).filter(_eff__lt=F("price"))
    if trending == "true":
        queryset = queryset.filter(Q(is_bestseller=True) | Q(rating__gte=4.5))
    return queryset


def _apply_product_ordering(queryset, request):
    ordering = (request.query_params.get("ordering") or "-created_at").strip()
    allowed = {"-created_at", "-rating", "-discount_percent"}
    if ordering not in allowed:
        ordering = "-created_at"
    if ordering == "-discount_percent":
        queryset = (
            queryset.annotate(_eff=product_effective_price_case())
            .filter(_eff__lt=F("price"))
            .annotate(
                discount_percent=ExpressionWrapper(
                    (F("price") - F("_eff")) * 100.0 / F("price"),
                    output_field=DecimalField(max_digits=8, decimal_places=3),
                )
            )
        )
        return queryset.order_by("-discount_percent", "-created_at")
    return queryset.order_by(ordering)


@api_view(["GET"])
@permission_classes([AllowAny])
def search_placeholders_list(request):
    site = SiteSettings.load()
    raw = site.search_placeholders or []
    if not isinstance(raw, list):
        raw = []
    out = [str(x).strip() for x in raw if str(x).strip()]
    return Response(out)


def _public_social_links_from_site(site: SiteSettings) -> dict[str, str]:
    raw = site.admin_extras or {}
    social = raw.get("social") if isinstance(raw, dict) else None
    if not isinstance(social, dict):
        social = {}
    keys = ("facebook", "instagram", "twitter", "youtube", "tiktok")
    return {k: str(social.get(k) or "").strip() for k in keys}


def _public_chabot_script_from_site(site: SiteSettings) -> str:
    raw = site.admin_extras or {}
    chatbot = raw.get("chatbot") if isinstance(raw, dict) else None
    if not isinstance(chatbot, dict):
        return ""
    return str(chatbot.get("chabot_script") or "").strip()


def _public_app_promotion_banner_from_site(site: SiteSettings):
    from core.services.app_promotion_banner import public_app_promotion_banner_from_site

    return public_app_promotion_banner_from_site(site)


@api_view(["GET"])
@permission_classes([AllowAny])
def firebase_messaging_config(request):
    """Public Firebase web push config for the SPA (VAPID key for token registration)."""
    import os

    from core.services.fcm_push_service import _resolve_credentials_path

    vapid_key = (getattr(settings, "FIREBASE_WEB_VAPID_KEY", "") or "").strip()
    cred_path = _resolve_credentials_path()
    return Response(
        {
            "vapid_key": vapid_key,
            "firebase_configured": bool(cred_path and os.path.isfile(cred_path)),
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def store_info(request):
    site = SiteSettings.load()
    logo_url = absolute_media_url(request, site.site_logo) if site.site_logo else ""
    favicon_url = absolute_media_url(request, site.site_favicon) if site.site_favicon else ""
    cover_url = absolute_media_url(request, site.cover_image) if site.cover_image else ""
    rcfg = get_reels_site_config()
    return Response(
        {
            "site_name": site.site_name,
            "site_description": site.site_description,
            "site_favicon_url": favicon_url,
            "cover_image_url": cover_url or logo_url,
            "meta_keywords": site.meta_keywords or "",
            "site_email": site.site_email,
            "phone": site.phone,
            "address": site.address,
            "currency": site.currency,
            "footer_text": site.footer_text,
            "site_logo_url": logo_url,
            "maintenance_mode": site.maintenance_mode,
            "temporary_shop_close": site.temporary_shop_close,
            "new_registrations": site.new_registrations,
            "kyc_required": site.kyc_required,
            "pos_enabled": site.pos_enabled,
            "social_links": _public_social_links_from_site(site),
            "chabot_script": _public_chabot_script_from_site(site),
            "app_promotion_banner": _public_app_promotion_banner_from_site(site),
            "reels_boost": {
                "standardMultiplier": rcfg["standardMultiplier"],
                "premiumMultiplier": rcfg["premiumMultiplier"],
                "megaMultiplier": rcfg["megaMultiplier"],
                "feedAlgorithm": rcfg["feedAlgorithm"],
                "feedMix": rcfg["feedMix"],
            },
        }
    )


def _decimal_from_request(v, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return default


@api_view(["GET"])
@permission_classes([AllowAny])
def shipping_zones_list(request):
    rows = ShippingZone.objects.filter(status=ShippingZone.Status.ACTIVE).order_by("name")
    return Response(
        [
            {
                "id": str(z.pk),
                "name": z.name,
                "areas": z.areas or "",
            }
            for z in rows
        ]
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def shipping_methods_list(request):
    rows = ShippingMethod.objects.filter(status=ShippingMethod.Status.ACTIVE).order_by("name")
    return Response(
        [
            {
                "id": str(m.pk),
                "name": m.name,
                "type": m.type,
            }
            for m in rows
        ]
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def shipping_quote(request):
    gate = storefront_orders_gate_response()
    if gate:
        return gate
    sh = ShippingSettings.load()
    raw_zone = request.data.get("zone_id") or request.data.get("zone")
    if not raw_zone and sh.default_zone_id:
        raw_zone = sh.default_zone_id
    zone = ShippingZone.objects.filter(pk=raw_zone).first() if raw_zone else None
    if not zone or zone.status != ShippingZone.Status.ACTIVE:
        return Response(
            {"detail": "Invalid or inactive zone_id. Choose a delivery zone."},
            status=400,
        )
    order_total = _decimal_from_request(request.data.get("order_total"), Decimal("0"))
    if order_total < 0:
        order_total = Decimal("0")

    raw_w = request.data.get("weight_kg")
    weight_kg: float
    if raw_w is not None and str(raw_w).strip() != "":
        try:
            weight_kg = float(raw_w)
        except (TypeError, ValueError):
            items_w = checkout_items_weight_kg(request.data.get("items"))
            if items_w > 0:
                weight_kg = items_w
            else:
                weight_kg = float(sh.default_checkout_weight_kg)
    else:
        items_w = checkout_items_weight_kg(request.data.get("items"))
        if items_w > 0:
            weight_kg = items_w
        else:
            weight_kg = float(sh.default_checkout_weight_kg)
    weight_kg = max(0.0, min(500.0, weight_kg))

    raw_mid = request.data.get("method_id") or request.data.get("shipping_method_id")
    method: ShippingMethod | None = None
    if raw_mid is not None and str(raw_mid).strip() != "":
        method = ShippingMethod.objects.filter(
            pk=raw_mid, status=ShippingMethod.Status.ACTIVE
        ).first()
        if not method:
            return Response(
                {"detail": "Invalid or inactive shipping_method_id."},
                status=400,
            )

    fee, breakdown = compute_shipping_fee(
        sh,
        zone,
        order_total=order_total,
        weight_kg=weight_kg,
        method=method,
    )
    customer_fee = Decimal("0") if sh.seller_pays_shipping else fee
    return Response(
        {
            "fee": float(customer_fee),
            "currency": "NPR",
            "zone": {"id": str(zone.pk), "name": zone.name},
            "weight_kg": weight_kg,
            "shipping_method_id": str(method.pk) if method else None,
            "breakdown": breakdown,
            "seller_pays_shipping": sh.seller_pays_shipping,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def brands_list(request):
    queryset = Brand.objects.filter(status=Brand.Status.ACTIVE).order_by("name")
    serializer = BrandSerializer(queryset, many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def categories_list(request):
    queryset = Category.objects.filter(parent__isnull=True, status=Category.Status.ACTIVE).order_by("sort_order", "name")
    serializer = CategoryTreeSerializer(queryset, many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def catalog_list(request):
    """Root categories with up to `per_category` active products each (full subtree per root)."""
    raw_per = request.query_params.get("per_category")
    try:
        per_n = int(raw_per) if raw_per is not None else 12
    except ValueError:
        per_n = 12
    per_n = max(1, min(per_n, 48))

    rows = Category.objects.filter(status=Category.Status.ACTIVE).values_list("id", "parent_id")
    children_map: dict[int, list[int]] = defaultdict(list)
    for cid, pid in rows:
        if pid:
            children_map[pid].append(cid)

    roots = Category.objects.filter(parent__isnull=True, status=Category.Status.ACTIVE).order_by(
        "sort_order", "name"
    )
    base = _active_products_queryset()
    catalog_map: dict[int, list] = {}
    for root in roots:
        ids = _subtree_category_ids(root.id, children_map)
        prods = list(base.filter(category_id__in=ids).order_by("-is_featured", "-created_at")[:per_n])
        catalog_map[root.id] = prods

    all_pids: list[int] = []
    for _rid, plist in catalog_map.items():
        for p in plist:
            all_pids.append(p.pk)
    list_ctx = _storefront_product_list_context(request, all_pids)
    list_ctx["catalog_products_by_root_id"] = catalog_map
    serializer = CatalogCategorySerializer(roots, many=True, context=list_ctx)
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def products_list(request):
    """
    Storefront product list filters:
    - category=<slug>
    - brand=<integer_pk> (active brand)
    - vendor_slug=<store_slug> (approved vendor storefront)
    - search=<term>
    - featured=true
    - bestseller=true
    - has_discount=true
    - trending=true
    - ordering=-created_at|-rating|-discount_percent
    """
    queryset = _active_products_queryset()
    queryset = _apply_product_list_filters(queryset, request)
    queryset = _apply_product_ordering(queryset, request)

    paginator = ProductPagination()
    page = paginator.paginate_queryset(queryset, request)
    pids = [p.pk for p in page] if page is not None else []
    serializer = ProductSerializer(
        page, many=True, context=_storefront_product_list_context(request, pids)
    )
    return paginator.get_paginated_response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def products_all_vendors_list(request):
    """Active products sold by approved vendors only (marketplace). Supports vendor_slug filter."""
    queryset = _active_products_queryset().filter(
        seller__isnull=False,
        seller__status=Vendor.Status.APPROVED,
    )
    queryset = _apply_product_list_filters(queryset, request)
    queryset = _apply_product_ordering(queryset, request)

    paginator = ProductPagination()
    page = paginator.paginate_queryset(queryset, request)
    pids = [p.pk for p in page] if page is not None else []
    serializer = ProductSerializer(
        page, many=True, context=_storefront_product_list_context(request, pids)
    )
    return paginator.get_paginated_response(serializer.data)


def _user_has_delivered_paid_purchase(user, product: Product) -> bool:
    """
    Eligible to review: delivered line item and either paid online/wallet, or COD (pending until
    settlement) — delivered implies collection for COD.
    """
    return OrderItem.objects.filter(
        product=product,
        order__customer=user,
        order__status=Order.Status.DELIVERED,
    ).filter(
        Q(order__payment_status=Order.PaymentStatus.PAID)
        | Q(
            order__payment_method=Order.PaymentMethod.COD,
            order__payment_status=Order.PaymentStatus.PENDING,
        )
    ).exists()


@api_view(["GET"])
@permission_classes([AllowAny])
def product_detail(request, identifier):
    queryset = _active_products_queryset()
    product = queryset.filter(slug=identifier).first() or queryset.filter(pk=identifier).first()

    if not product:
        return Response({"detail": "Product not found."}, status=404)

    serializer = ProductSerializer(
        product, context=_storefront_product_list_context(request, [product.pk])
    )
    data = dict(serializer.data)
    if request.user.is_authenticated:
        has_purchase = _user_has_delivered_paid_purchase(request.user, product)
        has_review = ProductReview.objects.filter(
            product=product, customer=request.user
        ).exists()
        data["can_submit_review"] = has_purchase and not has_review
    return Response(data)


@api_view(["GET", "POST"])
@authentication_classes([JWTAuthentication, TokenAuthentication, SessionAuthentication])
@permission_classes([AllowAny])
def product_reviews_list(request, identifier):
    queryset = Product.objects.filter(status=Product.Status.ACTIVE)
    product = queryset.filter(slug=identifier).first() or queryset.filter(pk=identifier).first()
    if not product:
        return Response({"detail": "Product not found."}, status=404)
    if request.method == "GET":
        qs = (
            ProductReview.objects.filter(product=product, status=ProductReview.Status.APPROVED)
            .select_related("customer")
            .order_by("-created_at")[:50]
        )
        data = [
            {
                "id": r.pk,
                "name": r.customer.name,
                "rating": r.rating,
                "comment": r.comment or "",
                "date": r.created_at.isoformat(),
            }
            for r in qs
        ]
        return Response(data)

    if not request.user.is_authenticated:
        return Response({"detail": "Authentication required."}, status=401)
    if ProductReview.objects.filter(product=product, customer=request.user).exists():
        return Response({"detail": "You have already submitted a review for this product."}, status=400)
    if not _user_has_delivered_paid_purchase(request.user, product):
        return Response(
            {
                "detail": "You can only review products from a delivered order (paid or COD on delivery).",
            },
            status=400,
        )
    try:
        rating = int(request.data.get("rating"))
    except (TypeError, ValueError):
        return Response({"detail": "rating must be an integer 1–5."}, status=400)
    if rating < 1 or rating > 5:
        return Response({"detail": "rating must be between 1 and 5."}, status=400)
    comment = (request.data.get("comment") or "").strip()[:5000]
    row = ProductReview.objects.create(
        product=product,
        customer=request.user,
        rating=rating,
        comment=comment,
        status=ProductReview.Status.PENDING,
    )
    return Response({"id": row.pk, "status": row.status}, status=201)


@api_view(["GET"])
@permission_classes([AllowAny])
def cms_pages_public_list(request):
    qs = CMSPage.objects.filter(status=CMSPage.Status.PUBLISHED).order_by("title")
    return Response([{"title": p.title, "slug": p.slug} for p in qs])


@api_view(["GET"])
@permission_classes([AllowAny])
def cms_page_public(request, slug):
    row = CMSPage.objects.filter(slug=slug, status=CMSPage.Status.PUBLISHED).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    from core.seo.api_aliases import with_seo_aliases

    payload = {
        "title": row.title,
        "slug": row.slug,
        "content": row.content,
        "seo_title": row.seo_title,
        "seo_description": row.seo_description,
        "last_updated": row.last_updated.isoformat() if row.last_updated else "",
        "featured_image_url": absolute_media_url(request, row.featured_image)
        if row.featured_image
        else "",
    }
    payload = with_seo_aliases(payload)
    payload["featuredImage"] = payload.get("featured_image_url") or ""
    return Response(payload)


@api_view(["GET"])
@permission_classes([AllowAny])
def blog_posts_list(request):
    qs = (
        BlogPost.objects.filter(status=BlogPost.Status.PUBLISHED)
        .select_related("author")
        .order_by("-published_at", "-created_at")
    )
    q = (request.query_params.get("search") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(excerpt__icontains=q) | Q(content__icontains=q))
    serializer = BlogPostListSerializer(qs[:48], many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def blog_post_public(request, slug):
    post = get_object_or_404(
        BlogPost.objects.select_related("author"),
        slug=slug,
        status=BlogPost.Status.PUBLISHED,
    )
    return Response(BlogPostDetailSerializer(post, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def sitemap_xml(request):
    """Legacy path — same XML as /api/meta/sitemap.xml."""
    from core.seo.sitemap import build_sitemap_xml

    return HttpResponse(build_sitemap_xml(), content_type="application/xml; charset=utf-8")


@api_view(["GET"])
@permission_classes([AllowAny])
def banners_list(request):
    placement = request.query_params.get("placement")
    queryset = Banner.objects.filter(status=Banner.Status.ACTIVE)
    if placement:
        queryset = queryset.filter(placement=placement)
    queryset = queryset.order_by("sort_order", "-created_at")
    serializer = BannerSerializer(queryset, many=True, context={"request": request})
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def deals_list(request):
    queryset = FlashDeal.objects.filter(status=FlashDeal.Status.ACTIVE).prefetch_related(
        "deal_products__product__images"
    ).order_by("-priority", "-start_at")
    pids: list[int] = []
    for d in queryset:
        for dp in d.deal_products.all():
            pids.append(dp.product_id)
    ctx = _storefront_product_list_context(request, pids)
    serializer = FlashDealSerializer(queryset, many=True, context=ctx)
    return Response(serializer.data)


def _public_reels_base_queryset():
    return (
        Reel.objects.filter(status__in=[Reel.Status.ACTIVE, Reel.Status.APPROVED])
        .select_related("vendor", "product", "product__category", "product__seller")
        .prefetch_related("comments")
    )


def _parse_vendor_ids_param(request):
    """Comma-separated vendor PKs, e.g. ?vendor_ids=1,2,3. Invalid tokens skipped."""
    raw = request.query_params.get("vendor_ids")
    if not raw or not str(raw).strip():
        return None
    ids = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids if ids else None


def public_reels_queryset_for_request(request):
    """
    Narrow the public feed:
    - vendor_ids=1,2,3 — filter vendor_id__in (takes precedence over vendor_id / vendor_slug)
    - vendor_id — single vendor
    - vendor_slug — single store slug
    """
    qs = _public_reels_base_queryset()
    vendor_ids = _parse_vendor_ids_param(request)
    if vendor_ids is not None:
        return qs.filter(vendor_id__in=vendor_ids)
    vendor_id = request.query_params.get("vendor_id")
    vendor_slug = request.query_params.get("vendor_slug")
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    if vendor_slug:
        qs = qs.filter(vendor__store_slug=vendor_slug)
    return qs


def _apply_only_direct_mp4_param(qs, request):
    raw = str(request.query_params.get("only_direct_mp4", "")).lower()
    if raw in ("1", "true", "yes"):
        return qs.filter(platform=Reel.Platform.DIRECT_MP4)
    return qs


def order_public_reels(queryset, tab: str):
    """
    Active boosts (non-expired window) rank by tier weight from site settings; expired
    ``is_sponsored`` rows do not receive boost ordering. Tab selects tie-breakers;
    trending also respects admin ``feedAlgorithm`` (chronological / popularity / mixed).
    """
    now = timezone.now()
    cfg = get_reels_site_config()
    std = float(cfg["standardMultiplier"])
    prem = float(cfg["premiumMultiplier"])
    mega = float(cfg["megaMultiplier"])
    algo = cfg["feedAlgorithm"]
    sponsored_ok = Q(is_sponsored=True) & (
        Q(boost_expires_at__isnull=True) | Q(boost_expires_at__gt=now)
    )
    boost_case = Case(
        When(sponsored_ok & Q(boost_tier=Reel.BoostTier.MEGA), then=Value(mega)),
        When(sponsored_ok & Q(boost_tier=Reel.BoostTier.PREMIUM), then=Value(prem)),
        When(sponsored_ok & Q(boost_tier=Reel.BoostTier.STANDARD), then=Value(std)),
        When(sponsored_ok, then=Value(std)),
        default=Value(0.0),
        output_field=FloatField(),
    )
    qs = queryset.annotate(_reel_boost_score=boost_case)
    t = (tab or "trending").lower()
    if t == "popular":
        return qs.order_by("-_reel_boost_score", "-views", "-likes", "-created_at")
    if t == "new":
        return qs.order_by("-_reel_boost_score", "-created_at")
    if algo == "chronological":
        return qs.order_by("-_reel_boost_score", "-created_at")
    if algo == "popularity":
        return qs.order_by("-_reel_boost_score", "-views", "-likes", "-created_at")
    return qs.order_by("-_reel_boost_score", "-likes", "-views", "-shares", "-created_at")


def annotate_reels_comments(queryset):
    return queryset.annotate(comments_count=Count("comments", distinct=True))


def _public_reels_list_response(request, qs, *, tab: str):
    """
    Blended slot feed when feedAlgorithm=personalized or ?feed=blended;
    otherwise legacy boost + tab ordering.
    """
    cfg = get_reels_site_config()
    if reel_feed_service.should_use_blended_feed(request, cfg["feedAlgorithm"]):
        audience = reel_feed_service.detect_audience(request)
        paginator = ProductPagination()
        page_size = paginator.get_page_size(request) or paginator.page_size
        try:
            page_num = int(request.query_params.get(paginator.page_query_param, 1))
        except (TypeError, ValueError):
            page_num = 1
        user = request.user if request.user.is_authenticated else None
        feed_seed = (
            request.query_params.get("feed_seed")
            or request.query_params.get("shuffle")
            or ""
        ).strip()[:64]
        reels, has_more = reel_feed_service.build_blended_feed_page(
            qs,
            user=user,
            audience=audience,
            page=page_num,
            page_size=page_size,
            mix=get_reels_feed_mix(audience),
            feed_seed=feed_seed or None,
        )
        if reels:
            hydrated = annotate_reels_comments(
                Reel.objects.filter(pk__in=[r.pk for r in reels])
                .select_related("vendor", "product", "product__category", "product__seller")
                .prefetch_related("comments")
            )
            by_id = {r.pk: r for r in hydrated}
            ordered = [by_id[r.pk] for r in reels if r.pk in by_id]
        else:
            ordered = []
        serializer = ReelPublicSerializer(ordered, many=True, context={"request": request})
        next_page = page_num + 1 if has_more else None
        prev_page = page_num - 1 if page_num > 1 else None

        def _page_url(p: int | None):
            if p is None:
                return None
            q = request.query_params.copy()
            q[paginator.page_query_param] = str(p)
            return request.build_absolute_uri(f"{request.path}?{q.urlencode()}")

        return Response(
            {
                "count": None,
                "next": _page_url(next_page),
                "previous": _page_url(prev_page),
                "results": serializer.data,
                "feed_meta": {
                    "mode": "blended",
                    "audience": audience,
                    "tab": tab,
                    "slot_mix": get_reels_feed_mix(audience),
                    **reel_feed_service.get_feed_ranking_meta(),
                },
            }
        )

    queryset = annotate_reels_comments(order_public_reels(qs, tab))
    paginator = ProductPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = ReelPublicSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


def _reel_public_statuses():
    return [Reel.Status.ACTIVE, Reel.Status.APPROVED]


def _reel_for_user_interaction(request, pk: int):
    """
    Reel eligible for interactions/comments if it is public, or the user owns the vendor
    that uploaded it (preview of pending/draft reels in the portal).
    """
    reel = (
        Reel.objects.filter(pk=pk)
        .select_related("vendor")
        .first()
    )
    if not reel:
        return None
    if reel.status in _reel_public_statuses():
        return reel
    if request.user.is_authenticated and reel.vendor.user_id == request.user.id:
        return reel
    return None


@api_view(["GET"])
@permission_classes([AllowAny])
def reels_list(request):
    tab = request.query_params.get("tab") or "trending"
    qs = public_reels_queryset_for_request(request)
    qs = _apply_only_direct_mp4_param(qs, request)
    return _public_reels_list_response(request, qs, tab=tab)


@api_view(["GET"])
@permission_classes([AllowAny])
def reels_trending_list(request):
    """Trending reels; optional vendor_ids / vendor_id / vendor_slug narrow the set (same as /website/reels/)."""
    qs = public_reels_queryset_for_request(request)
    qs = _apply_only_direct_mp4_param(qs, request)
    return _public_reels_list_response(request, qs, tab="trending")


def _abs_file_url(request, file_field):
    if not file_field:
        return ""
    return request.build_absolute_uri(file_field.url)


@api_view(["GET"])
@permission_classes([AllowAny])
def reels_vendors_directory(request):
    """Vendors with at least one active/approved public reel (for grouped discovery)."""
    active = [Reel.Status.ACTIVE, Reel.Status.APPROVED]
    qs = (
        Vendor.objects.annotate(
            public_reel_count=Count("reels", filter=Q(reels__status__in=active)),
        )
        .filter(public_reel_count__gt=0)
        .order_by("store_name")
    )
    paginator = ProductPagination()
    page = paginator.paginate_queryset(qs, request)
    results = []
    for v in page:
        results.append(
            {
                "id": v.id,
                "store_name": v.store_name,
                "store_slug": v.store_slug,
                "is_verified": v.is_verified,
                "logo_url": _abs_file_url(request, v.logo),
                "public_reel_count": v.public_reel_count,
            }
        )
    return paginator.get_paginated_response(results)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([AllowAny])
def reels_by_vendor_list(request, vendor_id: int):
    """
    Paginated reels for one vendor.
    - Authenticated seller: only when vendor_id matches their store — all statuses (dashboard parity).
    - Other authenticated sellers: 403 for another vendor_id.
    - Anonymous or non-vendor users: public active/approved reels only (tab ordering).
    """
    if not Vendor.objects.filter(pk=vendor_id).exists():
        return Response({"detail": "Vendor not found."}, status=404)
    tab = request.query_params.get("tab") or "trending"

    if request.user.is_authenticated:
        user_vendor = getattr(request.user, "vendor_profile", None)
        if user_vendor is not None:
            if user_vendor.pk != vendor_id:
                return Response({"detail": "You may only list your own vendor reels."}, status=403)
            qs = (
                Reel.objects.filter(vendor_id=vendor_id)
                .select_related("vendor", "product", "product__category")
                .prefetch_related("comments")
            )
            queryset = annotate_reels_comments(order_public_reels(qs, tab))
            paginator = ProductPagination()
            page = paginator.paginate_queryset(queryset, request)
            serializer = ReelPublicSerializer(page, many=True, context={"request": request})
            return paginator.get_paginated_response(serializer.data)

    qs = _public_reels_base_queryset().filter(vendor_id=vendor_id)
    qs = _apply_only_direct_mp4_param(qs, request)
    queryset = annotate_reels_comments(order_public_reels(qs, tab))
    paginator = ProductPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = ReelPublicSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def reels_dashboard(request):
    """Authenticated vendor: own reels for dashboard (engagement + recency)."""
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = (
        Reel.objects.filter(vendor=vendor)
        .select_related("vendor", "product", "product__category")
        .prefetch_related("comments")
    )
    queryset = annotate_reels_comments(qs.order_by("-likes", "-views", "-shares", "-created_at"))
    paginator = ProductPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = ReelPublicSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def reel_interaction_create(request, pk):
    reel = _reel_for_user_interaction(request, pk)
    if not reel:
        return Response({"detail": "Reel not found."}, status=404)
    interaction_type = (request.data.get("type") or "").strip()
    if interaction_type not in dict(ReelInteraction.Type.choices):
        return Response({"detail": "Invalid interaction type."}, status=400)
    interaction, created = ReelInteraction.objects.get_or_create(
        reel=reel,
        user=request.user,
        type=interaction_type,
    )
    return Response(
        {
            "id": interaction.pk,
            "created": created,
            "type": interaction.type,
        },
        status=201 if created else 200,
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def reel_interaction_delete(request, pk, interaction_type):
    if interaction_type not in dict(ReelInteraction.Type.choices):
        return Response({"detail": "Invalid interaction type."}, status=400)
    reel = _reel_for_user_interaction(request, pk)
    if not reel:
        return Response({"detail": "Reel not found."}, status=404)
    deleted, _ = ReelInteraction.objects.filter(
        reel_id=pk,
        user=request.user,
        type=interaction_type,
    ).delete()
    if deleted:
        reel_service.remove_interaction_counter(reel, interaction_type)
    return Response({"ok": True, "deleted": deleted > 0})


@api_view(["POST"])
@permission_classes([AllowAny])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def reel_view_record(request, pk):
    reel = _reel_for_user_interaction(request, pk)
    if not reel:
        return Response({"detail": "Reel not found."}, status=404)
    watch_seconds = request.data.get("watch_seconds")
    quick_skip = bool(request.data.get("quick_skip"))
    watch_completed = bool(request.data.get("watch_completed"))
    try:
        ws = int(watch_seconds) if watch_seconds is not None else None
    except (TypeError, ValueError):
        ws = None
    if ws is not None and ws < 0:
        ws = 0
    if ws is not None and ws > 3600:
        ws = 3600
    created, views = reel_service.record_unique_view(
        reel,
        request.user,
        request,
        watch_seconds=ws,
        quick_skip=quick_skip or (ws is not None and ws < 2),
        watch_completed=watch_completed,
    )
    return Response({"created": created, "views": views})


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def reel_comments(request, pk):
    reel = _reel_for_user_interaction(request, pk)
    if not reel:
        return Response({"detail": "Reel not found."}, status=404)
    if request.method == "GET":
        qs = ReelComment.objects.filter(reel=reel).select_related("user", "parent").order_by("-created_at")
        data = ReelCommentSerializer(qs, many=True).data
        return Response({"results": data})
    if not request.user.is_authenticated:
        return Response({"detail": "Authentication credentials were not provided."}, status=401)
    body = (request.data.get("body") or "").strip()
    if not body:
        return Response({"detail": "Comment body is required."}, status=400)
    if len(body) > 500:
        return Response({"detail": "Comment is too long."}, status=400)
    parent = None
    parent_id = request.data.get("parent_id")
    if parent_id:
        parent = ReelComment.objects.filter(pk=parent_id, reel=reel).first()
        if not parent:
            return Response({"detail": "Invalid parent_id."}, status=400)
    row = ReelComment.objects.create(
        reel=reel,
        user=request.user,
        body=body,
        parent=parent,
    )
    return Response(ReelCommentSerializer(row).data, status=201)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def cart_detail(request):
    cart, _ = Cart.objects.get_or_create(user=request.user)
    _sync_cart_items_for_checkout(cart, request.user)
    return Response(CartSerializer(cart, context=_cart_serializer_context(request, cart)).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def cart_item_add(request):
    gate = storefront_orders_gate_response()
    if gate:
        return gate
    product_id = request.data.get("product_id")
    quantity = int(request.data.get("quantity") or 1)
    if not product_id:
        return Response({"detail": "product_id is required."}, status=400)
    if quantity < 1:
        return Response({"detail": "quantity must be at least 1."}, status=400)
    product = _active_products_queryset().filter(pk=product_id).first()
    if not product:
        stale = (
            Product.objects.filter(pk=product_id)
            .select_related("seller")
            .first()
        )
        if stale and not product_is_storefront_purchasable(stale):
            return Response(
                {
                    "detail": "This product is not available for purchase. "
                    "It may be inactive or the seller is not approved yet."
                },
                status=400,
            )
        return Response({"detail": "Product not found."}, status=404)
    try:
        validate_child_may_purchase_product(request.user, product)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    cart, _ = Cart.objects.get_or_create(user=request.user)
    item, created = CartItem.objects.get_or_create(
        cart=cart,
        product=product,
        defaults={"quantity": quantity},
    )
    if not created:
        item.quantity += quantity
        item.save(update_fields=["quantity", "updated_at"])
    _sync_cart_items_for_checkout(cart, request.user)
    return Response(
        CartSerializer(cart, context=_cart_serializer_context(request, cart)).data,
        status=201 if created else 200,
    )


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def cart_item_detail(request, pk):
    if request.method == "PATCH":
        gate = storefront_orders_gate_response()
        if gate:
            return gate
    cart = Cart.objects.filter(user=request.user).first()
    if not cart:
        return Response({"detail": "Cart not found."}, status=404)
    item = CartItem.objects.filter(pk=pk, cart=cart).first()
    if not item:
        return Response({"detail": "Cart item not found."}, status=404)
    if request.method == "DELETE":
        item.delete()
        return Response({"ok": True})
    quantity = int(request.data.get("quantity") or 0)
    if quantity < 1:
        return Response({"detail": "quantity must be at least 1."}, status=400)
    try:
        validate_child_may_purchase_product(request.user, item.product)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    item.quantity = quantity
    item.save(update_fields=["quantity", "updated_at"])
    return Response({"id": item.pk, "quantity": item.quantity})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def wishlist_list(request):
    rows = list(
        ProductWishlist.objects.filter(user=request.user)
        .select_related(
            "product",
            "product__category",
            "product__category__parent",
            "product__seller",
            "product__brand",
            "product__unit",
        )
        .prefetch_related(_product_image_prefetch("product__images"))
        .order_by("-created_at")
    )
    pids = [w.product_id for w in rows]
    ctx = _storefront_product_list_context(request, pids)
    return Response(ProductWishlistSerializer(rows, many=True, context=ctx).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def wishlist_item_add(request):
    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"detail": "product_id is required."}, status=400)
    product = _active_products_queryset().filter(pk=product_id).first()
    if not product:
        stale = (
            Product.objects.filter(pk=product_id)
            .select_related("seller")
            .first()
        )
        if stale and not product_is_storefront_purchasable(stale):
            return Response(
                {
                    "detail": "This product is not available for purchase. "
                    "It may be inactive or the seller is not approved yet."
                },
                status=400,
            )
        return Response({"detail": "Product not found."}, status=404)
    item, _ = ProductWishlist.objects.get_or_create(user=request.user, product=product)
    ctx = _storefront_product_list_context(request, [product.pk])
    return Response(ProductWishlistSerializer(item, context=ctx).data, status=201)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def wishlist_item_remove(request, product_id):
    deleted, _ = ProductWishlist.objects.filter(
        user=request.user,
        product_id=product_id,
    ).delete()
    return Response({"ok": True, "deleted": deleted > 0})


@api_view(["GET"])
@permission_classes([AllowAny])
def website_family_invite_meta(request, token):
    """Public metadata for an invite link (no secrets)."""
    inv = FamilyInvite.objects.filter(token=token).select_related("group").first()
    if not inv:
        return Response({"detail": "Not found."}, status=404)
    return Response(
        {
            "group_name": inv.group.name,
            "role": inv.role,
            "expires_at": inv.expires_at.isoformat(),
            "status": inv.status,
        }
    )
