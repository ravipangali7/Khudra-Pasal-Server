"""HTML share landings for social crawlers (MOD_SHARE)."""

from __future__ import annotations

import json
from html import escape

from core.seo.og_image import resolve_share_og_image


def render_share_html(
    *,
    title: str,
    description: str,
    canonical_spa_url: str,
    og_type: str,
    og_image: str = "",
    site_name: str = "",
) -> str:
    t = escape(title)
    d = escape(description)
    canonical = escape(canonical_spa_url)
    og_url = canonical
    img = (og_image or "").strip()
    img_escaped = escape(img) if img else ""
    site = escape(site_name) if site_name else ""
    img_alt = escape(title)

    og_image_tags = ""
    twitter_image_tag = ""
    if img:
        og_image_tags = (
            f'    <meta property="og:image" content="{img_escaped}" />\n'
            f'    <meta property="og:image:secure_url" content="{img_escaped}" />\n'
            f'    <meta property="og:image:type" content="image/jpeg" />\n'
            f'    <meta property="og:image:alt" content="{img_alt}" />\n'
        )
        twitter_image_tag = f'    <meta name="twitter:image" content="{img_escaped}" />\n'

    site_name_tag = (
        f'    <meta property="og:site_name" content="{site}" />\n' if site else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en" prefix="og: https://ogp.me/ns#">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{t}</title>
  <meta name="description" content="{d}" />
  <link rel="canonical" href="{canonical}" />
  <meta property="og:title" content="{t}" />
  <meta property="og:description" content="{d}" />
  <meta property="og:url" content="{og_url}" />
  <meta property="og:type" content="{escape(og_type)}" />
{site_name_tag}{og_image_tags}  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{t}" />
  <meta name="twitter:description" content="{d}" />
  <meta name="twitter:url" content="{og_url}" />
{twitter_image_tag}  <meta http-equiv="refresh" content="0;url={canonical}" />
</head>
<body>
  <p><a href="{canonical}">{t}</a></p>
  <script>window.location.replace({json.dumps(canonical_spa_url)});</script>
</body>
</html>"""


def share_context_from_entity(
    *,
    request,
    meta_title: str,
    display_title: str,
    meta_description: str,
    excerpt: str,
    body: str,
    spa_path: str,
    og_type: str,
    entity_image: str,
    site_name: str,
    site_description: str,
    site_logo: str,
    cover_image: str,
) -> dict:
    from core.seo.resolvers import resolve_description, resolve_title, spa_url as build_spa

    title = resolve_title(meta_title, display_title, site_name)
    if site_name and title and site_name.lower() not in title.lower():
        page_title = f"{title} | {site_name}"
    else:
        page_title = title or site_name
    description = resolve_description(
        meta_description,
        excerpt,
        body,
        fallback=site_description,
    )
    canonical = build_spa(spa_path)
    image = resolve_share_og_image(
        request,
        entity_image=entity_image,
        cover_image=cover_image,
        site_logo=site_logo,
    )
    return {
        "title": page_title,
        "description": description,
        "canonical_spa_url": canonical,
        "share_url": canonical,
        "og_type": og_type,
        "og_image": image,
        "site_name": site_name,
    }
