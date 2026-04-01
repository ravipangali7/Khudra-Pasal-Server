from collections import defaultdict

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Prefetch, Q
from django.shortcuts import get_object_or_404
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
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
    SiteSettings,
    Vendor,
)
from core.services import reel_service
from core.views.vendor.common import vendor_or_error
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
    return (
        Product.objects.filter(status=Product.Status.ACTIVE)
        .select_related("category", "category__parent", "seller", "brand", "unit")
        .prefetch_related(_product_image_prefetch())
    )


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
        queryset = queryset.filter(discount_price__isnull=False, discount_price__lt=F("price"))
    if trending == "true":
        queryset = queryset.filter(Q(is_bestseller=True) | Q(rating__gte=4.5))
    return queryset


def _apply_product_ordering(queryset, request):
    ordering = (request.query_params.get("ordering") or "-created_at").strip()
    allowed = {"-created_at", "-rating", "-discount_percent"}
    if ordering not in allowed:
        ordering = "-created_at"
    if ordering == "-discount_percent":
        queryset = queryset.filter(discount_price__isnull=False, discount_price__lt=F("price")).annotate(
            discount_percent=ExpressionWrapper(
                (F("price") - F("discount_price")) * 100.0 / F("price"),
                output_field=DecimalField(max_digits=8, decimal_places=3),
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


@api_view(["GET"])
@permission_classes([AllowAny])
def store_info(request):
    site = SiteSettings.load()
    logo_url = ""
    if site.site_logo:
        logo_url = request.build_absolute_uri(site.site_logo.url)
    return Response(
        {
            "site_name": site.site_name,
            "site_description": site.site_description,
            "site_email": site.site_email,
            "phone": site.phone,
            "address": site.address,
            "currency": site.currency,
            "footer_text": site.footer_text,
            "site_logo_url": logo_url,
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

    serializer = CatalogCategorySerializer(
        roots,
        many=True,
        context={"request": request, "catalog_products_by_root_id": catalog_map},
    )
    return Response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def products_list(request):
    """
    Storefront product list filters:
    - category=<slug>
    - brand=<integer_pk> (active brand)
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
    serializer = ProductSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def products_all_vendors_list(request):
    """Active products sold by approved vendors only (marketplace)."""
    queryset = _active_products_queryset().filter(
        seller__isnull=False,
        seller__status=Vendor.Status.APPROVED,
    )
    queryset = _apply_product_list_filters(queryset, request)
    queryset = _apply_product_ordering(queryset, request)

    paginator = ProductPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = ProductSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


def _user_has_delivered_paid_purchase(user, product: Product) -> bool:
    return OrderItem.objects.filter(
        product=product,
        order__customer=user,
        order__status=Order.Status.DELIVERED,
        order__payment_status=Order.PaymentStatus.PAID,
    ).exists()


@api_view(["GET"])
@permission_classes([AllowAny])
def product_detail(request, identifier):
    queryset = _active_products_queryset()
    product = queryset.filter(slug=identifier).first() or queryset.filter(pk=identifier).first()

    if not product:
        return Response({"detail": "Product not found."}, status=404)

    serializer = ProductSerializer(product, context={"request": request})
    data = dict(serializer.data)
    if request.user.is_authenticated:
        has_purchase = _user_has_delivered_paid_purchase(request.user, product)
        has_review = ProductReview.objects.filter(
            product=product, customer=request.user
        ).exists()
        data["can_submit_review"] = has_purchase and not has_review
    return Response(data)


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
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
                "detail": "You can only review products from a delivered order with paid payment.",
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
def cms_page_public(request, slug):
    row = CMSPage.objects.filter(slug=slug, status=CMSPage.Status.PUBLISHED).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    return Response(
        {
            "title": row.title,
            "slug": row.slug,
            "content": row.content,
            "seo_title": row.seo_title,
            "seo_description": row.seo_description,
            "last_updated": row.last_updated.isoformat() if row.last_updated else "",
        }
    )


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
    serializer = FlashDealSerializer(queryset, many=True, context={"request": request})
    return Response(serializer.data)


def _public_reels_base_queryset():
    return (
        Reel.objects.filter(status__in=[Reel.Status.ACTIVE, Reel.Status.APPROVED])
        .select_related("vendor", "product", "product__category")
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


def order_public_reels(queryset, tab: str):
    """
    trending: engagement (likes, views, shares) then recency.
    popular: views then likes.
    new: latest first.
    """
    t = (tab or "trending").lower()
    if t == "popular":
        return queryset.order_by("-views", "-likes", "-created_at")
    if t == "new":
        return queryset.order_by("-created_at")
    return queryset.order_by(
        "-is_sponsored", "-likes", "-views", "-shares", "-created_at"
    )


def annotate_reels_comments(queryset):
    return queryset.annotate(comments_count=Count("comments", distinct=True))


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
    queryset = annotate_reels_comments(
        order_public_reels(public_reels_queryset_for_request(request), tab)
    )

    paginator = ProductPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = ReelPublicSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


@api_view(["GET"])
@permission_classes([AllowAny])
def reels_trending_list(request):
    """Trending reels; optional vendor_ids / vendor_id / vendor_slug narrow the set (same as /website/reels/)."""
    queryset = annotate_reels_comments(
        order_public_reels(public_reels_queryset_for_request(request), "trending")
    )

    paginator = ProductPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = ReelPublicSerializer(page, many=True, context={"request": request})
    return paginator.get_paginated_response(serializer.data)


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
    created, views = reel_service.record_unique_view(reel, request.user, request)
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
    return Response(CartSerializer(cart, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def cart_item_add(request):
    product_id = request.data.get("product_id")
    quantity = int(request.data.get("quantity") or 1)
    if not product_id:
        return Response({"detail": "product_id is required."}, status=400)
    if quantity < 1:
        return Response({"detail": "quantity must be at least 1."}, status=400)
    product = Product.objects.filter(pk=product_id, status=Product.Status.ACTIVE).first()
    if not product:
        return Response({"detail": "Product not found."}, status=404)
    cart, _ = Cart.objects.get_or_create(user=request.user)
    item, created = CartItem.objects.get_or_create(
        cart=cart,
        product=product,
        defaults={"quantity": quantity},
    )
    if not created:
        item.quantity += quantity
        item.save(update_fields=["quantity", "updated_at"])
    return Response(CartSerializer(cart, context={"request": request}).data, status=201 if created else 200)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def cart_item_detail(request, pk):
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
    item.quantity = quantity
    item.save(update_fields=["quantity", "updated_at"])
    return Response({"id": item.pk, "quantity": item.quantity})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def wishlist_list(request):
    rows = (
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
    return Response(ProductWishlistSerializer(rows, many=True, context={"request": request}).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes([TokenAuthentication, SessionAuthentication])
def wishlist_item_add(request):
    product_id = request.data.get("product_id")
    if not product_id:
        return Response({"detail": "product_id is required."}, status=400)
    product = Product.objects.filter(pk=product_id, status=Product.Status.ACTIVE).first()
    if not product:
        return Response({"detail": "Product not found."}, status=404)
    item, _ = ProductWishlist.objects.get_or_create(user=request.user, product=product)
    return Response(ProductWishlistSerializer(item, context={"request": request}).data, status=201)


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
