"""Product catalog export for merchant / ads feeds (CSV, JSON, RSS-style XML)."""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from xml.sax.saxutils import escape

from django.utils import timezone

from core.models import Product
from core.seo.og_image import public_media_url
from core.seo.resolvers import resolve_description, spa_url
from core.services.product_pricing import storefront_unit_price
from core.services.storefront_product_visibility import storefront_active_product_q

_CURRENCY = "NPR"
_MAX_DESC = 5000


def _catalog_queryset():
    return (
        Product.objects.filter(storefront_active_product_q())
        .select_related("category", "brand")
        .prefetch_related("images")
        .order_by("-updated_at")[:5000]
    )


def _product_image_link(product: Product) -> str:
    if getattr(product, "image", None):
        url = public_media_url(None, file_field=product.image)
        if url:
            return url
    cache = getattr(product, "_prefetched_objects_cache", None)
    if cache is not None and "images" in cache:
        imgs = sorted(product.images.all(), key=lambda x: (x.sort_order, x.id))
        if imgs:
            return public_media_url(None, file_field=imgs[0].image)
    first = product.images.order_by("sort_order", "id").first()
    if first:
        return public_media_url(None, file_field=first.image)
    return ""


def _format_price(amount: Decimal) -> str:
    return f"{amount.quantize(Decimal('0.01'))} {_CURRENCY}"


def _availability(product: Product) -> str:
    if product.status == Product.Status.OUT_OF_STOCK or product.stock <= 0:
        return "out of stock"
    return "in stock"


def catalog_row(product: Product, *, now=None) -> dict[str, str]:
    now = now or timezone.now()
    list_price = product.price
    sale_unit = storefront_unit_price(product, now=now)
    slug = product.slug or str(product.pk)
    description = resolve_description(
        product.seo_description,
        product.short_description,
        product.description,
        max_len=_MAX_DESC,
        fallback=product.name,
    )
    row = {
        "id": str(product.pk),
        "title": product.name,
        "description": description,
        "link": spa_url(f"/product/{slug}"),
        "image_link": _product_image_link(product),
        "availability": _availability(product),
        "price": _format_price(list_price),
        "brand": (product.brand.name if product.brand_id else ""),
        "category": (product.category.name if product.category_id else ""),
    }
    if sale_unit < list_price:
        row["sale_price"] = _format_price(sale_unit)
    return row


def iter_catalog_rows():
    now = timezone.now()
    for product in _catalog_queryset():
        yield catalog_row(product, now=now)


def build_catalog_json() -> str:
    return json.dumps({"items": list(iter_catalog_rows())}, ensure_ascii=False)


def build_catalog_csv() -> str:
    buf = io.StringIO()
    fieldnames = [
        "id",
        "title",
        "description",
        "link",
        "image_link",
        "availability",
        "price",
        "sale_price",
        "brand",
        "category",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in iter_catalog_rows():
        writer.writerow(row)
    return buf.getvalue()


def build_catalog_xml() -> str:
    channel_title = "Khudra Pasal"
    channel_link = spa_url("/")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
        "<channel>",
        f"<title>{escape(channel_title)}</title>",
        f"<link>{escape(channel_link)}</link>",
        f"<description>{escape(channel_title)} product feed</description>",
    ]
    for row in iter_catalog_rows():
        lines.append("<item>")
        for key, val in row.items():
            if not val:
                continue
            tag = f"g:{key}" if key not in ("title", "link", "description") else key
            lines.append(f"  <{tag}>{escape(val)}</{tag}>")
        lines.append("</item>")
    lines.extend(["</channel>", "</rss>"])
    return "\n".join(lines)
