"""App download promotion banner (Super Admin → Settings → App Promotion Banner)."""

from __future__ import annotations

from typing import Any

from core.models import SiteSettings

ADMIN_EXTRAS_KEY = "app_promotion_banner"

_PUBLIC_STRING_FIELDS = (
    "headline",
    "subline",
    "cta_label",
    "store_url",
    "gradient_from",
    "gradient_to",
)


def _section(raw: dict | None) -> dict:
    block = (raw or {}).get(ADMIN_EXTRAS_KEY) if isinstance(raw, dict) else None
    return block if isinstance(block, dict) else {}


def normalize_app_promotion_banner(payload: dict | None) -> dict[str, str]:
    """Sanitize admin input before persisting in admin_extras."""
    src = payload if isinstance(payload, dict) else {}
    out: dict[str, str] = {}
    for key in _PUBLIC_STRING_FIELDS:
        out[key] = str(src.get(key) or "").strip()
    if not out["cta_label"]:
        out["cta_label"] = "Get app"
    return out


def public_app_promotion_banner_from_site(site: SiteSettings) -> dict[str, str] | None:
    """
    Public storefront payload. Returns None until a headline is configured
  (banner hidden on web + native shell).
    """
    cfg = normalize_app_promotion_banner(_section(site.admin_extras))
    headline = cfg.get("headline") or ""
    if not headline:
        return None
    data = {k: cfg[k] for k in _PUBLIC_STRING_FIELDS if cfg.get(k)}
    data["headline"] = headline
    if not data.get("cta_label"):
        data["cta_label"] = "Get app"
    return data


def get_admin_app_promotion_banner(site: SiteSettings | None = None) -> dict[str, str]:
    site = site or SiteSettings.load()
    return normalize_app_promotion_banner(_section(site.admin_extras))


def merge_admin_extras_patch(current: dict | None, patch: dict | None) -> dict:
    """Re-export-friendly helper for tests; mirrors admin _merge_admin_extras."""
    base = dict(current) if isinstance(current, dict) else {}
    if not isinstance(patch, dict):
        return base
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base
