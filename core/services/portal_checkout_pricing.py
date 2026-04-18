"""Shared cart resolution, coupon split, and delivery allocation for portal checkout and quote."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.utils import timezone

from core.models import Coupon, Product, ShippingMethod, ShippingSettings, ShippingZone, Vendor
from core.services.child_shopping_guard import validate_child_may_purchase_product
from core.services.coupon_validation import (
    eligible_subtotal_for_coupon,
    line_eligible_for_coupon,
    split_discount_across_sellers,
    validate_and_compute_coupon,
)
from core.services.product_pricing import flash_pricing_for_products, storefront_unit_price
from core.services.shipping_quote import compute_shipping_fee


@dataclass(frozen=True)
class ResolvedCheckoutCart:
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]]
    flash_overrides: dict[int, Decimal]
    flash_deal_by_product_id: dict[int, int]
    cart_subtotal: Decimal
    list_subtotal: Decimal
    flash_product_ids: list[int]
    stock_warnings: list[dict[str, Any]]


def _parse_line_product_id(raw: dict[str, Any]) -> int | None:
    """Accept product_id or productId; coerce numeric strings; reject non-positive."""
    v = raw.get("product_id")
    if v is None:
        v = raw.get("productId")
    if v is None or (isinstance(v, bool)):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def parse_checkout_items(items: Any) -> list[tuple[int, int]]:
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    parsed: list[tuple[int, int]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("each item must be an object with product_id and quantity")
        pid = _parse_line_product_id(raw)
        try:
            qty = int(raw.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        if pid is None or qty < 1:
            raise ValueError("each item needs product_id and quantity")
        parsed.append((pid, qty))
    return parsed


def resolve_checkout_lines(
    parsed: list[tuple[int, int]],
    user,
    *,
    select_for_update: bool,
    strict_stock: bool,
) -> ResolvedCheckoutCart:
    now_ts = timezone.now()
    flash_rows = flash_pricing_for_products([a for a, _ in parsed], now_ts)
    flash_overrides = {pid: row.unit for pid, row in flash_rows.items()}
    flash_deal_by_product_id = {pid: row.flash_deal_id for pid, row in flash_rows.items()}
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]] = defaultdict(
        list
    )
    cart_subtotal = Decimal("0")
    list_subtotal = Decimal("0")
    stock_warnings: list[dict[str, Any]] = []

    for pid, qty in parsed:
        qs = Product.objects.filter(pk=pid, status=Product.Status.ACTIVE).select_related(
            "seller", "category"
        )
        if select_for_update:
            qs = qs.select_for_update()
        p = qs.first()
        if not p:
            raise ValueError(f"Product {pid} not available.")
        if p.seller_id is not None and p.seller.status != Vendor.Status.APPROVED:
            raise ValueError(f"Product {pid} not available.")
        if strict_stock:
            if p.stock < qty:
                raise ValueError(f"Insufficient stock for {p.name}.")
        elif p.stock < qty:
            stock_warnings.append(
                {
                    "product_id": pid,
                    "name": p.name,
                    "requested": qty,
                    "available": p.stock,
                }
            )
        validate_child_may_purchase_product(user, p)
        unit_price = storefront_unit_price(p, flash_overrides=flash_overrides)
        line_total = (unit_price * qty).quantize(Decimal("0.01"))
        cart_subtotal += line_total
        list_subtotal += (p.price * qty).quantize(Decimal("0.01"))
        groups[p.seller_id].append((p, qty, unit_price, line_total))

    flash_product_ids = sorted(flash_overrides.keys())
    return ResolvedCheckoutCart(
        groups=dict(groups),
        flash_overrides=flash_overrides,
        flash_deal_by_product_id=flash_deal_by_product_id,
        cart_subtotal=cart_subtotal,
        list_subtotal=list_subtotal,
        flash_product_ids=flash_product_ids,
        stock_warnings=stock_warnings,
    )


def _lines_for_coupon_from_groups(
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
) -> list[tuple[Product, int, Decimal]]:
    out: list[tuple[Product, int, Decimal]] = []
    for _sid, glines in groups.items():
        for pr, q, unit, _lt in glines:
            out.append((pr, q, unit))
    return out


def storefront_merchandise_subtotal(
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
) -> Decimal:
    t = Decimal("0")
    for _sid, glines in groups.items():
        for _pr, q, unit, _lt in glines:
            t += unit * q
    return t.quantize(Decimal("0.01"))


def apply_coupon_split(
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
    raw_coupon: Any,
    *,
    strict_coupon: bool,
) -> tuple[
    Coupon | None,
    Decimal,
    str | None,
    dict[int | None, Decimal],
    Decimal,
]:
    """
    Returns coupon_obj, discount_total, coupon_error (if lenient invalid code),
    seller_discounts, eligible_subtotal (merchandise subtotal eligible for coupon, or full cart).
    """
    lines_for_coupon = _lines_for_coupon_from_groups(groups)
    code = (
        str(raw_coupon).strip()
        if raw_coupon is not None and str(raw_coupon).strip()
        else None
    )
    coupon_obj, discount_total, coupon_err = validate_and_compute_coupon(
        code,
        lines=lines_for_coupon,
    )
    if coupon_err:
        if strict_coupon:
            raise ValueError(coupon_err)
        base = storefront_merchandise_subtotal(groups)
        return None, Decimal("0"), coupon_err, {sid: Decimal("0") for sid in groups}, base

    seller_discounts = {sid: Decimal("0") for sid in groups}
    if coupon_obj is not None:
        seller_eligible: dict[int | None, Decimal] = defaultdict(Decimal)
        for sid, glines in groups.items():
            for pr, q, unit, _lt in glines:
                if line_eligible_for_coupon(coupon_obj, pr):
                    seller_eligible[sid] += unit * q
        seller_discounts.update(
            split_discount_across_sellers(discount_total, dict(seller_eligible))
        )
        eligible = eligible_subtotal_for_coupon(coupon_obj, lines_for_coupon)
    else:
        eligible = storefront_merchandise_subtotal(groups)

    return coupon_obj, discount_total, None, seller_discounts, eligible


def _parse_optional_weight_kg(raw: Any) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _unit_weight_kg_from_attributes(attrs: Any) -> float:
    """Per-unit kg from Product.attributes: weight_kg or weight (numeric)."""
    if not isinstance(attrs, dict):
        return 0.0
    for key in ("weight_kg", "weight"):
        v = attrs.get(key)
        if v is None:
            continue
        try:
            w = float(v)
            if w >= 0:
                return w
        except (TypeError, ValueError):
            continue
    return 0.0


def cart_weight_kg_from_groups(
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
) -> float:
    """Sum of qty × unit weight from product attributes (0 if no weights set)."""
    total = 0.0
    for _sid, glines in groups.items():
        for pr, qty, _u, _lt in glines:
            total += _unit_weight_kg_from_attributes(pr.attributes) * qty
    return total


def checkout_items_weight_kg(items: Any) -> float:
    """Sum qty × unit weight from `[{product_id, quantity}, ...]` for public shipping quotes."""
    if not isinstance(items, list) or not items:
        return 0.0
    parsed: list[tuple[int, int]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        pid = raw.get("product_id")
        qty = int(raw.get("quantity") or 0)
        if pid and qty >= 1:
            parsed.append((int(pid), qty))
    if not parsed:
        return 0.0
    pids = list({a for a, _ in parsed})
    by_id = {p.pk: p for p in Product.objects.filter(pk__in=pids)}
    total = 0.0
    for pid, qty in parsed:
        p = by_id.get(pid)
        if p:
            total += _unit_weight_kg_from_attributes(p.attributes) * qty
    return total


def resolve_checkout_weight_kg(
    request_data: dict,
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
    sh: ShippingSettings,
) -> float:
    """
    1) Explicit weight_kg (top-level or delivery dict) if valid.
    2) Else sum from cart product attributes if > 0.
    3) Else default_checkout_weight_kg.
    Clamped to [0, 500].
    """
    _ship_raw = request_data.get("delivery")
    _d = _ship_raw if isinstance(_ship_raw, dict) else {}
    _top = request_data
    override = _parse_optional_weight_kg(_top.get("weight_kg") or _d.get("weight_kg"))
    if override is not None:
        w = override
    else:
        cart_w = cart_weight_kg_from_groups(groups)
        if cart_w > 0:
            w = cart_w
        else:
            w = float(sh.default_checkout_weight_kg)
    return max(0.0, min(500.0, w))


def compute_delivery_allocation(
    request_data: dict,
    want_delivery: bool,
    cart_subtotal: Decimal,
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
) -> tuple[
    Decimal,
    dict[int | None, Decimal],
    ShippingZone | None,
    str | None,
    float,
    str | None,
]:
    """
    Returns delivery_fee_total, delivery_alloc, checkout_zone, error_message,
    delivery_weight_kg (0 if not delivering), shipping_method_id echo (None if zone-only).
    """
    if not want_delivery:
        z = {sid: Decimal("0") for sid in groups}
        return Decimal("0"), z, None, None, 0.0, None

    sh = ShippingSettings.load()
    _ship_raw = request_data.get("delivery")
    _d = _ship_raw if isinstance(_ship_raw, dict) else {}
    _top = request_data
    raw_zid = _top.get("shipping_zone_id") or _d.get("shipping_zone_id")
    if not raw_zid and sh.default_zone_id:
        raw_zid = sh.default_zone_id
    checkout_zone = ShippingZone.objects.filter(pk=raw_zid).first() if raw_zid else None
    if not checkout_zone or checkout_zone.status != ShippingZone.Status.ACTIVE:
        return (
            Decimal("0"),
            {sid: Decimal("0") for sid in groups},
            None,
            "Active shipping_zone_id is required for delivery.",
            0.0,
            None,
        )

    raw_mid = (
        _top.get("shipping_method_id")
        or _top.get("method_id")
        or _d.get("shipping_method_id")
        or _d.get("method_id")
    )
    method: ShippingMethod | None = None
    method_id_echo: str | None = None
    if raw_mid is not None and str(raw_mid).strip() != "":
        method = ShippingMethod.objects.filter(
            pk=raw_mid, status=ShippingMethod.Status.ACTIVE
        ).first()
        if not method:
            return (
                Decimal("0"),
                {sid: Decimal("0") for sid in groups},
                None,
                "Invalid or inactive shipping_method_id.",
                0.0,
                None,
            )
        method_id_echo = str(method.pk)

    weight_kg = resolve_checkout_weight_kg(request_data, groups, sh)
    raw_fee, _ = compute_shipping_fee(
        sh,
        checkout_zone,
        order_total=cart_subtotal,
        weight_kg=weight_kg,
        method=method,
    )
    delivery_fee_total = Decimal("0") if sh.seller_pays_shipping else raw_fee

    seller_subtotals = {
        sid: sum(lt for *_rest, lt in lines) for sid, lines in groups.items()
    }
    sorted_seller_ids = sorted(
        seller_subtotals.keys(),
        key=lambda sid: (-seller_subtotals[sid], 0 if sid is None else sid),
    )
    delivery_alloc: dict[int | None, Decimal] = {}
    acc_delivery = Decimal("0")
    if delivery_fee_total == 0 or cart_subtotal <= 0:
        for sid in groups:
            delivery_alloc[sid] = Decimal("0")
    else:
        for i, sid in enumerate(sorted_seller_ids):
            if i == len(sorted_seller_ids) - 1:
                delivery_alloc[sid] = (delivery_fee_total - acc_delivery).quantize(
                    Decimal("0.01")
                )
            else:
                part = (
                    seller_subtotals[sid] / cart_subtotal * delivery_fee_total
                ).quantize(Decimal("0.01"))
                delivery_alloc[sid] = part
                acc_delivery += part

    return delivery_fee_total, delivery_alloc, checkout_zone, None, weight_kg, method_id_echo


def build_orders_plan(
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
    seller_subtotals: dict[int | None, Decimal],
    delivery_alloc: dict[int | None, Decimal],
    seller_discounts: dict[int | None, Decimal],
    seller_sort_key,
) -> tuple[list[tuple], Decimal]:
    """orders_plan tuples (vendor, lines, v_sub, v_delivery, d_amt, v_total); grand total."""
    from core.models import Vendor

    orders_plan: list[
        tuple[
            Vendor | None,
            list[tuple[Product, int, Decimal, Decimal]],
            Decimal,
            Decimal,
            Decimal,
            Decimal,
        ]
    ] = []
    for seller_id in sorted(groups.keys(), key=seller_sort_key):
        lines = groups[seller_id]
        v_sub = seller_subtotals[seller_id]
        v_delivery = delivery_alloc[seller_id]
        d_amt = seller_discounts.get(seller_id, Decimal("0"))
        v_total = (v_sub - d_amt + v_delivery).quantize(Decimal("0.01"))
        if v_total < 0:
            v_total = Decimal("0")
        vendor = None if seller_id is None else Vendor.objects.get(pk=seller_id)
        orders_plan.append((vendor, lines, v_sub, v_delivery, d_amt, v_total))
    grand_total = sum((p[5] for p in orders_plan), Decimal("0"))
    return orders_plan, grand_total


def savings_from_flash_vs_product_sale(
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
) -> Decimal:
    """Merchandise savings from flash vs product-level sale price (per unit, before coupon)."""
    from core.services.product_pricing import effective_unit_price

    t = Decimal("0")
    for _sid, glines in groups.items():
        for p, qty, unit_flash, _lt in glines:
            eff = effective_unit_price(p)
            if eff > unit_flash:
                t += (eff - unit_flash) * qty
    return t.quantize(Decimal("0.01"))


def checkout_quote_line_rows(
    groups: dict[int | None, list[tuple[Product, int, Decimal, Decimal]]],
    seller_discounts: dict[int | None, Decimal],
    flash_deal_by_product_id: dict[int, int],
    seller_sort_key,
) -> list[dict[str, Any]]:
    from core.services.coupon_validation import split_seller_discount_across_lines
    from core.services.product_pricing import effective_unit_price

    rows: list[dict[str, Any]] = []
    for sid in sorted(groups.keys(), key=seller_sort_key):
        glines = groups[sid]
        disc = seller_discounts.get(sid, Decimal("0"))
        shares = split_seller_discount_across_lines(glines, disc)
        for j, (p, qty, unit_flash, line_tot) in enumerate(glines):
            list_u = p.price
            eff = effective_unit_price(p)
            fid = flash_deal_by_product_id.get(p.pk)
            coup = shares[j]
            line_final = (line_tot - coup).quantize(Decimal("0.01"))
            rows.append(
                {
                    "product_id": p.pk,
                    "quantity": qty,
                    "list_unit": float(list_u),
                    "unit_after_product_sale": float(eff),
                    "unit_after_flash": float(unit_flash),
                    "line_subtotal_after_flash": float(line_tot),
                    "flash_deal_id": fid,
                    "coupon_discount": float(coup),
                    "line_total": float(line_final),
                }
            )
    return rows
