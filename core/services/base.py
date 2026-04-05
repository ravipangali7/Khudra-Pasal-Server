from __future__ import annotations

import uuid

from django.db.models import QuerySet

from core.models import User, Wallet


def new_wallet_txn_id() -> str:
    """Unique id for WalletTransaction.txn_id (max_length=30)."""
    return f"WT{uuid.uuid4().hex[:28]}"


def personal_wallet_qs(user: User) -> QuerySet[Wallet]:
    return Wallet.objects.filter(
        owner=user,
        type=Wallet.Type.PERSONAL,
        status=Wallet.Status.ACTIVE,
    ).order_by("id")


def get_or_create_personal_wallet(user: User) -> Wallet:
    w = personal_wallet_qs(user).first()
    if w:
        return w
    if user.role == User.Role.CHILD:
        from core.models import FamilyMember

        from core.services import family_portal_wallet_service

        fm = (
            FamilyMember.objects.filter(
                user=user,
                role=FamilyMember.Role.CHILD,
                status=FamilyMember.Status.ACTIVE,
            )
            .select_related("group")
            .first()
        )
        if fm and fm.group_id:
            mw = family_portal_wallet_service.get_member_family_wallet(
                fm.group, user
            )
            if mw:
                return mw
    return Wallet.objects.create(
        owner=user,
        type=Wallet.Type.PERSONAL,
        label="Personal",
        status=Wallet.Status.ACTIVE,
    )
