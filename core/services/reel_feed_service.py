"""
Blended public reel feed: slot mix by audience (customer / family parent / child).

Default slot ratios (page_size=20 → 10 / 4 / 3 / 2 / 1):
  50% personalized, 20% boosted, 15% trending, 10% category-follow, 5% experimental.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from datetime import timedelta

from django.db.models import (
    Case,
    Exists,
    FloatField,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.utils import timezone

from core.models import (
    FamilyMember,
    OrderItem,
    Reel,
    ReelInteraction,
    ReelView,
    User,
)
from core.services.reels_site_settings import get_reels_feed_mix

if TYPE_CHECKING:
    from django.http import HttpRequest

FeedAudience = str  # "customer" | "family" | "child"

DEFAULT_SLOT_KEYS = (
    "personalized",
    "boosted",
    "trending",
    "categoryFollow",
    "experimental",
)

# Interleave high-personalization slots with discovery (boost/trend/category/experimental).
_INTERLEAVE_ORDER: tuple[str, ...] = (
    "personalized",
    "boosted",
    "personalized",
    "trending",
    "personalized",
    "categoryFollow",
    "personalized",
    "boosted",
    "personalized",
    "experimental",
    "personalized",
    "trending",
    "personalized",
    "boosted",
    "personalized",
    "categoryFollow",
    "personalized",
    "trending",
    "personalized",
    "experimental",
)

_CHILD_INTERLEAVE_ORDER: tuple[str, ...] = (
    "personalized",
    "trending",
    "personalized",
    "boosted",
    "personalized",
    "categoryFollow",
    "personalized",
    "trending",
    "personalized",
    "categoryFollow",
    "personalized",
    "trending",
    "personalized",
    "boosted",
    "personalized",
    "trending",
    "personalized",
    "categoryFollow",
    "personalized",
    "trending",
)


def detect_audience(request: HttpRequest) -> FeedAudience:
    raw = (request.query_params.get("audience") or "").strip().lower()
    if raw in ("customer", "family", "child"):
        return raw
    user = getattr(request, "user", None)
    if user and getattr(user, "is_authenticated", False):
        if user.role == User.Role.CHILD:
            return "child"
        from core.portal_roles import user_has_family_portal_access

        if user_has_family_portal_access(user):
            return "family"
    return "customer"


def should_use_blended_feed(request: HttpRequest, feed_algorithm: str) -> bool:
    raw = (request.query_params.get("feed") or "").strip().lower()
    if raw in ("legacy", "ordered", "chronological", "popularity"):
        return False
    if raw in ("blended", "mixed_slots", "1", "true", "yes"):
        return True
    if feed_algorithm in ("chronological", "popularity"):
        return False
    # mixed + personalized → slot-based feed (50/20/15/10/5 by default)
    return feed_algorithm in ("mixed", "personalized")


def allocate_slot_counts(page_size: int, mix: dict[str, float]) -> dict[str, int]:
    """Largest-remainder allocation so slot counts sum to page_size."""
    keys = DEFAULT_SLOT_KEYS
    weights = [max(0.0, float(mix.get(k, 0.0))) for k in keys]
    total_w = sum(weights) or 1.0
    raw = [page_size * w / total_w for w in weights]
    floors = [int(math.floor(x)) for x in raw]
    remainder = page_size - sum(floors)
    frac_order = sorted(
        range(len(keys)),
        key=lambda i: (raw[i] - floors[i], weights[i]),
        reverse=True,
    )
    counts = dict(zip(keys, floors))
    for i in range(remainder):
        counts[keys[frac_order[i % len(keys)]]] += 1
    return counts


def _active_boost_q(now) -> Q:
    return Q(is_sponsored=True) & (
        Q(boost_expires_at__isnull=True) | Q(boost_expires_at__gt=now)
    )


def annotate_boost_score(qs: QuerySet) -> QuerySet:
    from core.services.reels_site_settings import get_reels_site_config

    cfg = get_reels_site_config()
    std = float(cfg["standardMultiplier"])
    prem = float(cfg["premiumMultiplier"])
    mega = float(cfg["megaMultiplier"])
    now = timezone.now()
    sponsored_ok = _active_boost_q(now)
    boost_case = Case(
        When(sponsored_ok & Q(boost_tier=Reel.BoostTier.MEGA), then=Value(mega)),
        When(sponsored_ok & Q(boost_tier=Reel.BoostTier.PREMIUM), then=Value(prem)),
        When(sponsored_ok & Q(boost_tier=Reel.BoostTier.STANDARD), then=Value(std)),
        When(sponsored_ok, then=Value(std)),
        default=Value(0.0),
        output_field=FloatField(),
    )
    return qs.annotate(_reel_boost_score=boost_case)


def _child_family_member(user: User) -> FamilyMember | None:
    if user.role != User.Role.CHILD:
        return None
    return (
        FamilyMember.objects.filter(
            user=user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .select_related("group")
        .first()
    )


def filter_reels_for_child(qs: QuerySet, user: User) -> QuerySet:
    """
    Drop reels tied to products in categories blocked for the child's family group.
    Reels without a product remain visible.
    """
    fm = _child_family_member(user)
    if not fm:
        return qs
    blocked_ids: list[int] = []
    from core.models import ProductRestriction

    blocked_ids = list(
        ProductRestriction.objects.filter(
            group_id=fm.group_id,
            family_member__isnull=True,
            is_blocked=True,
        ).values_list("category_id", flat=True)
    )
    if not blocked_ids:
        return qs
    return qs.exclude(product__category_id__in=blocked_ids)


def _user_category_ids(user: User) -> list[int]:
    if not user.is_authenticated:
        return []
    from_orders = OrderItem.objects.filter(
        order__customer=user,
        product__category_id__isnull=False,
    ).values_list("product__category_id", flat=True)
    from_likes = ReelInteraction.objects.filter(
        user=user,
        type=ReelInteraction.Type.LIKE,
        reel__product__category_id__isnull=False,
    ).values_list("reel__product__category_id", flat=True)
    from_cart = ReelInteraction.objects.filter(
        user=user,
        type=ReelInteraction.Type.CART_ADD,
        reel__product__category_id__isnull=False,
    ).values_list("reel__product__category_id", flat=True)
    seen: set[int] = set()
    out: list[int] = []
    for cid in list(from_orders) + list(from_likes) + list(from_cart):
        if cid and cid not in seen:
            seen.add(cid)
            out.append(int(cid))
    return out


def _user_vendor_ids(user: User) -> list[int]:
    if not user.is_authenticated:
        return []
    qs = (
        ReelInteraction.objects.filter(user=user)
        .values_list("reel__vendor_id", flat=True)
        .distinct()
    )
    return [int(v) for v in qs if v]


def annotate_personalized_score(qs: QuerySet, user: User) -> QuerySet:
    if not user.is_authenticated:
        return qs.annotate(_feed_personal_score=Value(0, output_field=IntegerField()))

    category_ids = _user_category_ids(user)
    vendor_ids = _user_vendor_ids(user)
    viewed_reel_ids = list(
        ReelView.objects.filter(user=user).values_list("reel_id", flat=True)[:500]
    )

    zero = Value(0, output_field=IntegerField())
    parts: list = []
    if vendor_ids:
        parts.append(
            Case(
                When(vendor_id__in=vendor_ids, then=Value(12)),
                default=zero,
                output_field=IntegerField(),
            )
        )
    if category_ids:
        parts.append(
            Case(
                When(product__category_id__in=category_ids, then=Value(10)),
                default=zero,
                output_field=IntegerField(),
            )
        )
    liked = ReelInteraction.objects.filter(
        user=user,
        type=ReelInteraction.Type.LIKE,
        reel_id=OuterRef("pk"),
    )
    bookmarked = ReelInteraction.objects.filter(
        user=user,
        type=ReelInteraction.Type.BOOKMARK,
        reel_id=OuterRef("pk"),
    )
    parts.append(
        Case(
            When(Exists(liked), then=Value(8)),
            default=zero,
            output_field=IntegerField(),
        )
    )
    parts.append(
        Case(
            When(Exists(bookmarked), then=Value(6)),
            default=zero,
            output_field=IntegerField(),
        )
    )
    if viewed_reel_ids:
        parts.append(
            Case(
                When(pk__in=viewed_reel_ids, then=Value(-4)),
                default=zero,
                output_field=IntegerField(),
            )
        )
    score_expr = parts[0]
    for p in parts[1:]:
        score_expr = score_expr + p
    return qs.annotate(_feed_personal_score=score_expr)


def _pool_fetch(
    qs: QuerySet,
    *,
    order_by: tuple[str, ...],
    limit: int,
    offset: int,
    exclude_ids: set[int],
) -> list[Reel]:
    if limit <= 0:
        return []
    if exclude_ids:
        qs = qs.exclude(pk__in=exclude_ids)
    rows = list(qs.order_by(*order_by)[offset : offset + limit])
    return rows


def _experimental_q(now) -> Q:
    cutoff = now - timedelta(days=14)
    return Q(created_at__gte=cutoff) & Q(views__lt=150)


def build_blended_feed_page(
    base_qs: QuerySet,
    *,
    user: User | None,
    audience: FeedAudience,
    page: int,
    page_size: int,
    mix: dict[str, float] | None = None,
) -> tuple[list[Reel], bool]:
    """
    Return up to page_size reels for the given page and whether a next page likely exists.
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    mix = mix or get_reels_feed_mix(audience)
    slot_counts = allocate_slot_counts(page_size, mix)

    u = user if user and user.is_authenticated else None
    qs = annotate_boost_score(base_qs)
    if audience == "child" and u:
        qs = filter_reels_for_child(qs, u)

    now = timezone.now()
    sponsored_ok = _active_boost_q(now)
    category_ids = _user_category_ids(u) if u else []

    exclude: set[int] = set()
    picked: list[Reel] = []
    pools: dict[str, list[Reel]] = {k: [] for k in DEFAULT_SLOT_KEYS}

    # Per-pool offset: prior pages consumed (page-1) * slots per page for that pool
    pool_offsets = {
        k: (page - 1) * slot_counts[k] for k in DEFAULT_SLOT_KEYS
    }

    # Personalized
    pqs = annotate_personalized_score(qs, u) if u else qs.annotate(
        _feed_personal_score=Value(0, output_field=IntegerField())
    )
    pools["personalized"] = _pool_fetch(
        pqs,
        order_by=("-_feed_personal_score", "-likes", "-views", "-created_at"),
        limit=slot_counts["personalized"] + 4,
        offset=pool_offsets["personalized"],
        exclude_ids=exclude,
    )

    # Boosted (active sponsorship only)
    bqs = qs.filter(sponsored_ok)
    pools["boosted"] = _pool_fetch(
        bqs,
        order_by=("-_reel_boost_score", "-views", "-likes", "-created_at"),
        limit=slot_counts["boosted"] + 3,
        offset=pool_offsets["boosted"],
        exclude_ids=exclude,
    )

    # Trending (engagement, non-primary boost ordering)
    pools["trending"] = _pool_fetch(
        qs,
        order_by=("-likes", "-views", "-shares", "-created_at"),
        limit=slot_counts["trending"] + 3,
        offset=pool_offsets["trending"],
        exclude_ids=exclude,
    )

    # Category-follow
    if category_ids:
        cqs = qs.filter(product__category_id__in=category_ids)
    else:
        cqs = qs.filter(product__category_id__isnull=False)
    pools["categoryFollow"] = _pool_fetch(
        cqs,
        order_by=("-likes", "-views", "-created_at"),
        limit=slot_counts["categoryFollow"] + 2,
        offset=pool_offsets["categoryFollow"],
        exclude_ids=exclude,
    )

    # Experimental / new
    eqs = qs.filter(_experimental_q(now))
    if u:
        seen = ReelView.objects.filter(user=u).values_list("reel_id", flat=True)
        eqs = eqs.exclude(pk__in=seen)
    pools["experimental"] = _pool_fetch(
        eqs,
        order_by=("-created_at",),
        limit=slot_counts["experimental"] + 2,
        offset=pool_offsets["experimental"],
        exclude_ids=exclude,
    )

    pattern = _CHILD_INTERLEAVE_ORDER if audience == "child" else _INTERLEAVE_ORDER
    pattern = pattern[:page_size] if len(pattern) >= page_size else (
        pattern * ((page_size // len(pattern)) + 1)
    )[:page_size]

    pool_idx = {k: 0 for k in DEFAULT_SLOT_KEYS}
    slots_filled = {k: 0 for k in DEFAULT_SLOT_KEYS}

    def take_from_pool(key: str) -> Reel | None:
        if slots_filled[key] >= slot_counts[key]:
            return None
        while pool_idx[key] < len(pools[key]):
            reel = pools[key][pool_idx[key]]
            pool_idx[key] += 1
            if reel.pk in exclude:
                continue
            slots_filled[key] += 1
            return reel
        return None

    for slot_key in pattern:
        if len(picked) >= page_size:
            break
        if slots_filled[slot_key] >= slot_counts[slot_key]:
            continue
        reel = take_from_pool(slot_key)
        if reel is None:
            continue
        exclude.add(reel.pk)
        picked.append(reel)

    # Backfill remaining slots from trending then personalized
    if len(picked) < page_size:
        for key in ("trending", "personalized", "boosted", "categoryFollow", "experimental"):
            while len(picked) < page_size:
                reel = take_from_pool(key)
                if reel is None:
                    break
                if reel.pk in exclude:
                    continue
                exclude.add(reel.pk)
                picked.append(reel)

    has_more = len(picked) >= page_size and qs.exclude(pk__in=exclude).exists()
    return picked[:page_size], has_more
