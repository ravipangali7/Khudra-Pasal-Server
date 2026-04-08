from __future__ import annotations

import secrets
import string
from typing import TYPE_CHECKING

from django.db import IntegrityError

from core.models import FamilyGroup, FamilyMember, User, Wallet, WalletTransferCode
from core.portal_roles import user_has_family_portal_access
from core.services import family_service
from core.services.base import get_or_create_personal_wallet
from core.services import family_portal_wallet_service

if TYPE_CHECKING:
    pass

_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 12


def normalize_transfer_code(raw: str) -> str:
    return (raw or "").strip().upper().replace(" ", "").replace("-", "")


def generate_transfer_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _family_groups_for_user(user: User) -> list[FamilyGroup]:
    led = list(
        FamilyGroup.objects.filter(leader=user, status=FamilyGroup.Status.ACTIVE)
    )
    member_groups = list(
        FamilyMember.objects.filter(
            user=user, status=FamilyMember.Status.ACTIVE
        ).select_related("group")
    )
    groups = {g.id: g for g in led}
    for fm in member_groups:
        groups[fm.group_id] = fm.group
    out = list(groups.values())
    out.sort(key=lambda g: (1 if g.is_platform_hub else 0, g.id))
    return out


def primary_family_group_for_hub(user: User) -> FamilyGroup | None:
    gl = _family_groups_for_user(user)
    if not gl:
        return None
    for g in gl:
        if family_service.user_can_manage_family_invites(user, g):
            return g
    return gl[0]


def resolve_recipient_wallet(user: User) -> Wallet:
    return get_or_create_personal_wallet(user)


def resolve_sender_wallet_for_hub_transfer(user: User) -> Wallet | None:
    if user.role == User.Role.CHILD:
        return get_or_create_personal_wallet(user)

    if user_has_family_portal_access(user):
        group = primary_family_group_for_hub(user)
        if group:
            w = family_portal_wallet_service.get_member_family_wallet(group, user)
            if w and w.owner_id == user.pk:
                return w

    if user.role in (User.Role.NORMAL, User.Role.PARENT, User.Role.CHILD):
        return get_or_create_personal_wallet(user)
    return None


def mask_display_name(name: str) -> str:
    name = (name or "").strip()
    if len(name) <= 2:
        return "***"
    return f"{name[0]}***{name[-1]}"


def get_or_create_transfer_code_row(user: User) -> tuple[WalletTransferCode, bool]:
    existing = WalletTransferCode.objects.filter(user=user).first()
    if existing:
        return existing, False
    for _ in range(64):
        code = generate_transfer_code()
        try:
            return WalletTransferCode.objects.create(user=user, code=code), True
        except IntegrityError:
            continue
    raise RuntimeError("Could not create wallet transfer code")
