"""Normalize media URLs for Open Graph (absolute + HTTPS when possible)."""

from __future__ import annotations

from django.conf import settings


def ensure_absolute_media_url(request, url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return f"https:{u}"
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if request is not None:
        return request.build_absolute_uri(u if u.startswith("/") else f"/{u}")
    base = (getattr(settings, "PUBLIC_SITE_URL", "") or "").rstrip("/")
    if base:
        return f"{base}{u if u.startswith('/') else '/' + u}"
    return u


def ensure_https_og_image(url: str) -> str:
    """Prefer HTTPS for og:image (crawlers require reachable absolute URLs)."""
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return f"https:{u}"
    if u.startswith("http://"):
        return "https://" + u[7:]
    return u
