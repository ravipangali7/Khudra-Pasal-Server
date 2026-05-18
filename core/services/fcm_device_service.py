"""Register and query per-device FCM tokens (many per user)."""

from __future__ import annotations

from typing import Iterable

from core.models import User, UserFcmDevice

_FCM_TOKEN_MAX_LEN = 8192


def _normalize_token(raw: str) -> str:
    return (raw or "").strip()


def register_user_fcm_token(
    user: User,
    token: str,
    *,
    platform: str = "",
) -> UserFcmDevice | None:
    """
    Upsert by token (no duplicates in DB). Reassigns token to [user] if it was on another account.
    """
    token = _normalize_token(token)
    if not token or len(token) > _FCM_TOKEN_MAX_LEN:
        return None

    plat = (platform or "").strip()[:16]
    if plat and plat not in dict(UserFcmDevice.Platform.choices):
        plat = ""

    UserFcmDevice.objects.filter(token=token).exclude(user=user).delete()

    device, _created = UserFcmDevice.objects.update_or_create(
        token=token,
        defaults={"user": user, "platform": plat},
    )
    return device


def fcm_tokens_for_user(user: User | int) -> list[str]:
    """All device tokens for one user (deduped), including legacy User.fcm_token if set."""
    uid = user.pk if isinstance(user, User) else int(user)
    tokens: list[str] = []
    seen: set[str] = set()
    for t in UserFcmDevice.objects.filter(user_id=uid).values_list("token", flat=True):
        t = _normalize_token(t)
        if t and t not in seen:
            seen.add(t)
            tokens.append(t)
    if isinstance(user, User):
        legacy = _normalize_token(getattr(user, "fcm_token", "") or "")
        if legacy and legacy not in seen:
            tokens.append(legacy)
    return tokens


def fcm_tokens_for_users(users: Iterable[User]) -> list[str]:
    """Flattened unique tokens for many users (broadcast / batch push)."""
    out: list[str] = []
    seen: set[str] = set()
    for u in users:
        for t in fcm_tokens_for_user(u):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def delete_fcm_tokens(tokens: Iterable[str]) -> int:
    """Remove invalid/unregistered tokens (after FCM send failures)."""
    normalized = [_normalize_token(t) for t in tokens]
    normalized = [t for t in normalized if t]
    if not normalized:
        return 0
    deleted, _ = UserFcmDevice.objects.filter(token__in=normalized).delete()
    User.objects.filter(fcm_token__in=normalized).update(fcm_token="")
    return deleted
