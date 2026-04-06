"""Enforce family product rules for child accounts (cart, checkout)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from core.models import Category, FamilyGroupPermission, FamilyMember, Product, ProductRestriction, User

if TYPE_CHECKING:
    pass


def _effective_unit_price(product: Product) -> Decimal:
    if product.discount_price is not None:
        return product.discount_price
    return product.price


def collect_ancestor_category_ids(category: Category) -> list[int]:
    """Leaf-to-root category PKs (includes leaf)."""
    out: list[int] = []
    cur: Category | None = category
    while cur is not None:
        out.append(cur.pk)
        cur = cur.parent
    return out


@dataclass(frozen=True)
class MergedProductRestriction:
    is_blocked: bool
    requires_approval: bool
    max_price: Decimal | None


def merge_product_restrictions(rows: list[ProductRestriction]) -> MergedProductRestriction | None:
    if not rows:
        return None
    blocked = any(r.is_blocked for r in rows)
    needs_appr = any(r.requires_approval for r in rows)
    caps = [r.max_price for r in rows if r.max_price is not None]
    min_cap: Decimal | None = min(caps) if caps else None
    return MergedProductRestriction(
        is_blocked=blocked,
        requires_approval=needs_appr,
        max_price=min_cap,
    )


def resolve_merged_restriction_for_product(
    *, group_id: int, category: Category
) -> MergedProductRestriction | None:
    ancestor_ids = collect_ancestor_category_ids(category)
    rows = list(
        ProductRestriction.objects.filter(
            group_id=group_id,
            family_member__isnull=True,
            category_id__in=ancestor_ids,
        )
    )
    return merge_product_restrictions(rows)


def child_has_active_purchase_approval(user: User, product: Product) -> bool:
    """True if an unconsumed APPROVED PurchaseApprovalRequest exists for this child+product."""
    from core.models import PurchaseApprovalRequest

    if user.role != User.Role.CHILD:
        return False
    unit = _effective_unit_price(product)
    return PurchaseApprovalRequest.objects.filter(
        child=user,
        product=product,
        status=PurchaseApprovalRequest.Status.APPROVED,
        consumed_at__isnull=True,
        amount__gte=unit,
    ).exists()


def validate_child_may_purchase_product(user: User, product: Product) -> None:
    """
    Raise ValueError with a user-facing message if this product may not be purchased.

    Non-child users are always allowed. Child users without an active CHILD FamilyMember
    row match portal_child_rules (no restrictions).
    """
    if user.role != User.Role.CHILD:
        return

    fm = (
        FamilyMember.objects.filter(
            user=user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .select_related("group")
        .first()
    )
    if not fm:
        return

    perm, _ = FamilyGroupPermission.objects.get_or_create(group=fm.group)
    if not perm.allow_online_purchases:
        raise ValueError("Online purchases are turned off for your account.")

    # Ensure category + parent chain for ancestor walk
    category = product.category
    if category is None:
        return
    merged = resolve_merged_restriction_for_product(group_id=fm.group_id, category=category)
    if merged is None:
        return

    if merged.is_blocked:
        raise ValueError("This category is blocked for your account.")
    if merged.requires_approval and not child_has_active_purchase_approval(user, product):
        raise ValueError(
            "This category requires parent approval before you can purchase."
        )

    unit = _effective_unit_price(product)
    if merged.max_price is not None and unit > merged.max_price:
        raise ValueError(
            f"This product exceeds the maximum price (Rs. {merged.max_price}) allowed for this category."
        )
