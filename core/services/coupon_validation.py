"""Validate coupons for portal checkout (eligible lines, min order, discount amount)."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from core.models import Coupon, Product

_Q2 = Decimal("0.01")


def _quantize(d: Decimal) -> Decimal:
    return d.quantize(_Q2, rounding=ROUND_HALF_UP)


def product_in_coupon_category(product: Product, category_id: int) -> bool:
    if not product.category_id:
        return False
    cur = product.category
    while cur is not None:
        if cur.id == category_id:
            return True
        cur = cur.parent
    return False


def line_eligible_for_coupon(
    coupon: Coupon,
    product: Product,
    flash_overrides: dict[int, Decimal],
) -> bool:
    """Flash override lines do not stack with coupons; vendor/category scope applies."""
    if product.pk in flash_overrides:
        return False
    if coupon.vendor_id is not None:
        if product.seller_id is None or product.seller_id != coupon.vendor_id:
            return False
    if coupon.category_id is not None:
        if not product_in_coupon_category(product, coupon.category_id):
            return False
    return True


def eligible_subtotal_for_coupon(
    coupon: Coupon,
    lines: list[tuple[Product, int, Decimal]],
    flash_overrides: dict[int, Decimal],
) -> Decimal:
    total = Decimal("0")
    for product, qty, unit in lines:
        if not line_eligible_for_coupon(coupon, product, flash_overrides):
            continue
        total += unit * qty
    return _quantize(total)


def compute_coupon_discount_amount(
    coupon: Coupon,
    eligible_subtotal: Decimal,
) -> Decimal:
    if eligible_subtotal <= 0:
        return Decimal("0.00")
    if coupon.type == Coupon.Type.PERCENTAGE:
        raw = eligible_subtotal * coupon.value / Decimal(100)
        return _quantize(min(raw, eligible_subtotal))
    if coupon.type == Coupon.Type.FIXED:
        return _quantize(min(coupon.value, eligible_subtotal))
    return Decimal("0.00")


def validate_and_compute_coupon(
    code: str | None,
    *,
    lines: list[tuple[Product, int, Decimal]],
    flash_overrides: dict[int, Decimal],
) -> tuple[Coupon | None, Decimal, str | None]:
    """
    Returns (coupon_or_none, discount_amount, error_message).
    Empty code → (None, 0, None).
    """
    if not code or not str(code).strip():
        return None, Decimal("0"), None
    raw = str(code).strip()
    c = Coupon.objects.select_related("vendor", "category").filter(code__iexact=raw).first()
    if not c:
        return None, Decimal("0"), "Invalid coupon code."
    if c.status != Coupon.Status.ACTIVE:
        return None, Decimal("0"), "This coupon is not active."
    now = timezone.now()
    if c.expires_at is not None and c.expires_at < now:
        return None, Decimal("0"), "This coupon has expired."
    if c.usage_limit is not None and c.used_count >= c.usage_limit:
        return None, Decimal("0"), "This coupon has reached its usage limit."

    eligible = eligible_subtotal_for_coupon(c, lines, flash_overrides)
    if eligible < c.min_order:
        return None, Decimal("0"), (
            f"Minimum order amount for this coupon is Rs. {c.min_order} on eligible items."
        )

    disc = compute_coupon_discount_amount(c, eligible)
    if disc <= 0:
        return None, Decimal("0"), "No discount applies for this cart with this coupon."

    return c, disc, None


def split_discount_across_sellers(
    discount_total: Decimal,
    seller_eligible: dict[int | None, Decimal],
) -> dict[int | None, Decimal]:
    """Proportional split by eligible subtotal per seller; last seller absorbs rounding remainder."""
    if discount_total <= 0:
        return {k: Decimal("0") for k in seller_eligible}
    total_eligible = sum(seller_eligible.values(), Decimal("0"))
    if total_eligible <= 0:
        return {k: Decimal("0") for k in seller_eligible}
    keys = sorted(seller_eligible.keys(), key=lambda s: (s is None, s or 0))
    acc = Decimal("0")
    out: dict[int | None, Decimal] = {}
    for i, sid in enumerate(keys):
        el = seller_eligible[sid]
        if i == len(keys) - 1:
            part = _quantize(discount_total - acc)
        else:
            part = _quantize(discount_total * el / total_eligible)
            acc += part
        out[sid] = part
    return out
