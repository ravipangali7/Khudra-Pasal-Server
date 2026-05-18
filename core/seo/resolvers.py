"""Shared title/description/image resolution for SPA and share HTML."""

from __future__ import annotations

import re
from html import unescape

from django.conf import settings

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(html: str) -> str:
    if not html:
        return ""
    return _HTML_TAG_RE.sub(" ", unescape(html)).replace("\xa0", " ").strip()


def truncate_text(text: str, max_len: int = 160) -> str:
    t = " ".join((text or "").split())
    if len(t) <= max_len:
        return t
    return f"{t[: max_len - 1].rstrip()}…"


def public_site_url() -> str:
    base = (getattr(settings, "PUBLIC_SITE_URL", None) or getattr(settings, "FRONTEND_URL", "") or "").strip()
    return base.rstrip("/")


def spa_url(path: str) -> str:
    base = public_site_url()
    p = path if path.startswith("/") else f"/{path}"
    return f"{base}{p}"


def resolve_title(meta_title: str, display_title: str, site_name: str = "") -> str:
    t = (meta_title or "").strip() or (display_title or "").strip()
    if not t and site_name:
        return site_name
    return t


def resolve_description(
    meta_description: str,
    excerpt: str = "",
    body: str = "",
    *,
    max_len: int = 160,
    fallback: str = "",
) -> str:
    for candidate in (meta_description, excerpt, strip_html(body)):
        c = (candidate or "").strip()
        if c:
            return truncate_text(c, max_len)
    return truncate_text(fallback, max_len) if fallback else ""


def resolve_og_image(*candidates: str) -> str:
    for url in candidates:
        u = (url or "").strip()
        if u:
            return u
    return ""
