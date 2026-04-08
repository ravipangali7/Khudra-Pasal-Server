from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from core.models import FamilyGroup, FamilyMember, FamilyWalletCategory, User, Wallet, WalletTransaction
from core.services import wallet_service


def category_allows_member_role(
    category: FamilyWalletCategory | None, member_role: str
) -> bool:
    if category is None:
        return True
    roles = category.allowed_member_roles
    if not roles:
        return True
    return member_role in roles


def get_default_shared_wallet(group: FamilyGroup) -> Wallet | None:
    return (
        Wallet.objects.filter(
            family_group=group,
            type=Wallet.Type.SHARED,
            family_category__isnull=True,
            status=Wallet.Status.ACTIVE,
        )
        .order_by("id")
        .first()
    )


def get_category_shared_wallet(
    group: FamilyGroup, category: FamilyWalletCategory | None
) -> Wallet | None:
    if category is None:
        return get_default_shared_wallet(group)
    return (
        Wallet.objects.filter(
            family_group=group,
            type=Wallet.Type.SHARED,
            family_category=category,
            status=Wallet.Status.ACTIVE,
        )
        .order_by("id")
        .first()
    )


def ensure_default_shared_wallet(group: FamilyGroup, leader: User) -> Wallet:
    """Return the main family shared pool (no category), creating it if missing (legacy data)."""
    w = get_default_shared_wallet(group)
    if w:
        return w
    return Wallet.objects.create(
        owner=leader,
        type=Wallet.Type.SHARED,
        label="Family wallet",
        family_group=group,
        family_category=None,
        status=Wallet.Status.ACTIVE,
    )


def ensure_category_shared_wallet(
    group: FamilyGroup, category: FamilyWalletCategory, leader: User
) -> Wallet:
    """Return the active shared bucket for a category, creating it if missing (legacy data)."""
    w = get_category_shared_wallet(group, category)
    if w:
        return w
    return Wallet.objects.create(
        owner=leader,
        type=Wallet.Type.SHARED,
        label=category.name,
        family_group=group,
        family_category=category,
        status=Wallet.Status.ACTIVE,
    )


def get_member_family_wallet(group: FamilyGroup, user: User) -> Wallet | None:
    fm = (
        FamilyMember.objects.filter(group=group, user=user)
        .only("role")
        .first()
    )
    if not fm:
        return None
    if fm.role == FamilyMember.Role.CHILD:
        w = (
            Wallet.objects.filter(
                owner=user,
                family_group=group,
                type=Wallet.Type.CHILD,
                status=Wallet.Status.ACTIVE,
            )
            .order_by("id")
            .first()
        )
        if w:
            return w
    w = (
        Wallet.objects.filter(
            owner=user,
            family_group=group,
            type=Wallet.Type.PARENT,
            status=Wallet.Status.ACTIVE,
        )
        .order_by("id")
        .first()
    )
    if w:
        return w
    return (
        Wallet.objects.filter(owner=user, family_group=group)
        .exclude(type=Wallet.Type.VENDOR)
        .filter(status=Wallet.Status.ACTIVE)
        .order_by("id")
        .first()
    )


def _require_active(w: Wallet | None, label: str) -> Wallet:
    if not w:
        raise ValueError(f"{label} wallet not found.")
    if w.status != Wallet.Status.ACTIVE:
        raise ValueError(f"{label} wallet is not active.")
    return w


@transaction.atomic
def family_wallet_load(
    *,
    group: FamilyGroup,
    amount: Decimal,
    performed_by: User,
    category: FamilyWalletCategory | None = None,
    method: str = "topup",
) -> tuple[Wallet, WalletTransaction]:
    if category is None:
        w = ensure_default_shared_wallet(group, group.leader)
    else:
        w = ensure_category_shared_wallet(group, category, group.leader)
    wt = wallet_service.credit_wallet(
        w,
        amount,
        wtype=WalletTransaction.Type.TOPUP,
        description=f"Family wallet load ({method})",
        reference_type="family_wallet_load",
        reference_id=str(group.pk),
        performed_by=performed_by,
    )
    wallet_service.apply_topup_bonus_after_credit(
        w,
        amount,
        bonus_reference_id=wt.txn_id,
        performed_by=performed_by,
    )
    w.refresh_from_db()
    return w, wt


@transaction.atomic
def family_wallet_distribute(
    *,
    group: FamilyGroup,
    to_user: User,
    amount: Decimal,
    performed_by: User,
    category: FamilyWalletCategory | None = None,
) -> tuple[Wallet, Wallet, WalletTransaction, WalletTransaction]:
    if category is None:
        from_w = ensure_default_shared_wallet(group, group.leader)
    else:
        from_w = ensure_category_shared_wallet(group, category, group.leader)
    to_w = _require_active(get_member_family_wallet(group, to_user), "Member")
    out_t, in_t = wallet_service.execute_transfer(
        from_w,
        to_w,
        amount,
        performed_by=performed_by,
        reference_type="family_distribute",
        reference_id=str(group.pk),
        family_wallet_category=category,
    )
    from_w.refresh_from_db()
    to_w.refresh_from_db()
    return from_w, to_w, out_t, in_t


def _is_group_family_wallet(group: FamilyGroup, w: Wallet) -> bool:
    """True if w is an active family-scoped wallet (shared bucket or canonical member wallet)."""
    if w.vendor_id or w.family_group_id != group.pk or w.status != Wallet.Status.ACTIVE:
        return False
    if w.type == Wallet.Type.SHARED:
        if w.family_category_id is None:
            return True
        return FamilyWalletCategory.objects.filter(
            pk=w.family_category_id, group=group
        ).exists()
    for fm in FamilyMember.objects.filter(
        group=group, status=FamilyMember.Status.ACTIVE
    ).select_related("user"):
        mw = get_member_family_wallet(group, fm.user)
        if mw and mw.pk == w.pk:
            return True
    return False


def _child_blocked_sending_to_shared(
    group: FamilyGroup, from_wallet: Wallet, to_wallet: Wallet, performed_by: User
) -> bool:
    if to_wallet.type != Wallet.Type.SHARED:
        return False
    if not from_wallet.owner_id or performed_by.pk != from_wallet.owner_id:
        return False
    return FamilyMember.objects.filter(
        group=group,
        user_id=from_wallet.owner_id,
        role=FamilyMember.Role.CHILD,
        status=FamilyMember.Status.ACTIVE,
    ).exists()


@transaction.atomic
def family_wallet_transfer_group_wallets(
    *,
    group: FamilyGroup,
    from_wallet_id,
    to_wallet_id,
    amount: Decimal,
    performed_by: User,
    category: FamilyWalletCategory | None = None,
) -> tuple[Wallet, Wallet, WalletTransaction, WalletTransaction]:
    from_w = Wallet.objects.filter(pk=from_wallet_id).first()
    to_w = Wallet.objects.filter(pk=to_wallet_id).first()
    from_w = _require_active(from_w, "Source")
    to_w = _require_active(to_w, "Destination")
    if not _is_group_family_wallet(group, from_w) or not _is_group_family_wallet(group, to_w):
        raise ValueError("One or both wallets are not valid for this family group.")
    if _child_blocked_sending_to_shared(group, from_w, to_w, performed_by):
        raise ValueError("Cannot move funds from a child wallet into the family pool.")
    if from_w.pk == to_w.pk:
        raise ValueError("Cannot transfer to the same wallet.")
    out_t, in_t = wallet_service.execute_transfer(
        from_w,
        to_w,
        amount,
        performed_by=performed_by,
        reference_type="family_wallet_transfer",
        reference_id=str(group.pk),
        family_wallet_category=category,
    )
    from_w.refresh_from_db()
    to_w.refresh_from_db()
    return from_w, to_w, out_t, in_t


@transaction.atomic
def family_wallet_transfer_members(
    *,
    group: FamilyGroup,
    from_user: User,
    to_user: User,
    amount: Decimal,
    performed_by: User,
    category: FamilyWalletCategory | None = None,
    reference_type: str = "family_member_transfer",
) -> tuple[Wallet, Wallet, WalletTransaction, WalletTransaction]:
    from_w = _require_active(get_member_family_wallet(group, from_user), "Sender")
    to_w = _require_active(get_member_family_wallet(group, to_user), "Recipient")
    if from_w.pk == to_w.pk:
        raise ValueError("Cannot transfer to the same wallet.")
    out_t, in_t = wallet_service.execute_transfer(
        from_w,
        to_w,
        amount,
        performed_by=performed_by,
        reference_type=reference_type or "family_member_transfer",
        reference_id=str(group.pk),
        family_wallet_category=category,
    )
    from_w.refresh_from_db()
    to_w.refresh_from_db()
    return from_w, to_w, out_t, in_t


@transaction.atomic
def create_category_wallet(
    *,
    group: FamilyGroup,
    category: FamilyWalletCategory,
    leader: User,
) -> Wallet:
    if Wallet.objects.filter(
        family_group=group,
        family_category=category,
        type=Wallet.Type.SHARED,
    ).exists():
        raise ValueError("Wallet for this category already exists.")
    return Wallet.objects.create(
        owner=leader,
        type=Wallet.Type.SHARED,
        label=category.name,
        family_group=group,
        family_category=category,
        status=Wallet.Status.ACTIVE,
    )
