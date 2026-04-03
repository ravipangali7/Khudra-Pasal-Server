"""Enforce family product rules for child accounts (cart, checkout)."""

from __future__ import annotations

from decimal import Decimal

from core.models import FamilyGroupPermission, FamilyMember, Product, ProductRestriction, User


def _effective_unit_price(product: Product) -> Decimal:
    if product.discount_price is not None:
        return product.discount_price
    return product.price


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

    pr = (
        ProductRestriction.objects.filter(
            group=fm.group,
            family_member__isnull=True,
            category_id=product.category_id,
        )
        .first()
    )
    if not pr:
        return

    if pr.is_blocked:
        raise ValueError("This category is blocked for your account.")
    if pr.requires_approval:
        raise ValueError(
            "This category requires parent approval before you can purchase."
        )

    unit = _effective_unit_price(product)
    if pr.max_price is not None and unit > pr.max_price:
        raise ValueError(
            f"This product exceeds the maximum price (Rs. {pr.max_price}) allowed for this category."
        )
