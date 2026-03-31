"""Shared helpers for vendor portal API."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Sum
from rest_framework.response import Response

from core.models import User, Vendor, WalletWithdrawal
from core.views.admin.admin_write_utils import absolute_media_url


def vendor_or_error(request):
    if not request.user.is_authenticated:
        return None, Response({"detail": "Authentication required."}, status=401)
    vendor = getattr(request.user, "vendor_profile", None)
    if not vendor:
        return None, Response({"detail": "Vendor profile required."}, status=403)
    return vendor, None


def parse_reel_tags(raw):
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return [s]
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def get_or_create_pos_walkin_user() -> User:
    """Synthetic customer for vendor POS when no customer_id is sent."""
    phone = "9800000000"
    u = User.objects.filter(phone=phone).first()
    if u:
        return u
    user = User.objects.create_user(
        username=f"wk_{phone}",
        email="",
        password="!",
        name="POS Walk-in",
        phone=phone,
        role=User.Role.NORMAL,
    )
    user.set_unusable_password()
    user.save(update_fields=["password"])
    return user


def vendor_pending_withdrawal_total(vendor: Vendor) -> Decimal:
    try:
        w = vendor.wallet
    except ObjectDoesNotExist:
        return Decimal("0")
    agg = WalletWithdrawal.objects.filter(
        wallet=w, status=WalletWithdrawal.Status.PENDING
    ).aggregate(t=Sum("amount"))
    return agg["t"] or Decimal("0")


def media_url(request, field):
    return absolute_media_url(request, field)
