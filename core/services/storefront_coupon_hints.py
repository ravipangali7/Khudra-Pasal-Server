"""Batched coupon promo hints for storefront product cards (active coupons only)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.db.models import F, Q
from django.utils import timezone

from core.models import Coupon, Product
from core.services.coupon_validation import line_eligible_for_coupon


def coupon_hints_for_product_ids(product_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """
    For each active product id, list of {code, type, value} for coupons that would
    apply to that product (vendor/category/product whitelist rules).
    """
    ids = [int(x) for x in product_ids if x is not None]
    if not ids:
        return {}
    now = timezone.now()
    products = list(
        Product.objects.filter(pk__in=ids, status=Product.Status.ACTIVE).select_related(
            "category",
            "category__parent",
            "seller",
        )
    )
    pmap = {p.pk: p for p in products}
    coupons = list(
        Coupon.objects.filter(status=Coupon.Status.ACTIVE)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gte=now))
        .filter(Q(usage_limit__isnull=True) | Q(used_count__lt=F("usage_limit")))
        .select_related("vendor", "category")
        .prefetch_related("products")
    )
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for pid, pr in pmap.items():
        for c in coupons:
            if line_eligible_for_coupon(c, pr):
                out[pid].append(
                    {
                        "code": c.code,
                        "type": c.type,
                        "value": str(c.value),
                    }
                )
    return dict(out)
