"""KhudraReels options stored on SiteSettings.admin_extras.reels."""

from __future__ import annotations

from typing import Any, TypedDict

from core.models import SiteSettings


class ReelsSiteConfig(TypedDict):
    standardMultiplier: float
    premiumMultiplier: float
    megaMultiplier: float
    feedAlgorithm: str


_DEFAULTS: ReelsSiteConfig = {
    "standardMultiplier": 2.0,
    "premiumMultiplier": 5.0,
    "megaMultiplier": 10.0,
    "feedAlgorithm": "mixed",
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
    }
