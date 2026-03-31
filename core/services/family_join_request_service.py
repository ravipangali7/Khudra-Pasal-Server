from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.models import (
    FamilyGroup,
    FamilyGroupPermission,
    FamilyInvite,
    FamilyJoinRequest,
    FamilyMember,
    User,
)
from core.services import family_member_provision_service, family_service, otp_service
from core.phone_auth import normalize_nepal_phone


def _invite_role_from_join_role(role: str) -> str:
    mapping = {
        FamilyJoinRequest.Role.CHILD: FamilyInvite.Role.CHILD,
        FamilyJoinRequest.Role.SPOUSE: FamilyInvite.Role.SPOUSE,
        FamilyJoinRequest.Role.GUEST: FamilyInvite.Role.GUEST,
        FamilyJoinRequest.Role.MANAGER: FamilyInvite.Role.MANAGER,
    }
    return mapping.get(role, FamilyInvite.Role.CHILD)


def _join_role_from_invite_role(invite_role: str) -> str:
    mapping = {
        FamilyInvite.Role.CHILD: FamilyJoinRequest.Role.CHILD,
        FamilyInvite.Role.SPOUSE: FamilyJoinRequest.Role.SPOUSE,
        FamilyInvite.Role.GUEST: FamilyJoinRequest.Role.GUEST,
        FamilyInvite.Role.MANAGER: FamilyJoinRequest.Role.MANAGER,
    }
    return mapping.get(invite_role, FamilyJoinRequest.Role.CHILD)


@transaction.atomic
def ensure_pending_join_request_after_invite_otp(
    *,
    user: User,
    invite: FamilyInvite,
) -> FamilyJoinRequest:
    """After OTP verification, ensure a pending join request exists; do not accept the invite."""
    normalized = normalize_nepal_phone(user.phone or "")
    if not normalized or normalized != invite.phone:
        raise ValueError("Phone does not match this invite.")

    existing = (
        FamilyJoinRequest.objects.select_for_update()
        .filter(
            invite=invite,
            group=invite.group,
            status=FamilyJoinRequest.Status.PENDING,
            phone=normalized,
        )
        .first()
    )
    if existing:
        return existing

    return FamilyJoinRequest.objects.create(
        group=invite.group,
        requested_by=user,
        name=(user.name or "").strip()[:150] or "?",
        email=(user.email or "").strip()[:254],
        phone=normalized,
        role=_join_role_from_invite_role(invite.role),
        age=None,
        status=FamilyJoinRequest.Status.PENDING,
        invite=invite,
        source=FamilyJoinRequest.Source.PARENT_INVITE,
    )


@transaction.atomic
def create_join_request_with_invite(
    *,
    parent: User,
    group: FamilyGroup,
    name: str,
    email: str,
    phone: str,
    role: str,
    age: int | None,
    spending_limit: Decimal,
    initial_balance: Decimal,
    invite_method: str,
) -> tuple[FamilyJoinRequest, FamilyInvite]:
    normalized = normalize_nepal_phone(phone)
    if not normalized:
        raise ValueError("Enter a valid Nepal mobile number.")
    invite_role = _invite_role_from_join_role(role)
    if invite_method not in (
        FamilyInvite.InviteMethod.LINK,
        FamilyInvite.InviteMethod.PHONE,
    ):
        invite_method = FamilyInvite.InviteMethod.PHONE

    inv = family_service.create_invite(
        parent,
        group,
        normalized,
        invite_role,
        spending_limit,
        initial_balance,
        invite_method=invite_method,
    )
    jr = FamilyJoinRequest.objects.create(
        group=group,
        requested_by=parent,
        name=name.strip()[:150],
        email=(email or "").strip()[:254],
        phone=normalized,
        role=role,
        age=age,
        status=FamilyJoinRequest.Status.PENDING,
        invite=inv,
        source=FamilyJoinRequest.Source.PARENT_INVITE,
    )
    return jr, inv


@transaction.atomic
def approve_join_request(
    *,
    reviewer: User,
    jr: FamilyJoinRequest,
) -> FamilyMember | None:
    if jr.status != FamilyJoinRequest.Status.PENDING:
        raise ValueError("This request is not pending.")
    if not family_service.user_can_manage_family_invites(reviewer, jr.group):
        raise ValueError("You cannot manage this request.")

    now = timezone.now()

    if (
        jr.source == FamilyJoinRequest.Source.SHARE_LINK
        or jr.join_link_id
    ):
        perm = FamilyGroupPermission.objects.filter(group=jr.group).first()
        lim = Decimal("0")
        if perm and perm.default_invite_spending_limit is not None:
            lim = perm.default_invite_spending_limit
        member = family_member_provision_service.provision_family_member(
            acting_user=reviewer,
            group=jr.group,
            name=jr.name,
            email=jr.email or "",
            phone=jr.phone,
            role=jr.role,
            spending_limit=lim,
            initial_balance=Decimal("0"),
        )
        jr.status = FamilyJoinRequest.Status.APPROVED
        jr.reviewed_by = reviewer
        jr.reviewed_at = now
        jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        return member

    user = User.objects.filter(phone=jr.phone).first()

    if user:
        if jr.group.leader_id == user.pk:
            raise ValueError("Group leader is already a member.")
        existing = FamilyMember.objects.filter(
            group=jr.group, user=user, status=FamilyMember.Status.ACTIVE
        ).first()
        if existing:
            jr.status = FamilyJoinRequest.Status.APPROVED
            jr.reviewed_by = reviewer
            jr.reviewed_at = now
            jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])
            return existing

        inv = jr.invite
        if not inv:
            raise ValueError("Invite missing for this request.")
        updated = FamilyInvite.objects.filter(
            pk=inv.pk, status=FamilyInvite.Status.PENDING
        ).update(status=FamilyInvite.Status.ACCEPTED)
        if not updated:
            raise ValueError("Invite is no longer pending.")
        inv.refresh_from_db()
        member = family_service.accept_invite(inv)
        jr.status = FamilyJoinRequest.Status.APPROVED
        jr.reviewed_by = reviewer
        jr.reviewed_at = now
        jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        return member

    jr.status = FamilyJoinRequest.Status.APPROVED
    jr.reviewed_by = reviewer
    jr.reviewed_at = now
    jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    return None


@transaction.atomic
def reject_join_request(
    *,
    reviewer: User,
    jr: FamilyJoinRequest,
) -> None:
    if jr.status != FamilyJoinRequest.Status.PENDING:
        raise ValueError("This request is not pending.")
    if not family_service.user_can_manage_family_invites(reviewer, jr.group):
        raise ValueError("You cannot manage this request.")

    now = timezone.now()
    jr.status = FamilyJoinRequest.Status.REJECTED
    jr.reviewed_by = reviewer
    jr.reviewed_at = now
    jr.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    if jr.invite_id:
        FamilyInvite.objects.filter(
            pk=jr.invite_id, status=FamilyInvite.Status.PENDING
        ).update(status=FamilyInvite.Status.EXPIRED)

    group_label = (jr.group.name or "").strip() or "the family"
    otp_service.send_template_sms(
        jr.phone,
        f"Your request to join {group_label} was not approved.",
    )
