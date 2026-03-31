"""Group-level product restrictions for family portal (ProductRestriction rows)."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from core.models import Category, FamilyGroup, ProductRestriction


def _is_default_restriction(is_blocked: bool, requires_approval: bool, max_price) -> bool:
    return not is_blocked and not requires_approval and max_price is None


@transaction.atomic
def upsert_group_level_restriction(
    *,
    group: FamilyGroup,
    category_id: int,
    is_blocked: bool = False,
    requires_approval: bool = False,
    max_price=None,
) -> ProductRestriction | None:
    """
    Create/update or remove a group-level restriction (family_member=NULL).
    Removes the row when all values are defaults (no effective restriction).
    """
    category = Category.objects.filter(pk=category_id).first()
    if not category:
        raise ValueError("Invalid category_id.")

    mp = max_price
    if mp is not None and mp != "":
        mp = Decimal(str(mp))
    else:
        mp = None

    if _is_default_restriction(is_blocked, requires_approval, mp):
        deleted, _ = ProductRestriction.objects.filter(
            group=group,
            category_id=category_id,
            family_member__isnull=True,
        ).delete()
        return None if deleted else None

    qs = ProductRestriction.objects.filter(
        group=group,
        category_id=category_id,
        family_member__isnull=True,
    )
    first = qs.first()
    if qs.count() > 1:
        qs.exclude(pk=first.pk).delete()
        first = qs.first()

    if first:
        first.is_blocked = is_blocked
        first.requires_approval = requires_approval
        first.max_price = mp
        first.save(
            update_fields=["is_blocked", "requires_approval", "max_price"]
        )
        return first

    return ProductRestriction.objects.create(
        group=group,
        family_member=None,
        category=category,
        is_blocked=is_blocked,
        requires_approval=requires_approval,
        max_price=mp,
    )


@transaction.atomic
def replace_group_level_restrictions(
    *,
    group: FamilyGroup,
    rules: list[dict],
) -> list[ProductRestriction]:
    """
    Replace all group-level restrictions with the given list.
    Each item: category_id, is_blocked, requires_approval, max_price (optional).
    """
    ProductRestriction.objects.filter(
        group=group,
        family_member__isnull=True,
    ).delete()

    out: list[ProductRestriction] = []
    seen_cat: set[int] = set()
    for item in rules:
        cid = int(item["category_id"])
        if cid in seen_cat:
            continue
        seen_cat.add(cid)
        is_blocked = bool(item.get("is_blocked", False))
        requires_approval = bool(item.get("requires_approval", False))
        mp = item.get("max_price", None)
        if _is_default_restriction(is_blocked, requires_approval, mp):
            continue
        pr = upsert_group_level_restriction(
            group=group,
            category_id=cid,
            is_blocked=is_blocked,
            requires_approval=requires_approval,
            max_price=mp,
        )
        if pr:
            out.append(pr)
    return out


def list_group_level_restrictions(*, group: FamilyGroup) -> list[ProductRestriction]:
    return list(
        ProductRestriction.objects.filter(
            group=group,
            family_member__isnull=True,
        )
        .select_related("category")
        .order_by("category__name", "id")
    )
