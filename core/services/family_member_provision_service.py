"""Direct family member provisioning (User + FamilyMember + wallets) for family portal."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from core.models import FamilyGroup, FamilyJoinRequest, FamilyMember, User
from core.phone_auth import normalize_nepal_phone
from core.services import family_service


def _valid_join_roles() -> frozenset[str]:
    return frozenset(c[0] for c in FamilyJoinRequest.Role.choices)


@transaction.atomic
def provision_family_member(
    *,
    acting_user: User,
    group: FamilyGroup,
    name: str,
    email: str,
    phone: str,
    role: str,
    spending_limit: Decimal,
    initial_balance: Decimal,
) -> FamilyMember:
    """
    Create or reactivate a FamilyMember with an existing or new User account.
    Mirrors accept_invite User.role and wallet behavior for the given member role.
    """
    if group.status != FamilyGroup.Status.ACTIVE:
        raise ValueError("Family group is not active.")
    if not family_service.user_can_manage_family_invites(acting_user, group):
        raise ValueError("You cannot add members to this family.")

    normalized = normalize_nepal_phone(phone)
    if not normalized:
        raise ValueError("Enter a valid Nepal mobile number.")
    if normalized == acting_user.phone:
        raise ValueError("You cannot add your own phone number.")

    leader = group.leader
    if leader and leader.phone == normalized:
        raise ValueError("The group leader is already a member.")

    role_norm = (role or FamilyJoinRequest.Role.CHILD).strip().lower()
    if role_norm not in _valid_join_roles():
        raise ValueError("Invalid role for a new family member.")

    member_role = role_norm
    lim = spending_limit or Decimal("0")
    ib = initial_balance or Decimal("0")

    user = User.objects.select_for_update().filter(phone=normalized).first()
    if not user:
        user = User(
            username=normalized,
            email=(email or "").strip()[:254],
            name=(name or "").strip()[:150] or "?",
            phone=normalized,
            role=User.Role.NORMAL,
        )
        user.set_unusable_password()
        user.save()
    else:
        u_updates: dict = {}
        if (name or "").strip():
            u_updates["name"] = (name or "").strip()[:150]
        em = (email or "").strip()[:254]
        if em:
            u_updates["email"] = em
        if u_updates:
            User.objects.filter(pk=user.pk).update(**u_updates)
            user.refresh_from_db()

    existing = (
        FamilyMember.objects.select_for_update()
        .filter(group=group, user=user)
        .first()
    )
    if existing and existing.status == FamilyMember.Status.ACTIVE:
        raise ValueError("This person is already an active member of this group.")

    if existing:
        FamilyMember.objects.filter(pk=existing.pk).update(
            role=member_role,
            status=FamilyMember.Status.ACTIVE,
            spending_limit_daily=Decimal("0.00"),
            spending_limit_weekly=Decimal("0.00"),
            spending_limit_monthly=lim,
            initial_balance=ib,
        )
        existing.refresh_from_db()
        fm = existing
    else:
        fm = FamilyMember.objects.create(
            group=group,
            user=user,
            role=member_role,
            status=FamilyMember.Status.ACTIVE,
            spending_limit_daily=Decimal("0.00"),
            spending_limit_weekly=Decimal("0.00"),
            spending_limit_monthly=lim,
            initial_balance=ib,
        )

    if member_role == FamilyMember.Role.CHILD:
        User.objects.filter(pk=user.pk).update(role=User.Role.CHILD)
    elif member_role in (
        FamilyMember.Role.SPOUSE,
        FamilyMember.Role.MANAGER,
    ):
        if user.role == User.Role.CHILD:
            User.objects.filter(pk=user.pk).update(role=User.Role.NORMAL)

    user.refresh_from_db()
    family_service.ensure_family_wallets_for_member(group, user, member_role)
    return fm
