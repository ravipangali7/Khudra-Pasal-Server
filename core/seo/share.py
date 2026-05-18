"""HTML share landings for social crawlers (MOD_SHARE)."""

from __future__ import annotations

from html import escape

from core.seo.resolvers import resolve_description, resolve_og_image, resolve_title


def render_share_html(
    *,
    title: str,
    description: str,
    canonical_spa_url: str,
    og_type: str,
    og_image: str = "",
    share_url: str = "",
) -> str:
    t = escape(title)
    d = escape(description)
    canonical = escape(canonical_spa_url)
    og_url = escape(share_url or canonical_spa_url)
    img = (og_image or "").strip()
    img_escaped = escape(img) if img else ""

    og_image_tags = ""
    twitter_image_tag = ""
    if img and img.lower().startswith("https://"):
        og_image_tags = (
            f'    <meta property="og:image" content="{img_escaped}" />\n'
            f'    <meta property="og:image:secure_url" content="{img_escaped}" />\n'
        )
        twitter_image_tag = f'    <meta name="twitter:image" content="{img_escaped}" />\n'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{t}</title>
  <meta name="description" content="{d}" />
  <link rel="canonical" href="{canonical}" />
  <meta property="og:title" content="{t}" />
  <meta property="og:description" content="{d}" />
  <meta property="og:url" content="{og_url}" />
  <meta property="og:type" content="{escape(og_type)}" />
{og_image_tags}  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{t}" />
  <meta name="twitter:description" content="{d}" />
{twitter_image_tag}  <meta http-equiv="refresh" content="0;url={canonical}" />
</head>
<body>
  <p><a href="{canonical}">{t}</a></p>
</body>
</html>"""


def share_context_from_entity(
    *,
    meta_title: str,
    display_title: str,
    meta_description: str,
    excerpt: str,
    body: str,
    spa_path: str,
    share_api_path: str,  # absolute API share URL
    og_type: str,
    entity_image: str,
    site_name: str,
    site_description: str,
    site_logo: str,
    cover_image: str,
) -> dict:
    from core.seo.resolvers import spa_url as build_spa

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
    share_url = share_api_path if share_api_path else canonical
    image = resolve_og_image(entity_image, cover_image, site_logo)
    return {
        "title": page_title,
        "description": description,
        "canonical_spa_url": canonical,
        "share_url": share_url,
        "og_type": og_type,
        "og_image": image,
    }
