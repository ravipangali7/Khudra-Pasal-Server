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
    return Wallet.objects.create(
        owner=user,
        type=Wallet.Type.PERSONAL,
        label="Personal",
        status=Wallet.Status.ACTIVE,
    )
