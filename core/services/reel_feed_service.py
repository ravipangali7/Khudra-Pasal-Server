"""
Blended public reel feed with composite ranking (reel_ranking_service).

Default slot ratios (page_size=20): 50% personalized, 20% boosted, 20% trending, 10% random.
Boosted reels: one inserted after every four organic reels (20% cap, no domination).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from datetime import timedelta

from django.db.models import IntegerField, Q, QuerySet, Value
from django.utils import timezone

from core.models import (
    FamilyMember,
    OrderItem,
    Reel,
    ReelInteraction,
    ReelView,
    User,
)
from core.services import reel_ranking_service as ranking
from core.services.reels_site_settings import get_reels_feed_mix

if TYPE_CHECKING:
    from django.http import HttpRequest

FeedAudience = str  # "customer" | "family" | "child"

DEFAULT_SLOT_KEYS = (
    "personalized",
    "boosted",
    "trending",
    "random",
)

_LEGACY_SLOT_KEYS = ("categoryFollow", "experimental")

# Organic interleave weights within the 80% non-boost-injection slots (personalized-heavy).
_ORGANIC_INTERLEAVE: tuple[str, ...] = (
    "personalized",
    "personalized",
    "trending",
    "personalized",
    "random",
    "personalized",
    "trending",
    "personalized",
    "personalized",
    "trending",
    "personalized",
    "random",
    "personalized",
    "trending",
    "personalized",
    "personalized",
)

_CHILD_ORGANIC_INTERLEAVE: tuple[str, ...] = (
    "personalized",
    "trending",
    "personalized",
    "trending",
    "personalized",
    "random",
    "personalized",
    "trending",
    "personalized",
    "trending",
    "personalized",
    "trending",
    "personalized",
    "random",
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
    if raw in ("blended", "mixed_slots", "ranked", "1", "true", "yes"):
        return True
    if feed_algorithm in ("chronological", "popularity"):
        return False
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

    from django.db.models import Case, FloatField, Value, When

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
    fm = _child_family_member(user)
    if not fm:
        return qs
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


def _experimental_q(now) -> Q:
    cutoff = now - timedelta(days=14)
    return Q(created_at__gte=cutoff) & Q(views__lt=150)


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
    return list(qs.order_by(*order_by)[offset : offset + limit])


def _fetch_ranked_pool(
    base_qs: QuerySet,
    ctx: ranking.UserFeedContext,
    *,
    limit: int,
    offset: int,
    exclude_ids: set[int],
    explore_seed: str,
    extra_order: tuple[str, ...] = (),
) -> list[Reel]:
    if limit <= 0:
        return []
    qs = ranking.filter_low_quality(base_qs)
    qs = ranking.annotate_ranking_scores(qs, ctx, explore_seed=explore_seed)
    qs = annotate_boost_score(qs)
    order = tuple(extra_order) + (
        "-_rank_final",
        "-_rank_eng",
        "-likes",
        "-views",
        "-created_at",
    )
    rows = _pool_fetch(qs, order_by=order, limit=limit + 8, offset=offset, exclude_ids=exclude_ids)
    return ranking.rank_reels_in_memory(rows, ctx, explore_seed=explore_seed)[:limit]


def _build_organic_from_pools(
    pools: dict[str, list[Reel]],
    slot_counts: dict[str, int],
    *,
    audience: FeedAudience,
    page_size: int,
) -> list[Reel]:
    """Fill organic slots (personalized + trending + random) via interleave pattern."""
    organic_counts = {
        "personalized": slot_counts.get("personalized", 0),
        "trending": slot_counts.get("trending", 0),
        "random": slot_counts.get("random", 0),
    }
    pattern = (
        _CHILD_ORGANIC_INTERLEAVE if audience == "child" else _ORGANIC_INTERLEAVE
    )
    pattern = (pattern * ((page_size // len(pattern)) + 2))[: page_size * 2]

    pool_idx = {k: 0 for k in organic_counts}
    slots_filled = {k: 0 for k in organic_counts}
    exclude: set[int] = set()
    picked: list[Reel] = []
    organic_target = sum(organic_counts.values())

    def take(key: str) -> Reel | None:
        if slots_filled[key] >= organic_counts[key]:
            return None
        while pool_idx[key] < len(pools.get(key, [])):
            reel = pools[key][pool_idx[key]]
            pool_idx[key] += 1
            if reel.pk in exclude:
                continue
            slots_filled[key] += 1
            return reel
        return None

    for slot_key in pattern:
        if len(picked) >= organic_target:
            break
        if slots_filled.get(slot_key, 0) >= organic_counts.get(slot_key, 0):
            continue
        reel = take(slot_key)
        if reel:
            exclude.add(reel.pk)
            picked.append(reel)

    for key in ("personalized", "trending", "random"):
        while len(picked) < organic_target:
            reel = take(key)
            if reel is None:
                break
            exclude.add(reel.pk)
            picked.append(reel)

    return picked


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
    Ranked blended feed page. Flow: filter → score → rank → inject boosted → diversity.
    """
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    mix = mix or get_reels_feed_mix(audience)
    slot_counts = allocate_slot_counts(page_size, mix)

    u = user if user and user.is_authenticated else None
    ctx = ranking.build_user_feed_context(u)
    explore_seed = f"{u.pk if u else 'anon'}:{page}:{audience}"

    qs = annotate_boost_score(base_qs)
    qs = ranking.filter_low_quality(qs)
    if audience == "child" and u:
        qs = filter_reels_for_child(qs, u)

    now = timezone.now()
    sponsored_ok = _active_boost_q(now)
    exclude: set[int] = set()

    pool_offsets = {k: (page - 1) * slot_counts.get(k, 0) for k in DEFAULT_SLOT_KEYS}

    boosted_pool = _fetch_ranked_pool(
        qs.filter(sponsored_ok),
        ctx,
        limit=slot_counts["boosted"] + 6,
        offset=pool_offsets["boosted"],
        exclude_ids=exclude,
        explore_seed=explore_seed,
        extra_order=("-_reel_boost_score",),
    )

    trending_pool = _fetch_ranked_pool(
        qs,
        ctx,
        limit=slot_counts["trending"] + 6,
        offset=pool_offsets["trending"],
        exclude_ids=exclude,
        explore_seed=explore_seed,
    )

    converting_pool = _fetch_ranked_pool(
        qs.filter(product_id__isnull=False),
        ctx,
        limit=8,
        offset=(page - 1) * 2,
        exclude_ids=exclude,
        explore_seed=explore_seed,
        extra_order=("-cart_adds", "-likes"),
    )

    random_qs = qs.filter(_experimental_q(now))
    if u:
        random_qs = random_qs.exclude(
            pk__in=ReelView.objects.filter(user=u).values_list("reel_id", flat=True)
        )
    explore_pool = _fetch_ranked_pool(
        random_qs,
        ctx,
        limit=slot_counts["random"] + 6,
        offset=pool_offsets["random"],
        exclude_ids=exclude,
        explore_seed=explore_seed,
        extra_order=("-created_at",),
    )

    personalized_pool = _fetch_ranked_pool(
        qs,
        ctx,
        limit=slot_counts["personalized"] + 8,
        offset=pool_offsets["personalized"],
        exclude_ids=exclude,
        explore_seed=explore_seed,
    )

    if ctx.is_new_user:
        picked = ranking.build_new_user_feed(
            boosted_pool,
            trending_pool,
            converting_pool,
            explore_pool,
            page_size=page_size,
        )
    else:
        pools = {
            "personalized": personalized_pool,
            "trending": trending_pool,
            "random": explore_pool,
        }
        organic = _build_organic_from_pools(pools, slot_counts, audience=audience, page_size=page_size)
        picked = ranking.inject_boosted_reels(organic, boosted_pool, page_size=page_size)

    picked = ranking.apply_diversity(picked, ctx)

    if u:
        for reel in picked:
            ranking.record_feed_impression(u.id, reel.pk)

    if len(picked) < page_size:
        backfill = _fetch_ranked_pool(
            qs,
            ctx,
            limit=page_size - len(picked) + 4,
            offset=(page - 1) * page_size,
            exclude_ids=exclude | {r.pk for r in picked},
            explore_seed=explore_seed,
        )
        for reel in backfill:
            if len(picked) >= page_size:
                break
            if reel.pk not in {r.pk for r in picked}:
                picked.append(reel)
        picked = ranking.apply_diversity(picked[:page_size], ctx)

    has_more = len(picked) >= page_size and qs.exclude(pk__in=[r.pk for r in picked]).exists()
    return picked[:page_size], has_more


def get_feed_ranking_meta() -> dict:
    return {
        "ranking_version": ranking.RANKING_VERSION,
        "boost_after_organic": ranking.BOOST_AFTER_ORGANIC,
        "max_vendor_per_window": ranking.MAX_VENDOR_PER_WINDOW,
        "diversity_window": ranking.DIVERSITY_WINDOW,
    }
