from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Avg, Count, F

from core.models import OrderItem, Product, ProductApproval, ProductReview


def increase_product_stock(product_id: int, quantity: int) -> None:
    """Increase stock for physical products; no-op for digital. Use for procurement receipts."""
    if quantity < 1:
        return
    p = Product.objects.select_for_update().filter(pk=product_id).first()
    if not p:
        raise ValueError("Product not found")
    if p.type == Product.Type.DIGITAL:
        return
    Product.objects.filter(pk=product_id).exclude(type=Product.Type.DIGITAL).update(
        stock=F("stock") + quantity
    )


def decrease_product_stock(product_id: int, quantity: int) -> None:
    """Atomically decrement stock for physical products; no-op for non-physical.

    Call inside an existing transaction; locks the product row then applies an F() update.
    Raises ValueError if the product is missing or stock is insufficient.
    """
    if quantity < 1:
        return
    p = Product.objects.select_for_update().filter(pk=product_id).first()
    if not p:
        raise ValueError("Product not found")
    if p.type == Product.Type.DIGITAL:
        return
    updated = Product.objects.filter(
        pk=product_id,
        stock__gte=quantity,
    ).exclude(type=Product.Type.DIGITAL).update(stock=F("stock") - quantity)
    if not updated:
        raise ValueError("Insufficient stock for product")


@transaction.atomic
def apply_product_approval(approval: ProductApproval) -> None:
    product = approval.product
    if approval.status == ProductApproval.Status.APPROVED:
        if approval.type == ProductApproval.Type.NEW:
            Product.objects.filter(pk=product.pk).update(status=Product.Status.ACTIVE)
        else:
            Product.objects.filter(pk=product.pk).update(status=Product.Status.ACTIVE)
    elif approval.status == ProductApproval.Status.DENIED:
        if approval.type == ProductApproval.Type.NEW:
            Product.objects.filter(pk=product.pk).update(status=Product.Status.DRAFT)


@transaction.atomic
def sync_stock_status(product: Product) -> None:
    if product.stock == 0 and product.status == Product.Status.ACTIVE:
        Product.objects.filter(pk=product.pk).update(status=Product.Status.OUT_OF_STOCK)
    elif product.stock > 0 and product.status == Product.Status.OUT_OF_STOCK:
        Product.objects.filter(pk=product.pk).update(status=Product.Status.ACTIVE)


@transaction.atomic
def refresh_product_rating(product: Product) -> None:
    agg = ProductReview.objects.filter(
        product=product,
        status=ProductReview.Status.APPROVED,
    ).aggregate(
        avg=Avg("rating"),
        cnt=Count("id"),
    )
    avg = agg["avg"]
    cnt = agg["cnt"] or 0
    if cnt and avg is not None:
        rating = Decimal(str(avg)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        rating = Decimal("0.00")
    Product.objects.filter(pk=product.pk).update(rating=rating, review_count=cnt)


@transaction.atomic
def deduct_line_stock(order_item: OrderItem) -> None:
    try:
        decrease_product_stock(order_item.product_id, order_item.quantity)
    except ValueError as e:
        raise ValueError("Insufficient stock for order line") from e


@transaction.atomic
def restore_line_stock(order_item: OrderItem) -> None:
    Product.objects.filter(pk=order_item.product_id).update(
        stock=F("stock") + order_item.quantity
    )
