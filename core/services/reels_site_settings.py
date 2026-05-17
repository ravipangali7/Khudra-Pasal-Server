"""KhudraReels options stored on SiteSettings.admin_extras.reels."""

from __future__ import annotations

from typing import Any, TypedDict

from core.models import SiteSettings


class ReelsFeedMix(TypedDict):
    personalized: float
    boosted: float
    trending: float
    categoryFollow: float
    experimental: float


class ReelsSiteConfig(TypedDict):
    standardMultiplier: float
    premiumMultiplier: float
    megaMultiplier: float
    feedAlgorithm: str
    feedMix: ReelsFeedMix


_DEFAULTS: ReelsSiteConfig = {
    "standardMultiplier": 2.0,
    "premiumMultiplier": 5.0,
    "megaMultiplier": 10.0,
    "feedAlgorithm": "mixed",
    "feedMix": {
        "personalized": 0.50,
        "boosted": 0.20,
        "trending": 0.15,
        "categoryFollow": 0.10,
        "experimental": 0.05,
    },
}

_CHILD_FEED_MIX: ReelsFeedMix = {
    "personalized": 0.40,
    "boosted": 0.10,
    "trending": 0.25,
    "categoryFollow": 0.20,
    "experimental": 0.05,
}

_VALID_ALGOS = frozenset({"chronological", "popularity", "mixed", "personalized"})


def _float_from(v: Any, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x < 0:
        return default
    if x > 1_000_000:
        return 1_000_000.0
    return x


def _normalize_feed_mix(raw: Any) -> ReelsFeedMix:
    base = _DEFAULTS["feedMix"]
    if not isinstance(raw, dict):
        return dict(base)
    out: ReelsFeedMix = {
        "personalized": _float_from(raw.get("personalized"), base["personalized"]),
        "boosted": _float_from(raw.get("boosted"), base["boosted"]),
        "trending": _float_from(raw.get("trending"), base["trending"]),
        "categoryFollow": _float_from(raw.get("categoryFollow"), base["categoryFollow"]),
        "experimental": _float_from(raw.get("experimental"), base["experimental"]),
    }
    total = sum(out.values())
    if total <= 0:
        return dict(base)
    return {k: v / total for k, v in out.items()}


def get_reels_feed_mix(audience: str = "customer") -> ReelsFeedMix:
    """Slot ratios for blended feed; child audience uses a safer discovery mix."""
    if audience == "child":
        return dict(_CHILD_FEED_MIX)
    site = SiteSettings.load()
    raw = site.admin_extras or {}
    reels = raw.get("reels") if isinstance(raw, dict) else None
    if isinstance(reels, dict) and isinstance(reels.get("feedMix"), dict):
        return _normalize_feed_mix(reels["feedMix"])
    return dict(_DEFAULTS["feedMix"])


def get_reels_site_config() -> ReelsSiteConfig:
    site = SiteSettings.load()
    raw = site.admin_extras or {}
    reels = raw.get("reels") if isinstance(raw, dict) else None
    if not isinstance(reels, dict):
        reels = {}
    algo = str(reels.get("feedAlgorithm") or _DEFAULTS["feedAlgorithm"]).strip().lower()
    if algo not in _VALID_ALGOS:
        algo = "mixed"
    return {
        "standardMultiplier": _float_from(
            reels.get("standardMultiplier"), _DEFAULTS["standardMultiplier"]
        ),
        "premiumMultiplier": _float_from(
            reels.get("premiumMultiplier"), _DEFAULTS["premiumMultiplier"]
        ),
        "megaMultiplier": _float_from(reels.get("megaMultiplier"), _DEFAULTS["megaMultiplier"]),
        "feedAlgorithm": algo,
        "feedMix": _normalize_feed_mix(reels.get("feedMix")),
    }
