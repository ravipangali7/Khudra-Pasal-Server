from __future__ import annotations

import secrets
from datetime import timedelta
from django.db import transaction
from django.utils import timezone

from core.models import (
    FamilyGroup,
    FamilyJoinRequest,
    FamilyPortalJoinLink,
    User,
)
from core.phone_auth import normalize_nepal_phone


def link_is_usable(link: FamilyPortalJoinLink) -> bool:
    if link.revoked_at:
        return False
    if link.expires_at and link.expires_at < timezone.now():
        return False
    if link.group.status != FamilyGroup.Status.ACTIVE:
        return False
    return True


def get_active_link_for_group(group: FamilyGroup) -> FamilyPortalJoinLink | None:
    qs = (
        FamilyPortalJoinLink.objects.filter(group=group, revoked_at__isnull=True)
        .select_related("group")
        .order_by("-created_at")
    )
    for link in qs[:5]:
        if link_is_usable(link):
            return link
    return None


def resolve_public_link(token: str) -> FamilyPortalJoinLink | None:
    link = (
        FamilyPortalJoinLink.objects.filter(token=token)
        .select_related("group")
        .first()
    )
    if not link or not link_is_usable(link):
        return None
    return link


@transaction.atomic
def create_or_rotate_link(
    *,
    creator: User,
    group: FamilyGroup,
    default_role: str = "child",
    title: str = "",
    welcome_message: str = "",
    expires_in_days: int | None = None,
) -> FamilyPortalJoinLink:
    now = timezone.now()
    FamilyPortalJoinLink.objects.filter(group=group, revoked_at__isnull=True).update(
        revoked_at=now
    )
    token = secrets.token_hex(32)
    while FamilyPortalJoinLink.objects.filter(token=token).exists():
        token = secrets.token_hex(32)
    expires_at = None
    if expires_in_days is not None:
        d = max(1, min(int(expires_in_days), 90))
        expires_at = now + timedelta(days=d)
    role = (default_role or "child").strip().lower()
    valid_roles = frozenset({"child", "spouse", "guest", "manager"})
    if role not in valid_roles:
        role = "child"
    return FamilyPortalJoinLink.objects.create(
        group=group,
        token=token,
        created_by=creator,
        default_role=role,
        title=(title or "").strip()[:120],
        welcome_message=(welcome_message or "").strip()[:2000],
        expires_at=expires_at,
    )


@transaction.atomic
def submit_join_application(
    *,
    link: FamilyPortalJoinLink,
    applicant_user: User,
    name: str,
    email: str,
    phone: str,
    applicant_note: str = "",
) -> FamilyJoinRequest:
    if not link_is_usable(link):
        raise ValueError("This invitation link is no longer valid.")

    normalized = normalize_nepal_phone(phone)
    if not normalized:
        raise ValueError("Enter a valid Nepal mobile number.")

    user_phone = normalize_nepal_phone(applicant_user.phone or "")
    if not user_phone or user_phone != normalized:
        raise ValueError("Phone number must match the account you signed in with.")

    nm = (name or "").strip()
    if not nm:
        raise ValueError("Name is required.")
    if len(nm) > 150:
        raise ValueError("Name is too long.")

    pending = FamilyJoinRequest.objects.filter(
        group=link.group,
        phone=normalized,
        status=FamilyJoinRequest.Status.PENDING,
    ).exists()
    if pending:
        raise ValueError(
            "A pending join request for this phone number already exists for this family."
        )

    note = (applicant_note or "").strip()[:2000]

    return FamilyJoinRequest.objects.create(
        group=link.group,
        requested_by=applicant_user,
        name=nm[:150],
        email=(email or "").strip()[:254],
        phone=normalized,
        role=link.default_role,
        age=None,
        status=FamilyJoinRequest.Status.PENDING,
        invite=None,
        join_link=link,
        source=FamilyJoinRequest.Source.SHARE_LINK,
        applicant_note=note,
    )
