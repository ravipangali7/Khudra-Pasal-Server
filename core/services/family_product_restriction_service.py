"""Group-level product restrictions for family portal (ProductRestriction rows)."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from core.models import Category, FamilyGroup, ProductRestriction
from core.services import purchase_approval_service


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
    skip_requires_toggle_invalidation: bool = False,
) -> ProductRestriction | None:
    """
    Create/update or remove a group-level restriction (family_member=NULL).
    Removes the row when all values are defaults (no effective restriction).
    """
    category = Category.objects.filter(pk=category_id).first()
    if not category:
        raise ValueError("Invalid category_id.")

    existing = ProductRestriction.objects.filter(
        group=group,
        category_id=category_id,
        family_member__isnull=True,
    ).first()
    old_requires = bool(existing.requires_approval) if existing else False

    mp = max_price
    if mp is not None and mp != "":
        mp = Decimal(str(mp))
    else:
        mp = None

    def _maybe_invalidate(old_req: bool, new_req: bool) -> None:
        if skip_requires_toggle_invalidation or old_req == new_req:
            return
        purchase_approval_service.invalidate_purchase_approvals_for_category_requires_toggle(
            group=group, category_id=category_id
        )

    if _is_default_restriction(is_blocked, requires_approval, mp):
        _maybe_invalidate(old_requires, False)
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
        _maybe_invalidate(old_requires, bool(first.requires_approval))
        return first

    pr = ProductRestriction.objects.create(
        group=group,
        family_member=None,
        category=category,
        is_blocked=is_blocked,
        requires_approval=requires_approval,
        max_price=mp,
    )
    _maybe_invalidate(old_requires, bool(pr.requires_approval))
    return pr


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
    old_requires_by_cat = {
        r.category_id: bool(r.requires_approval)
        for r in ProductRestriction.objects.filter(
            group=group,
            family_member__isnull=True,
        )
    }
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
            skip_requires_toggle_invalidation=True,
        )
        if pr:
            out.append(pr)

    new_requires_by_cat = {
        r.category_id: bool(r.requires_approval)
        for r in ProductRestriction.objects.filter(
            group=group,
            family_member__isnull=True,
        )
    }
    for cid in set(old_requires_by_cat) | set(new_requires_by_cat):
        if old_requires_by_cat.get(cid, False) != new_requires_by_cat.get(cid, False):
            purchase_approval_service.invalidate_purchase_approvals_for_category_requires_toggle(
                group=group, category_id=cid
            )
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
