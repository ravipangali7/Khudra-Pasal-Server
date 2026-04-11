"""Shared POS checkout: Order + OrderItem creation with explicit stock deduction."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from django.db import transaction

from core.models import Order, OrderItem, Product, User, Vendor
from core.services import product_service
from core.services.product_pricing import effective_unit_price


def gen_pos_order_number() -> str:
    for _ in range(20):
        cand = f"KP-{uuid4().hex[:12].upper()}"
        if len(cand) <= 20 and not Order.objects.filter(order_number=cand).exists():
            return cand
    return f"KP-{uuid4().hex[:12].upper()}"[:20]


@transaction.atomic
def create_pos_order(
    *,
    acting_vendor: Vendor | None,
    customer: User,
    items: list[dict],
    payment_method: str,
    tax_percent: Decimal,
    discount: Decimal,
    notes: str,
) -> Order:
    """Create a paid, delivered POS order, line items, and decrement stock (atomic)."""
    if not items:
        raise ValueError("items must be a non-empty list")

    lines: list[tuple[Product, int, Decimal, Decimal]] = []
    subtotal = Decimal("0")

    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("each item must be an object")
        pid = raw.get("product_id")
        try:
            qty = int(raw.get("quantity") or 0)
        except (TypeError, ValueError) as e:
            raise ValueError("invalid quantity") from e
        if pid is None or qty < 1:
            raise ValueError("each item needs product_id and quantity >= 1")

        qs = Product.objects.select_for_update().filter(pk=pid)
        if acting_vendor is not None:
            qs = qs.filter(seller=acting_vendor)
        p = qs.first()
        if not p:
            raise ValueError(
                f"Product {pid} not found"
                + (f" for this vendor." if acting_vendor is not None else ".")
            )

        if p.stock < qty:
            raise ValueError(f"Insufficient stock for {p.name}.")

        up_raw = raw.get("unit_price")
        if up_raw is not None and up_raw != "":
            unit_price = Decimal(str(up_raw)).quantize(Decimal("0.01"))
        else:
            unit_price = effective_unit_price(p)
        line_total = (unit_price * qty).quantize(Decimal("0.01"))
        subtotal += line_total
        lines.append((p, qty, unit_price, line_total))

    tax_amount = (subtotal * tax_percent / Decimal("100")).quantize(Decimal("0.01"))
    total = (subtotal + tax_amount - discount).quantize(Decimal("0.01"))
    if total < 0:
        total = Decimal("0")

    order = Order.objects.create(
        order_number=gen_pos_order_number(),
        customer=customer,
        seller=acting_vendor,
        status=Order.Status.DELIVERED,
        payment_method=payment_method,
        payment_status=Order.PaymentStatus.PAID,
        subtotal=subtotal,
        delivery_fee=Decimal("0"),
        discount_amount=discount,
        total=total,
        want_delivery=False,
        notes=notes[:500],
        is_pos_order=True,
    )
    seen_product_ids: set[int] = set()
    for p, qty, unit_price, line_total in lines:
        OrderItem.objects.create(
            order=order,
            product=p,
            quantity=qty,
            list_unit_price=p.price,
            flash_deal_id=None,
            unit_price=unit_price,
            coupon_discount_amount=Decimal("0"),
            total_price=line_total,
        )
        product_service.decrease_product_stock_for_pos(p.pk, qty)
        seen_product_ids.add(p.pk)
    for pid in seen_product_ids:
        p_sync = Product.objects.get(pk=pid)
        product_service.sync_stock_status(p_sync)
    return order
