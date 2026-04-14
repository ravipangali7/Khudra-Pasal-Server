"""
Resolve users by phone when clients send different formats (e.g. 98xxxxxxxx vs +977-98xxxxxxxx).
USERNAME_FIELD is phone — ModelBackend only matches exact DB values.
"""

from __future__ import annotations

from django.db.models import Q

from core.models import User


def _digits_only(value: str) -> str:
    # ASCII 0-9 only — avoids Unicode "digits" and keeps SMS gateway input predictable.
    return "".join(c for c in (value or "") if c in "0123456789")


def _nepal_mobile_core(digits: str) -> str | None:
    """
    Return ~10-digit Nepal mobile subscriber part (e.g. 98xxxxxxxx).
    Avoids slicing last 10 of 97798... which would drop the leading 9 of 98xxxxxxxx.
    """
    if not digits:
        return None
    if digits.startswith("977") and len(digits) > 3:
        rest = digits[3:]
        if len(rest) >= 10:
            return rest[:10]
        return rest
    if len(digits) == 10 and digits.startswith("9"):
        return digits
    if len(digits) == 11 and digits.startswith("09"):
        return digits[1:]
    if len(digits) >= 10:
        return digits[-10:]
    return None


def normalize_nepal_phone(phone_input: str) -> str | None:
    """Return canonical 10-digit Nepal mobile or None if invalid."""
    core = _nepal_mobile_core(_digits_only(phone_input))
    if not core or len(core) != 10 or not core.startswith("9"):
        return None
    return core


def phone_lookup_variants(phone_input: str) -> list[str]:
    """Build possible phone strings that might match User.phone."""
    raw = (phone_input or "").strip()
    variants: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    add(raw)

    digits = _digits_only(raw)
    if not digits:
        return variants

    add(digits)

    core = _nepal_mobile_core(digits)
    if core:
        add(core)
        add(f"+977{core}")
        add(f"977{core}")
        add(f"+977-{core}")
        if not core.startswith("0"):
            add(f"0{core}")

    return variants


def find_user_by_phone_input(phone_input: str) -> User | None:
    """Find first user whose phone equals any normalized variant."""
    variants = phone_lookup_variants(phone_input)
    if not variants:
        return None

    q = Q()
    for v in variants:
        q |= Q(phone=v)

    return User.objects.filter(q).first()


def authenticate_user_by_phone(request, phone_input: str, password: str) -> User | None:
    """
    Match user by flexible phone, then verify password and is_active.
    Same outcome as authenticate() for ModelBackend but tolerant of phone formatting.
    """
    user = find_user_by_phone_input(phone_input)
    if user is None:
        return None
    if not user.is_active:
        return None
    if not user.check_password(password):
        return None
    return user
