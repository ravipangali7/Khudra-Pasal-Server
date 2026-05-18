"""Resolve absolute HTTPS Open Graph image URLs for share HTML and APIs."""

from __future__ import annotations

from django.conf import settings

from core.seo.media_urls import ensure_https_og_image


def public_api_origin(request=None) -> str:
    """Host that serves /media/ (Django API), not the SPA storefront."""
    explicit = (getattr(settings, "PUBLIC_API_URL", None) or "").strip().rstrip("/")
    if explicit:
        return explicit
    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    return ""


def is_usable_og_image_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u:
        return False
    if "placeholder" in u or u.endswith("/placeholder.svg"):
        return False
    return u.startswith("http://") or u.startswith("https://") or u.startswith("//")


def public_media_url(request, url: str = "", *, file_field=None) -> str:
    """Absolute HTTPS URL for a media file or path (always on the API origin)."""
    if file_field is not None:
        try:
            url = file_field.url
        except (ValueError, AttributeError):
            url = ""
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return ensure_https_og_image(f"https:{u}")
    if u.startswith("http://") or u.startswith("https://"):
        return ensure_https_og_image(u)
    origin = public_api_origin(request)
    path = u if u.startswith("/") else f"/{u}"
    if origin:
        return ensure_https_og_image(f"{origin}{path}")
    if request is not None:
        return ensure_https_og_image(request.build_absolute_uri(path))
    return ""


def resolve_share_og_image(
    request,
    *,
    entity_image: str = "",
    cover_image: str = "",
    site_logo: str = "",
) -> str:
    """Entity image → site cover → logo; skip placeholders; always absolute HTTPS."""
    for candidate in (entity_image, cover_image, site_logo):
        raw = (candidate or "").strip()
        if not raw:
            continue
        if raw.startswith("//"):
            normalized = ensure_https_og_image(f"https:{raw}")
        elif raw.startswith("http://") or raw.startswith("https://"):
            normalized = ensure_https_og_image(raw)
        else:
            normalized = public_media_url(request, raw)
        if is_usable_og_image_url(normalized):
            return normalized
    return ""
