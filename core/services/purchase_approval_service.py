"""Child purchase approval requests (family leader approves before checkout)."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import FamilyMember, Product, PurchaseApprovalRequest, User
from core.services import notification_service
from core.services.child_shopping_guard import (
    _effective_unit_price,
    resolve_merged_restriction_for_product,
)


def create_child_purchase_request(
    *, child: User, product_id: int, note: str = ""
) -> PurchaseApprovalRequest:
    fm = (
        FamilyMember.objects.filter(
            user=child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .select_related("group")
        .first()
    )
    if not fm:
        raise ValueError("Child profile not found.")

    product = (
        Product.objects.filter(
            pk=product_id,
            status=Product.Status.ACTIVE,
            seller__isnull=False,
        )
        .select_related("category")
        .first()
    )
    if not product:
        raise ValueError("Product not available.")

    merged = resolve_merged_restriction_for_product(
        group_id=fm.group_id, category=product.category
    )
    if merged is None or merged.is_blocked:
        raise ValueError("This product cannot be requested for purchase under your family rules.")
    if not merged.requires_approval:
        raise ValueError("This product does not require parent approval.")

    if PurchaseApprovalRequest.objects.filter(
        child=child,
        product=product,
        status=PurchaseApprovalRequest.Status.PENDING,
    ).exists():
        raise ValueError("You already have a pending request for this product.")

    unit = _effective_unit_price(product)
    leader = fm.group.leader
    par = PurchaseApprovalRequest.objects.create(
        child=child,
        parent=leader,
        product=product,
        amount=unit,
        note=(note or "")[:255],
        status=PurchaseApprovalRequest.Status.PENDING,
    )
    notification_service.notify_parent_purchase_approval_requested(par)
    return par


@transaction.atomic
def consume_purchase_approvals_after_checkout(
    user: User, product_qty_pairs: list[tuple[Product, int]]
) -> None:
    """Mark approved requests consumed (one approval unit per purchased quantity)."""
    if user.role != User.Role.CHILD or not product_qty_pairs:
        return
    now = timezone.now()
    for product, qty in product_qty_pairs:
        remaining = int(qty)
        while remaining > 0:
            row = (
                PurchaseApprovalRequest.objects.select_for_update()
                .filter(
                    child=user,
                    product=product,
                    status=PurchaseApprovalRequest.Status.APPROVED,
                    consumed_at__isnull=True,
                )
                .order_by("id")
                .first()
            )
            if not row:
                break
            row.consumed_at = now
            row.save(update_fields=["consumed_at"])
            remaining -= 1


@transaction.atomic
def approve_or_reject_request(
    *,
    acting_parent: User,
    request_id: int,
    status: str,
    parent_note: str = "",
) -> PurchaseApprovalRequest:
    if status not in (
        PurchaseApprovalRequest.Status.APPROVED,
        PurchaseApprovalRequest.Status.REJECTED,
    ):
        raise ValueError("status must be approved or rejected.")

    par = (
        PurchaseApprovalRequest.objects.select_for_update()
        .filter(pk=request_id)
        .select_related("child", "product", "parent")
        .first()
    )
    if not par:
        raise ValueError("Request not found.")
    if par.parent_id != acting_parent.pk:
        raise ValueError("You cannot update this request.")
    if par.status != PurchaseApprovalRequest.Status.PENDING:
        raise ValueError("This request is no longer pending.")

    par.status = status
    par.parent_note = (parent_note or "")[:255]
    par.responded_at = timezone.now()
    par.save(update_fields=["status", "parent_note", "responded_at"])
    notification_service.notify_child_purchase_approval_decision(par)
    return par
