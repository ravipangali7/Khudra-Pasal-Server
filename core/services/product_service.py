from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Avg, Count, F

from core.models import OrderItem, Product, ProductApproval, ProductReview


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
    updated = Product.objects.filter(
        pk=order_item.product_id,
        stock__gte=order_item.quantity,
    ).update(stock=F("stock") - order_item.quantity)
    if not updated:
        raise ValueError("Insufficient stock for order line")


@transaction.atomic
def restore_line_stock(order_item: OrderItem) -> None:
    Product.objects.filter(pk=order_item.product_id).update(
        stock=F("stock") + order_item.quantity
    )
