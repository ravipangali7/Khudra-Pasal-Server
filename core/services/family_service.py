from __future__ import annotations

import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import (
    FamilyGroup,
    FamilyGroupPermission,
    FamilyInvite,
    FamilyMember,
    User,
    Wallet,
)
from core.phone_auth import normalize_nepal_phone

_PLATFORM_HUB_TYPES = frozenset(
    {
        FamilyGroup.Type.FLAT,
        FamilyGroup.Type.HOTEL,
        FamilyGroup.Type.HOSTEL,
    }
)


def get_platform_hub_group(group_type: str) -> FamilyGroup | None:
    """Singleton hub for flat/hotel/hostel (seeded by migration)."""
    if group_type not in _PLATFORM_HUB_TYPES:
        return None
    return (
        FamilyGroup.objects.filter(
            is_platform_hub=True,
            type=group_type,
            status=FamilyGroup.Status.ACTIVE,
        )
        .order_by("id")
        .first()
    )


def user_can_manage_family_invites(user: User, group: FamilyGroup) -> bool:
    if group.leader_id == user.pk:
        return True
    return FamilyMember.objects.filter(
        group=group,
        user=user,
        status=FamilyMember.Status.ACTIVE,
        role__in=[
            FamilyMember.Role.PARENT,
            FamilyMember.Role.SPOUSE,
            FamilyMember.Role.MANAGER,
        ],
    ).exists()


@transaction.atomic
def create_family_group_for_user(
    user: User,
    name: str,
    group_type: str | None = None,
) -> FamilyGroup:
    if user.role != User.Role.NORMAL:
        raise ValueError("Only normal customer accounts can create a family group.")
    if FamilyGroup.objects.filter(
        leader=user, status=FamilyGroup.Status.ACTIVE
    ).exists():
        raise ValueError("You already lead an active family group.")
    if FamilyMember.objects.filter(
        user=user, status=FamilyMember.Status.ACTIVE
    ).exists():
        raise ValueError("You are already a member of a family group.")

    gtype = group_type or FamilyGroup.Type.FAMILY

    if gtype in _PLATFORM_HUB_TYPES:
        hub = get_platform_hub_group(gtype)
        if not hub:
            raise ValueError("Shared group is not available for this type yet.")
        if FamilyMember.objects.filter(
            group=hub, user=user, status=FamilyMember.Status.ACTIVE
        ).exists():
            raise ValueError("You are already in this shared group.")
        FamilyGroupPermission.objects.get_or_create(group=hub)
        FamilyMember.objects.create(
            group=hub,
            user=user,
            role=FamilyMember.Role.PARENT,
            status=FamilyMember.Status.ACTIVE,
        )
        User.objects.filter(pk=user.pk).update(role=User.Role.PARENT)
        _ensure_family_wallets_for_member(hub, user, FamilyMember.Role.PARENT)
        return hub

    group = FamilyGroup.objects.create(
        name=name.strip()[:100],
        leader=user,
        type=gtype,
        status=FamilyGroup.Status.ACTIVE,
    )
    FamilyGroupPermission.objects.get_or_create(group=group)
    FamilyMember.objects.create(
        group=group,
        user=user,
        role=FamilyMember.Role.PARENT,
        status=FamilyMember.Status.ACTIVE,
    )
    User.objects.filter(pk=user.pk).update(role=User.Role.PARENT)

    Wallet.objects.create(
        owner=user,
        type=Wallet.Type.SHARED,
        label="Family wallet",
        family_group=group,
        status=Wallet.Status.ACTIVE,
    )
    Wallet.objects.create(
        owner=user,
        type=Wallet.Type.PARENT,
        label="Parent wallet",
        family_group=group,
        status=Wallet.Status.ACTIVE,
    )
    return group


def ensure_family_wallets_for_member(group: FamilyGroup, user: User, role: str) -> None:
    """Create child/parent family-scoped wallets for a member if missing."""
    _ensure_family_wallets_for_member(group, user, role)


def _ensure_family_wallets_for_member(group: FamilyGroup, user: User, role: str) -> None:
    if role == FamilyMember.Role.CHILD:
        if not Wallet.objects.filter(
            owner=user, family_group=group, type=Wallet.Type.CHILD
        ).exists():
            Wallet.objects.create(
                owner=user,
                type=Wallet.Type.CHILD,
                label="Child wallet",
                family_group=group,
                status=Wallet.Status.ACTIVE,
            )
    elif role in (
        FamilyMember.Role.PARENT,
        FamilyMember.Role.SPOUSE,
        FamilyMember.Role.MANAGER,
        FamilyMember.Role.GUEST,
    ):
        if not Wallet.objects.filter(
            owner=user, family_group=group
        ).exclude(type=Wallet.Type.VENDOR).exists():
            Wallet.objects.create(
                owner=user,
                type=Wallet.Type.PARENT,
                label="Family wallet",
                family_group=group,
                status=Wallet.Status.ACTIVE,
            )


@transaction.atomic
def create_invite(
    inviter: User,
    group: FamilyGroup,
    phone: str,
    role: str,
    spending_limit: Decimal,
    initial_balance: Decimal,
    *,
    invite_method: str = FamilyInvite.InviteMethod.PHONE,
    expires_in_days: int = 7,
) -> FamilyInvite:
    if group.status != FamilyGroup.Status.ACTIVE:
        raise ValueError("Family group is not active.")
    if not user_can_manage_family_invites(inviter, group):
        raise ValueError("You cannot invite members to this family.")
    normalized = normalize_nepal_phone(phone)
    if not normalized:
        raise ValueError("Enter a valid Nepal mobile number.")
    if normalized == inviter.phone:
        raise ValueError("You cannot invite your own phone number.")

    token = secrets.token_hex(32)
    while FamilyInvite.objects.filter(token=token).exists():
        token = secrets.token_hex(32)

    expires_at = timezone.now() + timedelta(days=max(1, min(expires_in_days, 30)))
    return FamilyInvite.objects.create(
        group=group,
        invited_by=inviter,
        invite_method=invite_method,
        phone=normalized,
        token=token,
        role=role,
        spending_limit=spending_limit,
        initial_balance=initial_balance,
        expires_at=expires_at,
        status=FamilyInvite.Status.PENDING,
    )


@transaction.atomic
def accept_invite(invite: FamilyInvite) -> FamilyMember | None:
    if invite.status != FamilyInvite.Status.ACCEPTED:
        return None
    user = None
    if invite.phone:
        user = User.objects.filter(phone=invite.phone).first()
    if not user:
        return None

    lim = invite.spending_limit or Decimal("0")
    member, created = FamilyMember.objects.get_or_create(
        group=invite.group,
        user=user,
        defaults={
            "role": invite.role,
            "status": FamilyMember.Status.ACTIVE,
            "spending_limit_daily": Decimal("0.00"),
            "spending_limit_weekly": Decimal("0.00"),
            "spending_limit_monthly": lim,
            "initial_balance": invite.initial_balance or Decimal("0.00"),
        },
    )
    if not created:
        FamilyMember.objects.filter(pk=member.pk).update(
            status=FamilyMember.Status.ACTIVE,
            spending_limit_daily=Decimal("0.00"),
            spending_limit_weekly=Decimal("0.00"),
            spending_limit_monthly=lim,
            initial_balance=invite.initial_balance or Decimal("0.00"),
            role=invite.role,
        )
        member.refresh_from_db()

    if invite.role == FamilyInvite.Role.CHILD:
        User.objects.filter(pk=user.pk).update(role=User.Role.CHILD)
    elif invite.role in (
        FamilyInvite.Role.SPOUSE,
        FamilyInvite.Role.MANAGER,
    ):
        if user.role == User.Role.CHILD:
            User.objects.filter(pk=user.pk).update(role=User.Role.NORMAL)
        # Spouse/manager use family portal via FamilyMember, not User.role.PARENT
    ensure_family_wallets_for_member(invite.group, user, invite.role)

    return member


@transaction.atomic
def freeze_group_wallets(group: FamilyGroup) -> None:
    if group.status != FamilyGroup.Status.FROZEN:
        return
    Wallet.objects.filter(family_group=group).update(status=Wallet.Status.FROZEN)


@transaction.atomic
def finalize_purchase_approval_request(request) -> None:
    from core.models import PurchaseApprovalRequest

    if request.status not in (
        PurchaseApprovalRequest.Status.APPROVED,
        PurchaseApprovalRequest.Status.REJECTED,
    ):
        return
    PurchaseApprovalRequest.objects.filter(
        pk=request.pk, responded_at__isnull=True
    ).update(responded_at=timezone.now())
