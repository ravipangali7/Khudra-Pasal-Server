"""Generate portal order bills (PNG) and invoice-shaped JSON for preview/download."""

from __future__ import annotations

import io
import logging
import os
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.files.base import ContentFile

from core.models import Order, SiteSettings
from core.views.admin.admin_write_utils import absolute_media_url, product_primary_image_url

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

BILL_WIDTH = 820
BILL_PADDING = 36
LINE_THUMB = 52
LINE_ROW_H = 64
HEADER_H = 120
FOOTER_H = 140


def _public_origin() -> str:
    raw = (
        os.environ.get("DJANGO_PUBLIC_ORIGIN", "").strip()
        or os.environ.get("API_PUBLIC_URL", "").strip()
    ).rstrip("/")
    if raw:
        return raw
    return "http://127.0.0.1:8000"


def public_file_url(file_field) -> str:
    if not file_field:
        return ""
    try:
        path = file_field.url
    except ValueError:
        return ""
    if path.startswith("http"):
        return path
    return f"{_public_origin()}{path}"


def portal_orders_path_for_order(order: Order) -> str:
    portal = order.placed_portal or Order.PlacedPortal.PORTAL_MAIN
    if portal == Order.PlacedPortal.PORTAL_FAMILY:
        return f"/family-portal/my-orders/{order.pk}"
    if portal == Order.PlacedPortal.PORTAL_CHILD:
        return f"/child-portal/my-orders/{order.pk}"
    return f"/portal/orders/{order.pk}"


def portal_bill_action_url(order: Order) -> str:
    base = portal_orders_path_for_order(order)
    return f"{base}?bill=1"


def _order_should_have_bill(order: Order) -> bool:
    if order.is_pos_order:
        return True
    if order.placed_portal:
        return True
    return True


def serialize_order_invoice(order: Order, request: HttpRequest | None = None) -> dict:
    site = SiteSettings.load()
    addr = getattr(order, "delivery_address", None)
    customer_name = (order.customer.name or "").strip() or (
        order.customer.phone or ""
    ).strip() or f"Customer #{order.customer_id}"
    full_address = ""
    phone = order.customer.phone or ""
    if addr:
        phone = addr.mobile or phone
        parts = [x for x in (addr.area_location, addr.landmark) if (x or "").strip()]
        full_address = ", ".join(parts)

    lines = []
    for it in order.items.select_related("product").all():
        img = product_primary_image_url(request, it.product) if request else ""
        if not img and it.product.image:
            try:
                img = it.product.image.url
            except ValueError:
                img = ""
        lines.append(
            {
                "name": it.product.name,
                "sku": it.product.sku or "",
                "qty": it.quantity,
                "unit_price": float(it.unit_price),
                "total": float(it.total_price),
                "image_url": img,
            }
        )

    logo_url = ""
    if site.site_logo:
        logo_url = absolute_media_url(request, site.site_logo) if request else public_file_url(
            site.site_logo
        )

    return {
        "title": "Invoice",
        "docId": order.order_number,
        "date": order.created_at.date().isoformat(),
        "branding": {
            "site_name": site.site_name or "Khudra Pasal",
            "site_logo_url": logo_url,
            "phone": site.phone or "",
            "address": site.address or "",
            "currency": site.currency or "NPR",
        },
        "billTo": {
            "name": customer_name,
            "phone": phone,
            "address": full_address,
        },
        "lines": lines,
        "subtotal": float(order.subtotal),
        "discount": float(order.discount_amount + order.app_promo_discount_amount),
        "delivery": float(order.delivery_fee),
        "total": float(order.total),
        "notes": (order.notes or "")[:500],
        "has_bill_image": bool(order.bill_image),
        "bill_image_url": public_file_url(order.bill_image) if order.bill_image else "",
    }


def _load_font(size: int):
    from PIL import ImageFont

    candidates = []
    if os.name == "nt":
        candidates.extend(
            [
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
            ]
        )
    candidates.extend(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _open_product_thumb(product, size: int = LINE_THUMB):
    from PIL import Image

    field = getattr(product, "image", None)
    if field:
        try:
            path = field.path
            if os.path.isfile(path):
                img = Image.open(path).convert("RGBA")
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
                return img
        except (OSError, ValueError):
            pass
    first = None
    cache = getattr(product, "_prefetched_objects_cache", None)
    if cache is not None and "images" in cache:
        imgs = sorted(product.images.all(), key=lambda x: (x.sort_order, x.id))
        first = imgs[0] if imgs else None
    else:
        first = product.images.order_by("sort_order", "id").first()
    if first is not None and first.image:
        try:
            path = first.image.path
            if os.path.isfile(path):
                img = Image.open(path).convert("RGBA")
                img.thumbnail((size, size), Image.Resampling.LANCZOS)
                return img
        except (OSError, ValueError):
            pass
    placeholder = Image.new("RGBA", (size, size), (241, 245, 249, 255))
    return placeholder


def _draw_wrapped_text(draw, text: str, xy, font, fill, max_width: int, max_lines: int = 2):
    words = (text or "").split()
    if not words:
        return xy[1]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if len(lines) < max_lines:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, len(lines[-1]) - 3)] + "..."
    y = xy[1]
    for line in lines:
        draw.text((xy[0], y), line, font=font, fill=fill)
        y += getattr(font, "size", 12) + 4
    return y


def generate_order_bill_image(order: Order) -> bool:
    """Render bill PNG and save on order.bill_image. Returns True on success."""
    from PIL import Image, ImageDraw

    order = (
        Order.objects.select_related("customer", "seller", "delivery_address")
        .prefetch_related("items__product", "items__product__images")
        .filter(pk=order.pk)
        .first()
    )
    if not order:
        return False

    site = SiteSettings.load()
    items = list(order.items.all())
    n_lines = max(len(items), 1)
    height = (
        BILL_PADDING * 2
        + HEADER_H
        + n_lines * LINE_ROW_H
        + FOOTER_H
        + 40
    )
    img = Image.new("RGB", (BILL_WIDTH, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font_title = _load_font(22)
    font_bold = _load_font(14)
    font = _load_font(12)
    font_small = _load_font(11)

    y = BILL_PADDING
    site_name = site.site_name or "Khudra Pasal"
    draw.text((BILL_PADDING, y), site_name, font=font_title, fill=(15, 23, 42))
    y += 30
    if site.phone:
        draw.text((BILL_PADDING, y), site.phone, font=font_small, fill=(71, 85, 105))
        y += 16
    draw.text(
        (BILL_WIDTH - BILL_PADDING, BILL_PADDING),
        "INVOICE",
        font=font_small,
        fill=(100, 116, 139),
        anchor="ra",
    )
    draw.text(
        (BILL_WIDTH - BILL_PADDING, BILL_PADDING + 18),
        order.order_number,
        font=font_bold,
        fill=(15, 23, 42),
        anchor="ra",
    )
    draw.text(
        (BILL_WIDTH - BILL_PADDING, BILL_PADDING + 36),
        order.created_at.date().isoformat(),
        font=font_small,
        fill=(71, 85, 105),
        anchor="ra",
    )

    y = BILL_PADDING + HEADER_H - 20
    draw.line([(BILL_PADDING, y), (BILL_WIDTH - BILL_PADDING, y)], fill=(226, 232, 240), width=1)
    y += 14
    draw.text((BILL_PADDING, y), "BILL TO", font=font_small, fill=(100, 116, 139))
    y += 16
    customer_name = (order.customer.name or "").strip() or order.customer.phone or "Customer"
    draw.text((BILL_PADDING, y), customer_name[:80], font=font_bold, fill=(15, 23, 42))
    y += 20
    addr = getattr(order, "delivery_address", None)
    if addr and addr.area_location:
        y = _draw_wrapped_text(
            draw,
            ", ".join(x for x in (addr.area_location, addr.landmark) if x),
            (BILL_PADDING, y),
            font_small,
            (71, 85, 105),
            BILL_WIDTH - BILL_PADDING * 2,
            2,
        )

    y += 12
    draw.line([(BILL_PADDING, y), (BILL_WIDTH - BILL_PADDING, y)], fill=(226, 232, 240), width=1)
    y += 10

    col_name_x = BILL_PADDING + LINE_THUMB + 14
    name_w = BILL_WIDTH - BILL_PADDING - col_name_x - 160
    draw.text((col_name_x, y), "Item", font=font_small, fill=(100, 116, 139))
    draw.text((BILL_WIDTH - BILL_PADDING - 120, y), "Qty", font=font_small, fill=(100, 116, 139))
    draw.text((BILL_WIDTH - BILL_PADDING, y), "Total", font=font_small, fill=(100, 116, 139), anchor="ra")
    y += 18

    cur = site.currency or "NPR"
    for it in items:
        row_y = y
        thumb = _open_product_thumb(it.product)
        if thumb.mode == "RGBA":
            bg = Image.new("RGB", thumb.size, (255, 255, 255))
            bg.paste(thumb, mask=thumb.split()[3])
            thumb = bg
        img.paste(thumb, (BILL_PADDING, row_y + 4))
        _draw_wrapped_text(
            draw,
            it.product.name,
            (col_name_x, row_y + 6),
            font,
            (15, 23, 42),
            name_w,
            2,
        )
        draw.text(
            (BILL_WIDTH - BILL_PADDING - 120, row_y + 18),
            str(it.quantity),
            font=font,
            fill=(15, 23, 42),
        )
        total_txt = f"{cur} {it.total_price:,.2f}"
        draw.text(
            (BILL_WIDTH - BILL_PADDING, row_y + 18),
            total_txt,
            font=font_bold,
            fill=(15, 23, 42),
            anchor="ra",
        )
        y += LINE_ROW_H
        draw.line([(BILL_PADDING, y - 6), (BILL_WIDTH - BILL_PADDING, y - 6)], fill=(241, 245, 249), width=1)

    y += 8
    totals_x = BILL_WIDTH - BILL_PADDING - 220
    def _total_row(label: str, amount: Decimal, bold: bool = False) -> None:
        nonlocal y
        f = font_bold if bold else font
        draw.text((totals_x, y), label, font=f, fill=(71, 85, 105) if not bold else (15, 23, 42))
        draw.text(
            (BILL_WIDTH - BILL_PADDING, y),
            f"{cur} {amount:,.2f}",
            font=f,
            fill=(5, 150, 105) if bold else (15, 23, 42),
            anchor="ra",
        )
        y += 22

    discount = order.discount_amount + order.app_promo_discount_amount
    _total_row("Subtotal", order.subtotal)
    _total_row("Discount", discount)
    _total_row("Delivery", order.delivery_fee)
    y += 4
    draw.line([(totals_x, y), (BILL_WIDTH - BILL_PADDING, y)], fill=(203, 213, 225), width=1)
    y += 10
    _total_row("Total", order.total, bold=True)

    y += 16
    draw.text(
        (BILL_WIDTH // 2, y),
        "Thank you for your business.",
        font=font_small,
        fill=(148, 163, 184),
        anchor="ma",
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    fname = f"{order.order_number}-bill.png"
    order.bill_image.save(fname, ContentFile(buf.read()), save=True)
    return True


def ensure_order_bill(order_id: int) -> None:
    order = Order.objects.filter(pk=order_id).first()
    if not order or not _order_should_have_bill(order):
        return
    if order.bill_image:
        return
    try:
        generate_order_bill_image(order)
    except Exception:
        logger.exception("Failed to generate bill for order %s", order_id)


def bill_image_url_for_order(order: Order) -> str:
    if not order.bill_image:
        return ""
    return public_file_url(order.bill_image)
