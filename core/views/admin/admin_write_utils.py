"""Shared helpers for admin write endpoints and API responses."""

import ipaddress

from django.db.models import Q
from rest_framework.response import Response

from core.models import User


def absolute_media_url(request, file_field) -> str:
    if not file_field:
        return ""
    try:
        url = file_field.url
    except ValueError:
        return ""
    if url.startswith("http"):
        return url
    return request.build_absolute_uri(url)


def validation_error(message: str, field: str | None = None, status: int = 400):
    if field:
        return Response({field: [message], "detail": message}, status=status)
    return Response({"detail": message}, status=status)


def scalar_request_value(raw):
    """Normalize QueryDict / JSON values: first list item, strip strings."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def parse_int_pk(raw, field_name: str):
    """
    Parse a numeric primary key from client input.
    Returns (int | None, None): value or None if empty; never raises.
    Returns (None, Response) if non-empty but not a valid integer (400).
    """
    val = scalar_request_value(raw)
    if val is None:
        return None, None
    try:
        return int(val), None
    except (TypeError, ValueError):
        return None, validation_error(
            f"{field_name} must be a valid integer", field=field_name
        )


def resolve_user_by_pk_or_phone(raw, field_name: str = "user_id"):
    """
    Resolve User by numeric primary key and, if not found, by phone (digits / suffix).
    Returns (User, None) or (None, Response).
    """
    s = scalar_request_value(raw)
    if s is None:
        return None, validation_error(f"{field_name} is required", field=field_name)

    pk = None
    try:
        pk = int(s)
    except (ValueError, TypeError):
        pass

    user = User.objects.filter(pk=pk).first() if pk is not None else None

    if user is None:
        digits = "".join(c for c in s if c.isdigit())
        if len(digits) >= 7:
            q = Q(phone=s)
            if digits != s:
                q |= Q(phone=digits)
            if len(digits) >= 10:
                q |= Q(phone__endswith=digits[-10:])
            qs = User.objects.filter(q).distinct()
            n = qs.count()
            if n == 1:
                user = qs.first()
            elif n > 1:
                return None, validation_error(
                    "multiple users match this phone; use numeric user ID",
                    field=field_name,
                )

    if user is None:
        return None, validation_error("user not found", field=field_name)
    return user, None


def client_ip_from_request(request) -> str | None:
    """
    Best-effort client IP for audit logs. Uses X-Forwarded-For first hop when present.
    Returns None if missing or not a valid IPv4/IPv6 for GenericIPAddressField.
    """
    raw = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if raw:
        candidate = raw.split(",")[0].strip()
    else:
        candidate = (request.META.get("REMOTE_ADDR") or "").strip()
    if not candidate:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate
