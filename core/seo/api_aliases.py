"""CamelCase SEO field aliases for public API responses."""


def with_seo_aliases(data: dict) -> dict:
    """Add metaTitle / metaDescription / metaKeywords without removing snake_case keys."""
    out = dict(data)
    if "seo_title" in out:
        out["metaTitle"] = out.get("seo_title") or ""
    if "seo_description" in out:
        out["metaDescription"] = out.get("seo_description") or ""
    if "seo_keywords" in out:
        out["metaKeywords"] = out.get("seo_keywords") or ""
    return out
