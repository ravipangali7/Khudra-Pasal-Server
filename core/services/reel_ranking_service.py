"""
Reels feed ranking: composite scores, diversity, boosted insertion, new-user ordering.

Pipeline: fetch → filter low quality → score → rank → inject boosted → diversity → return.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.db.models import Case, Count, F, FloatField, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from core.models import OrderItem, Reel, ReelInteraction, ReelView, User

if TYPE_CHECKING:
    pass

# FinalScore weights
W_BOOST = 0.30
W_ENGAGEMENT = 0.30
W_PERSONALIZATION = 0.25
W_FRESHNESS = 0.10
W_EXPLORE = 0.05

# Personalization bonuses / penalties (spec §7–9)
BONUS_CATEGORY_MATCH = 40.0
BONUS_CART_PRODUCT = 30.0
PENALTY_QUICK_SKIP = 30.0
PENALTY_CATEGORY_SKIP_STREAK = 20.0
PENALTY_QUICK_SKIP_HEAVY = 50.0
PENALTY_REPORTED = 100.0
PENALTY_LOW_COMPLETION = 15.0

FRESHNESS_HALF_LIFE_DAYS = 7.0
MAX_VENDOR_PER_WINDOW = 2
DIVERSITY_WINDOW = 10
MAX_REEL_IMPRESSIONS_PER_DAY = 2
BOOST_AFTER_ORGANIC = 4

RANKING_VERSION = "v2"


@dataclass
class UserFeedContext:
    user: User | None
    is_new_user: bool = False
    category_ids: list[int] = field(default_factory=list)
    vendor_ids: list[int] = field(default_factory=list)
    cart_product_ids: list[int] = field(default_factory=list)
    purchased_product_ids: list[int] = field(default_factory=list)
    liked_reel_ids: set[int] = field(default_factory=set)
    shared_reel_ids: set[int] = field(default_factory=set)
    viewed_reel_ids: set[int] = field(default_factory=set)
    quick_skip_reel_ids: set[int] = field(default_factory=set)
    quick_skip_category_ids: set[int] = field(default_factory=set)
    search_category_ids: list[int] = field(default_factory=list)
    today_impression_counts: dict[int, int] = field(default_factory=dict)


def _today_key() -> str:
    return timezone.localdate().isoformat()


def impression_cache_key(user_id: int, reel_id: int) -> str:
    return f"reel_feed_imp:{user_id}:{reel_id}:{_today_key()}"


def record_feed_impression(user_id: int, reel_id: int) -> int:
    key = impression_cache_key(user_id, reel_id)
    try:
        return cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=86400)
        return 1


def build_user_feed_context(user: User | None) -> UserFeedContext:
    if not user or not user.is_authenticated:
        return UserFeedContext(user=None, is_new_user=True)

    category_ids: list[int] = []
    seen_cat: set[int] = set()

    def _add_cats(qs_values):
        for cid in qs_values:
            if cid and cid not in seen_cat:
                seen_cat.add(int(cid))
                category_ids.append(int(cid))

    _add_cats(
        OrderItem.objects.filter(
            order__customer=user,
            product__category_id__isnull=False,
        ).values_list("product__category_id", flat=True)[:200]
    )
    _add_cats(
        ReelInteraction.objects.filter(
            user=user,
            reel__product__category_id__isnull=False,
        ).values_list("reel__product__category_id", flat=True)[:200]
    )

    cart_product_ids = list(
        ReelInteraction.objects.filter(
            user=user,
            type=ReelInteraction.Type.CART_ADD,
            reel__product_id__isnull=False,
        ).values_list("reel__product_id", flat=True)
    )
    purchased_product_ids = list(
        OrderItem.objects.filter(order__customer=user).values_list("product_id", flat=True)[:200]
    )

    interactions = ReelInteraction.objects.filter(user=user).values_list(
        "reel_id", "type"
    )
    liked: set[int] = set()
    shared: set[int] = set()
    for rid, itype in interactions:
        if itype == ReelInteraction.Type.LIKE:
            liked.add(rid)
        elif itype == ReelInteraction.Type.SHARE:
            shared.add(rid)

    views_qs = ReelView.objects.filter(user=user).select_related("reel__product")
    viewed: set[int] = set()
    quick_skip_reels: set[int] = set()
    quick_skip_cats: set[int] = set()
    for v in views_qs[:800]:
        viewed.add(v.reel_id)
        if getattr(v, "quick_skip", False):
            quick_skip_reels.add(v.reel_id)
            if v.reel.product_id and v.reel.product and v.reel.product.category_id:
                quick_skip_cats.add(v.reel.product.category_id)

    vendor_ids = list(
        ReelInteraction.objects.filter(user=user)
        .values_list("reel__vendor_id", flat=True)
        .distinct()[:50]
    )

    has_orders = OrderItem.objects.filter(order__customer=user).exists()
    has_engagement = bool(liked or shared or cart_product_ids or len(viewed) > 3)
    is_new = not has_orders and not has_engagement

    imp_counts: dict[int, int] = {}
    for rid in viewed:
        key = impression_cache_key(user.id, rid)
        c = cache.get(key)
        if c:
            imp_counts[rid] = int(c)

    return UserFeedContext(
        user=user,
        is_new_user=is_new,
        category_ids=category_ids,
        vendor_ids=[int(v) for v in vendor_ids if v],
        cart_product_ids=[int(p) for p in cart_product_ids if p],
        purchased_product_ids=[int(p) for p in purchased_product_ids if p],
        liked_reel_ids=liked,
        shared_reel_ids=shared,
        viewed_reel_ids=viewed,
        quick_skip_reel_ids=quick_skip_reels,
        quick_skip_category_ids=quick_skip_cats,
        today_impression_counts=imp_counts,
    )


def _safe_ratio(num: float, den: float) -> float:
    if den <= 0:
        return 0.0
    return min(1.0, max(0.0, num / den))


def engagement_score(reel: Reel, *, comments_count: int = 0) -> float:
    views = max(1, int(reel.views or 0))
    full_watch = _safe_ratio(
        (reel.likes or 0) * 2 + (reel.shares or 0) * 3 + (reel.cart_adds or 0),
        views,
    )
    likes_n = _safe_ratio(reel.likes or 0, views) * 100
    shares_n = _safe_ratio(reel.shares or 0, views) * 100
    comments_n = _safe_ratio(comments_count, views) * 100
    return (
        likes_n * 0.2
        + shares_n * 0.3
        + comments_n * 0.2
        + full_watch * 100 * 0.3
    )


def trending_score(reel: Reel, *, views_24h: int = 0) -> float:
    views = max(1, int(reel.views or 0))
    watch_completion = _safe_ratio(
        (reel.likes or 0) + (reel.shares or 0) + (reel.bookmarks or 0),
        views,
    )
    v24 = views_24h if views_24h > 0 else min(reel.views or 0, 500)
    return v24 * 0.3 + (reel.shares or 0) * 0.3 + watch_completion * 100 * 0.4


def boost_score(reel: Reel, *, remaining_budget_ratio: float = 1.0) -> float:
    views = max(1, int(reel.views or 0))
    ctr = _safe_ratio(reel.cart_adds or 0, views) * 100
    watch_completion = _safe_ratio(
        (reel.likes or 0) + (reel.shares or 0),
        views,
    ) * 100
    product_clicks = _safe_ratio(reel.cart_adds or 0, views) * 100
    budget = max(0.0, min(1.0, remaining_budget_ratio)) * 100
    tier_mult = 1.0
    if reel.boost_tier == Reel.BoostTier.MEGA:
        tier_mult = 1.5
    elif reel.boost_tier == Reel.BoostTier.PREMIUM:
        tier_mult = 1.25
    return (
        budget * 0.4 + ctr * 0.2 + watch_completion * 0.2 + product_clicks * 0.2
    ) * tier_mult


def freshness_score(reel: Reel, now=None) -> float:
    now = now or timezone.now()
    age_days = max(0.0, (now - reel.created_at).total_seconds() / 86400.0)
    decay = math.exp(-age_days / FRESHNESS_HALF_LIFE_DAYS)
    return decay * 100.0


def random_explore_score(reel: Reel, *, seed: str) -> float:
    h = hashlib.md5(f"{seed}:{reel.pk}".encode()).hexdigest()
    return (int(h[:8], 16) % 10000) / 100.0


def personalization_score(reel: Reel, ctx: UserFeedContext) -> float:
    if not ctx.user:
        return 0.0
    score = 0.0
    cat_id = reel.product.category_id if reel.product_id and reel.product else None
    if cat_id and cat_id in ctx.category_ids:
        score += BONUS_CATEGORY_MATCH
    if reel.product_id and reel.product_id in ctx.cart_product_ids:
        score += BONUS_CART_PRODUCT
    if reel.product_id and reel.product_id in ctx.purchased_product_ids:
        score += 20.0
    if reel.vendor_id and reel.vendor_id in ctx.vendor_ids:
        score += 12.0
    if reel.pk in ctx.liked_reel_ids:
        score += 15.0
    if reel.pk in ctx.shared_reel_ids:
        score += 10.0
    if cat_id and cat_id in ctx.search_category_ids:
        score += 8.0
    if reel.pk in ctx.quick_skip_reel_ids:
        score -= PENALTY_QUICK_SKIP + PENALTY_QUICK_SKIP_HEAVY
    if cat_id and cat_id in ctx.quick_skip_category_ids:
        score -= PENALTY_CATEGORY_SKIP_STREAK
    if reel.pk in ctx.viewed_reel_ids:
        score -= 8.0
    return score


def remaining_boost_budget_ratio(reel: Reel) -> float:
    if not reel.is_sponsored:
        return 0.0
    budget = reel.boost_daily_budget_npr
    if not budget:
        return 1.0
    spent_proxy = (reel.views or 0) // 50
    return max(0.05, 1.0 - min(0.95, spent_proxy / max(budget, 1)))


def final_score(
    reel: Reel,
    ctx: UserFeedContext,
    *,
    comments_count: int = 0,
    views_24h: int = 0,
    explore_seed: str = "",
    tier_boost: float = 0.0,
) -> float:
    if reel.status == Reel.Status.REJECTED:
        return -PENALTY_REPORTED

    eng = engagement_score(reel, comments_count=comments_count)
    trend = trending_score(reel, views_24h=views_24h)
    pers = personalization_score(reel, ctx)
    fresh = freshness_score(reel)
    explore = random_explore_score(reel, seed=explore_seed or "default")

    b = boost_score(reel, remaining_budget_ratio=remaining_boost_budget_ratio(reel))
    b = max(b, tier_boost)

    views = max(1, int(reel.views or 0))
    completion = _safe_ratio((reel.likes or 0) + (reel.shares or 0), views)
    if completion < 0.02 and (reel.views or 0) > 30:
        eng -= PENALTY_LOW_COMPLETION
        trend -= PENALTY_LOW_COMPLETION * 0.5

    return (
        b * W_BOOST
        + eng * W_ENGAGEMENT
        + pers * W_PERSONALIZATION
        + fresh * W_FRESHNESS
        + explore * W_EXPLORE
        + trend * 0.05
    )


def filter_low_quality(qs: QuerySet) -> QuerySet:
    """Drop rejected and very low-signal reels from ranking pools."""
    return qs.exclude(status=Reel.Status.REJECTED).filter(
        Q(views__gte=1)
        | Q(likes__gte=1)
        | Q(is_sponsored=True)
        | Q(created_at__gte=timezone.now() - timedelta(days=3))
    )


def annotate_views_24h(qs: QuerySet) -> QuerySet:
    cutoff = timezone.now() - timedelta(hours=24)
    return qs.annotate(
        _views_24h=Count(
            "unique_views",
            filter=Q(unique_views__created_at__gte=cutoff),
        )
    )


def annotate_ranking_scores(
    qs: QuerySet,
    ctx: UserFeedContext,
    *,
    explore_seed: str = "",
) -> QuerySet:
    """DB-level approximations for pool ordering (personalization + engagement proxies)."""
    qs = annotate_views_24h(qs)
    zero = Value(0.0, output_field=FloatField())
    cat_ids = ctx.category_ids
    cart_pids = ctx.cart_product_ids

    pers = zero
    if cat_ids:
        pers = pers + Case(
            When(product__category_id__in=cat_ids, then=Value(float(BONUS_CATEGORY_MATCH))),
            default=zero,
            output_field=FloatField(),
        )
    if cart_pids:
        pers = pers + Case(
            When(product_id__in=cart_pids, then=Value(float(BONUS_CART_PRODUCT))),
            default=zero,
            output_field=FloatField(),
        )

    eng = (
        Coalesce(F("likes"), 0) * 0.2
        + Coalesce(F("shares"), 0) * 0.3
        + Coalesce(F("cart_adds"), 0) * 0.2
        + Coalesce(F("views"), 0) * 0.01 * 0.3
    )

    sponsored = Q(is_sponsored=True) & (
        Q(boost_expires_at__isnull=True) | Q(boost_expires_at__gt=timezone.now())
    )
    boost = Case(
        When(sponsored, then=Coalesce(F("cart_adds"), 0) * 2.0 + Coalesce(F("views"), 0) * 0.01),
        default=zero,
        output_field=FloatField(),
    )

    qs = qs.annotate(
        _rank_pers=pers,
        _rank_eng=eng,
        _rank_boost=boost,
        _rank_final=F("_rank_pers") * W_PERSONALIZATION
        + F("_rank_eng") * W_ENGAGEMENT
        + F("_rank_boost") * W_BOOST
        + Coalesce(F("_views_24h"), 0) * 0.05,
    )
    return qs


def exceeds_daily_impression_cap(ctx: UserFeedContext, reel_id: int) -> bool:
    if not ctx.user:
        return False
    count = ctx.today_impression_counts.get(reel_id, 0)
    if reel_id in ctx.viewed_reel_ids:
        count = max(count, 1)
    return count >= MAX_REEL_IMPRESSIONS_PER_DAY


def apply_diversity(feed: list[Reel], ctx: UserFeedContext) -> list[Reel]:
    """Max 2 reels per vendor per 10; respect daily impression cap per reel."""
    if not feed:
        return feed
    out: list[Reel] = []
    vendor_window: list[int] = []

    for reel in feed:
        if ctx.user and exceeds_daily_impression_cap(ctx, reel.pk):
            continue
        vid = reel.vendor_id
        if vid:
            recent = vendor_window[-DIVERSITY_WINDOW:]
            if recent.count(vid) >= MAX_VENDOR_PER_WINDOW:
                continue
        out.append(reel)
        if vid:
            vendor_window.append(vid)
    return out


def inject_boosted_reels(
    organic: list[Reel],
    boosted: list[Reel],
    *,
    page_size: int,
) -> list[Reel]:
    """Insert one boosted reel after every BOOST_AFTER_ORGANIC organic reels."""
    if not boosted:
        return organic[:page_size]
    result: list[Reel] = []
    b_idx = 0
    organic_since_boost = 0
    o_idx = 0
    used: set[int] = set()

    while len(result) < page_size and (o_idx < len(organic) or b_idx < len(boosted)):
        if organic_since_boost >= BOOST_AFTER_ORGANIC and b_idx < len(boosted):
            while b_idx < len(boosted):
                br = boosted[b_idx]
                b_idx += 1
                if br.pk not in used:
                    result.append(br)
                    used.add(br.pk)
                    organic_since_boost = 0
                    break
            continue
        if o_idx >= len(organic):
            while b_idx < len(boosted) and len(result) < page_size:
                br = boosted[b_idx]
                b_idx += 1
                if br.pk not in used:
                    result.append(br)
                    used.add(br.pk)
            break
        r = organic[o_idx]
        o_idx += 1
        if r.pk in used:
            continue
        result.append(r)
        used.add(r.pk)
        organic_since_boost += 1

    return result[:page_size]


def build_new_user_feed(
    boosted: list[Reel],
    trending: list[Reel],
    converting: list[Reel],
    explore: list[Reel],
    *,
    page_size: int,
) -> list[Reel]:
    """
    New user order: top boosted → trending → best converting → explore → another boosted.
    """
    slots: list[Reel | None] = [None] * page_size
    picks = [
        ("boosted", boosted),
        ("trending", trending),
        ("converting", converting),
        ("explore", explore),
        ("boosted", boosted),
    ]
    idx = 0
    pool_idx = {k: 0 for k in ("boosted", "trending", "converting", "explore")}
    used: set[int] = set()

    for key, pool in picks:
        if idx >= page_size:
            break
        pi = pool_idx[key]
        while pi < len(pool):
            r = pool[pi]
            pi += 1
            if r.pk in used:
                continue
            slots[idx] = r
            used.add(r.pk)
            idx += 1
            break
        pool_idx[key] = pi

    for key in ("trending", "converting", "explore", "boosted"):
        pool = {"boosted": boosted, "trending": trending, "converting": converting, "explore": explore}[key]
        pi = pool_idx[key]
        while idx < page_size and pi < len(pool):
            r = pool[pi]
            pi += 1
            if r.pk in used:
                continue
            slots[idx] = r
            used.add(r.pk)
            idx += 1
        pool_idx[key] = pi

    return [s for s in slots if s is not None]


def rank_reels_in_memory(
    reels: list[Reel],
    ctx: UserFeedContext,
    *,
    explore_seed: str = "",
    comments_by_reel: dict[int, int] | None = None,
    views_24h_by_reel: dict[int, int] | None = None,
) -> list[Reel]:
    comments_by_reel = comments_by_reel or {}
    views_24h_by_reel = views_24h_by_reel or {}

    def sort_key(r: Reel) -> float:
        return final_score(
            r,
            ctx,
            comments_count=comments_by_reel.get(r.pk, getattr(r, "comments_count", 0) or 0),
            views_24h=views_24h_by_reel.get(
                r.pk, getattr(r, "_views_24h", 0) or 0
            ),
            explore_seed=explore_seed,
            tier_boost=float(getattr(r, "_reel_boost_score", 0) or 0),
        )

    return sorted(reels, key=sort_key, reverse=True)
