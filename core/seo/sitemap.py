"""Dynamic XML sitemap built from registered providers."""

from __future__ import annotations

from importlib import import_module
from xml.sax.saxutils import escape

from django.conf import settings

from core.seo.resolvers import public_site_url

SITEMAP_PROVIDERS = [
    "core.seo.providers.static_pages",
    "core.seo.providers.categories",
    "core.seo.providers.cms_pages",
    "core.seo.providers.blog_posts",
    "core.seo.providers.products",
]


def sitemap_entry(
    loc: str,
    lastmod: str = "",
    changefreq: str = "",
    priority: str = "",
) -> str:
    parts = [f"  <url>\n    <loc>{escape(loc)}</loc>"]
    if lastmod:
        parts.append(f"    <lastmod>{escape(lastmod)}</lastmod>")
    if changefreq:
        parts.append(f"    <changefreq>{escape(changefreq)}</changefreq>")
    if priority:
        parts.append(f"    <priority>{escape(priority)}</priority>")
    parts.append("  </url>")
    return "\n".join(parts)


def iter_sitemap_entries():
    for provider_path in getattr(settings, "SITEMAP_PROVIDERS", SITEMAP_PROVIDERS):
        mod = import_module(provider_path)
        yield from mod.entries()


def build_sitemap_xml() -> str:
    body = "\n".join(
        sitemap_entry(
            e["loc"],
            e.get("lastmod", ""),
            e.get("changefreq", ""),
            e.get("priority", ""),
        )
        for e in iter_sitemap_entries()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</urlset>"
    )
