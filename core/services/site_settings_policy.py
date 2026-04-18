"""Enforcement helpers for singleton :class:`~core.models.SiteSettings` toggles."""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from core.models import SiteSettings, User


def _site() -> SiteSettings:
    return SiteSettings.load()


def site_kyc_required_flag() -> bool:
    return bool(_site().kyc_required)


def pos_checkout_allowed() -> bool:
    return bool(_site().pos_enabled)


def storefront_orders_gate_response() -> Response | None:
    """
    Block ordering flows (checkout, quotes, cart writes, public shipping quote)
    when maintenance or temporary shop close is enabled.
    """
    s = _site()
    if s.maintenance_mode:
        return Response(
            {
                "detail": "The site is under maintenance. Ordering is unavailable.",
                "code": "maintenance_mode",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if s.temporary_shop_close:
        return Response(
            {
                "detail": "The shop is temporarily closed for new orders.",
                "code": "temporary_shop_close",
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def new_account_gate_response() -> Response | None:
    """Block OTP/OAuth flows that would create a brand-new user row."""
    s = _site()
    if s.maintenance_mode:
        return Response(
            {
                "detail": "The site is under maintenance. New registrations are unavailable.",
                "code": "maintenance_mode",
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    if not s.new_registrations:
        return Response(
            {
                "detail": "New registrations are currently disabled.",
                "code": "registrations_closed",
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def social_new_user_would_be_created(provider: str, provider_user_id: str, email: str) -> bool:
    """
    True if :func:`core.views.social_oauth._get_or_create_social_user` would insert a new User
    (no existing social link and no linkable email row).
    """
    sp = User.SocialProvider.GOOGLE if provider == "google" else User.SocialProvider.FACEBOOK
    pid = str(provider_user_id or "").strip()
    if not pid:
        return False
    if User.objects.filter(social_provider=sp, social_provider_id=pid).exists():
        return False
    em = (email or "").strip()
    if em and User.objects.filter(email__iexact=em).exclude(email="").exists():
        return False
    return True


def pos_disabled_response() -> Response:
    return Response(
        {
            "detail": "The POS system is disabled in site settings.",
            "code": "pos_disabled",
        },
        status=status.HTTP_403_FORBIDDEN,
    )
