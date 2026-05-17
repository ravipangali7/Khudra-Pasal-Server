"""Unified login with explicit portal/surface — token only when User.role matches that surface."""

from __future__ import annotations

from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from core.models import User
from core.phone_auth import authenticate_user_by_phone
from core.portal_roles import (
    PORTAL_ADMIN,
    PORTAL_CHILD,
    PORTAL_FAMILY,
    PORTAL_VENDOR,
    assert_portal_login_allowed,
    normalize_portal_key,
    primary_spa_redirect,
    user_allowed_for_portal_key,
    user_has_family_portal_access,
)


def resolve_surface_and_redirect(user: User) -> tuple[str, str]:
    """Best-effort SPA path hint (does not bypass portal role checks on login)."""
    path = primary_spa_redirect(user)
    if path.startswith("/admin"):
        return "admin", path
    if path.startswith("/vendor"):
        return "vendor", path
    return "portal", path


def user_payload(user: User, surface: str) -> dict:
    data = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "role": user.role,
        "kyc_status": user.kyc_status,
    }
    if surface == "vendor" and hasattr(user, "vendor_profile"):
        v = user.vendor_profile
        data["store_name"] = v.store_name
        data["store_slug"] = v.store_slug
        data["vendor_status"] = v.status
    if surface == "admin":
        data["is_staff"] = user.is_staff
        data["is_superuser"] = user.is_superuser
    return data


def build_auth_success_response(
    user: User, portal_key: str, request=None
) -> dict:
    """Issue token only after portal_key is validated (caller must validate first)."""
    if request is not None:
        from core.services.app_promotion_attribution import merge_attribution_from_request

        merge_attribution_from_request(user, request)
    surface, redirect = resolve_surface_and_redirect(user)
    if portal_key == PORTAL_FAMILY and user_has_family_portal_access(user):
        redirect = "/family-portal/dashboard"
    elif portal_key == PORTAL_CHILD and user_allowed_for_portal_key(user, PORTAL_CHILD):
        redirect = "/child-portal/dashboard"
    token, _ = Token.objects.get_or_create(user=user)
    return {
        "token": token.key,
        "surface": surface,
        "redirect": redirect,
        "portal": portal_key,
        "user": user_payload(user, surface),
    }


def build_auth_response_for_portal(
    user: User, portal_key: str, request=None
) -> dict | Response:
    denied = assert_portal_login_allowed(user, portal_key)
    if denied:
        return denied
    return build_auth_success_response(user, portal_key, request=request)


@api_view(["POST"])
@permission_classes([AllowAny])
def unified_login(request):
    phone = request.data.get("phone", "").strip()
    password = request.data.get("password", "")
    portal_key = normalize_portal_key(
        request.data.get("portal") or request.data.get("surface")
    )
    if not portal_key:
        return Response(
            {
                "detail": "Specify which portal you are signing in to: "
                '"portal" (customer), "family-portal", "child-portal", "vendor", or "admin".',
                "code": "portal_required",
            },
            status=400,
        )

    user = authenticate_user_by_phone(request, phone, password)
    if not user:
        return Response({"detail": "Invalid credentials."}, status=400)

    out = build_auth_response_for_portal(user, portal_key, request=request)
    if isinstance(out, Response):
        return out
    return Response(out)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def auth_session_home(request):
    """SPA shell guard: canonical home path for the authenticated user."""
    return Response({"redirect": primary_spa_redirect(request.user)})
