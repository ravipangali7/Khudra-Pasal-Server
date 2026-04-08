"""Single source of truth for product list vs effective (sale) unit price."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Case, DecimalField, ExpressionWrapper, F, Q, When
from django.utils import timezone

from core.models import FlashDeal, FlashDealProduct, Product

_Q2 = Decimal("0.01")


def _quantize_price(value: Decimal) -> Decimal:
    return value.quantize(_Q2, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FlashPricingRow:
    """Winning active flash line: final storefront unit and deal id for attribution."""

    unit: Decimal
    flash_deal_id: int


def flash_pricing_for_products(
    product_ids: Iterable[int],
    now,
) -> dict[int, FlashPricingRow]:
    """
    For each product id, the winning active flash deal line (if any).
    Picks FlashDealProduct with highest priority deal, then newest start_at.
    Respects FlashDeal.vendor: platform-wide (null) or must match product.seller_id.

    - If override_price is set: use it as the flash unit.
    - Else: flash_candidate = list_price × (1 − deal.discount_percent/100);
      charged unit = min(effective_unit_price(product), flash_candidate) so flash
      never raises the price above an existing product-level sale.
    """
    ids = [int(x) for x in product_ids if x is not None]
    if not ids:
        return {}
    qs = (
        FlashDealProduct.objects.filter(
            product_id__in=ids,
            flash_deal__status=FlashDeal.Status.ACTIVE,
            flash_deal__start_at__lte=now,
            flash_deal__end_at__gte=now,
        )
        .select_related("flash_deal", "product")
        .order_by("-flash_deal__priority", "-flash_deal__start_at", "pk")
    )
    out: dict[int, FlashPricingRow] = {}
    for row in qs:
        pid = row.product_id
        if pid in out:
            continue
        deal = row.flash_deal
        if deal.vendor_id is not None:
            sid = row.product.seller_id
            if sid is None or sid != deal.vendor_id:
                continue
        pr = row.product
        list_price = pr.price
        eff = effective_unit_price(pr)
        if row.override_price is not None:
            unit = _quantize_price(row.override_price)
        else:
            pct = deal.discount_percent
            if pct is None or pct <= 0:
                continue
            if pct >= 100:
                flash_candidate = Decimal("0.00")
            else:
                flash_candidate = list_price * (Decimal(100) - pct) / Decimal(100)
            flash_candidate = _quantize_price(flash_candidate)
            unit = min(eff, flash_candidate)
        out[pid] = FlashPricingRow(
            unit=_quantize_price(unit),
            flash_deal_id=deal.pk,
        )
    return out


def flash_override_prices_for_products(
    product_ids: Iterable[int],
    now,
) -> dict[int, Decimal]:
    """Map product_id → flash storefront unit (compat alias for flash_pricing_for_products)."""
    return {pid: row.unit for pid, row in flash_pricing_for_products(product_ids, now).items()}


def flash_deal_ids_for_products(
    product_ids: Iterable[int],
    now,
) -> dict[int, int]:
    """Map product_id → winning flash deal pk (subset of products with an active flash line)."""
    return {pid: row.flash_deal_id for pid, row in flash_pricing_for_products(product_ids, now).items()}


def storefront_unit_price(
    product: Product,
    *,
    now=None,
    flash_overrides: dict[int, Decimal] | None = None,
) -> Decimal:
    """
    Storefront / checkout unit price: product discount (effective_unit_price), then
    flash deal override_price when an active flash deal sets it for this product.
    """
    base = effective_unit_price(product)
    if flash_overrides is not None:
        if product.pk in flash_overrides:
            return _quantize_price(flash_overrides[product.pk])
        return base
    t = now if now is not None else timezone.now()
    one = flash_pricing_for_products([product.pk], t)
    if product.pk in one:
        return one[product.pk].unit
    return base


def effective_unit_price(product: Product) -> Decimal:
    """
    Sale unit price: applies discount_type + discount to list `price`.
    When no discount is configured, returns `price`.
    """
    list_price = product.price
    dtype = (product.discount_type or "").strip()
    disc = product.discount
    if not dtype or disc is None:
        return list_price
    if disc <= 0:
        return list_price
    if dtype == Product.DiscountType.PERCENTAGE:
        if disc >= 100:
            return Decimal("0.00").quantize(_Q2, rounding=ROUND_HALF_UP)
        eff = list_price * (Decimal(100) - disc) / Decimal(100)
        return eff.quantize(_Q2, rounding=ROUND_HALF_UP)
    if dtype == Product.DiscountType.FLAT:
        eff = list_price - disc
        if eff < 0:
            return Decimal("0.00").quantize(_Q2, rounding=ROUND_HALF_UP)
        return eff.quantize(_Q2, rounding=ROUND_HALF_UP)
    return list_price


def has_product_discount(product: Product) -> bool:
    eff = effective_unit_price(product)
    return eff < product.price


def validate_and_set_product_discount(
    product: Product,
    *,
    discount_type_raw: str | None,
    discount_raw,
) -> None:
    """
    Parses discount_type (flat|percentage|empty) and discount value.
    Mutates product.discount_type and product.discount.
    Raises ValueError with a user-facing message on invalid input.
    """
    dt = (discount_type_raw or "").strip().lower()
    if dt in ("", "none", "null"):
        product.discount_type = ""
        product.discount = None
        return

    if dt not in (Product.DiscountType.FLAT, Product.DiscountType.PERCENTAGE):
        raise ValueError("discount_type must be flat, percentage, or empty.")

    if discount_raw is None or discount_raw == "":
        raise ValueError("discount is required when discount_type is set.")

    from decimal import InvalidOperation

    try:
        disc = Decimal(str(discount_raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("discount must be a valid number.")

    if disc <= 0:
        raise ValueError("discount must be greater than zero.")

    list_price = product.price
    if dt == Product.DiscountType.PERCENTAGE:
        if disc > 100:
            raise ValueError("Percentage discount cannot exceed 100%.")
    else:
        if disc >= list_price:
            raise ValueError("Flat discount must be less than list price.")

    eff = _effective_for_values(list_price, dt, disc)
    if eff <= 0:
        raise ValueError("Discount would reduce the price to zero or below.")

    product.discount_type = dt
    product.discount = disc.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def product_effective_price_case():
    """
    ORM Case expression matching effective_unit_price() for use in filters/ordering.
    """
    return Case(
        When(
            Q(discount_type="") | Q(discount__isnull=True) | Q(discount__lte=0),
            then=F("price"),
        ),
        When(
            discount_type=Product.DiscountType.FLAT,
            then=F("price") - F("discount"),
        ),
        When(
            discount_type=Product.DiscountType.PERCENTAGE,
            then=ExpressionWrapper(
                F("price") * (Decimal(100) - F("discount")) / Decimal(100),
                output_field=DecimalField(max_digits=14, decimal_places=4),
            ),
        ),
        default=F("price"),
        output_field=DecimalField(max_digits=14, decimal_places=4),
    )


def _effective_for_values(list_price: Decimal, dtype: str, disc: Decimal) -> Decimal:
    if dtype == Product.DiscountType.PERCENTAGE:
        if disc >= 100:
            return Decimal("0.00").quantize(_Q2, rounding=ROUND_HALF_UP)
        eff = list_price * (Decimal(100) - disc) / Decimal(100)
        return eff.quantize(_Q2, rounding=ROUND_HALF_UP)
    eff = list_price - disc
    if eff < 0:
        return Decimal("0.00").quantize(_Q2, rounding=ROUND_HALF_UP)
    return eff.quantize(_Q2, rounding=ROUND_HALF_UP)
