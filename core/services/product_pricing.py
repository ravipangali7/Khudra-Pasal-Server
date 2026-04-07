"""Single source of truth for product list vs effective (sale) unit price."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Case, DecimalField, ExpressionWrapper, F, Q, When

from core.models import Product

_Q2 = Decimal("0.01")


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
