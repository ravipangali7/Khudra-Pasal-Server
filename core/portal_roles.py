"""
Portal surface → allowed User.role (see TASK-admin-portal-login-roles.md).
Login must validate role before issuing that surface's session/token.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework.response import Response

from core.models import EmployeeProfile, FamilyGroup, FamilyMember, User, Vendor

# API / URL namespace keys (match urlpatterns prefixes under /api/)
PORTAL_ADMIN = "admin"
PORTAL_VENDOR = "vendor"
PORTAL_MAIN = "portal"
PORTAL_FAMILY = "family-portal"
PORTAL_CHILD = "child-portal"

VALID_PORTAL_KEYS = frozenset(
    {PORTAL_ADMIN, PORTAL_VENDOR, PORTAL_MAIN, PORTAL_FAMILY, PORTAL_CHILD}
)

_VENDOR_STAFF_ROLES = frozenset(
    {
        User.Role.STAFF,
        User.Role.FINANCE,
        User.Role.MODERATOR,
        User.Role.VIEWER,
    }
)

_SUGGEST = {
    PORTAL_ADMIN: "Use the administration login at /admin (API: /api/admin/auth/login/).",
    PORTAL_VENDOR: "Use the vendor login at /vendor (API: /api/vendor/auth/login/).",
    PORTAL_MAIN: "Use the customer portal login at /portal (API: /api/portal/auth/login/).",
    PORTAL_FAMILY: "Use the family portal login at /family-portal (API: /api/family-portal/auth/login/).",
    PORTAL_CHILD: "Use the child portal login at /child-portal (API: /api/child-portal/auth/login/).",
}


def user_allowed_for_vendor_portal(user: User) -> bool:
    """Staff/finance/moderator/viewer, or seller with a vendor profile (role often normal)."""
    if user.role in _VENDOR_STAFF_ROLES:
        return True
    if Vendor.objects.filter(user_id=user.pk).exists():
        return True
    return False


def user_allowed_for_admin_portal(user: User) -> bool:
    """
    Super Admin (role + Django flags), or active EmployeeProfile (operational staff UI).
    """
    if not user.is_active or not user.is_staff:
        return False
    if user.role == User.Role.SUPER_ADMIN and user.is_superuser:
        return True
    if user.role == User.Role.STAFF:
        return EmployeeProfile.objects.filter(
            user=user, status=EmployeeProfile.Status.ACTIVE
        ).exists()
    return False


def user_has_family_portal_access(user: User) -> bool:
    """
    Family portal: must actively lead a group or be an active parent/spouse/manager member.

    User.role == PARENT alone is not enough (avoids portal access without a manageable family row).
    """
    if FamilyGroup.objects.filter(
        leader=user, status=FamilyGroup.Status.ACTIVE
    ).exists():
        return True
    return FamilyMember.objects.filter(
        user=user,
        status=FamilyMember.Status.ACTIVE,
        role__in=[
            FamilyMember.Role.PARENT,
            FamilyMember.Role.SPOUSE,
            FamilyMember.Role.MANAGER,
        ],
    ).exists()


def primary_spa_redirect(user: User) -> str:
    """
    Canonical SPA entry path for this user.

    Admin and vendor win first. For shoppers, active portal follows User.role
    (switch-portal) so Normal mode stays on /portal even with family membership.
    """
    if user_allowed_for_portal_key(user, PORTAL_ADMIN):
        return "/admin"
    if user_allowed_for_portal_key(user, PORTAL_VENDOR):
        return "/vendor"
    if user.role == User.Role.PARENT:
        if user_has_family_portal_access(user):
            return "/family-portal"
        return "/portal"
    if user.role == User.Role.CHILD:
        if user_allowed_for_portal_key(user, PORTAL_CHILD):
            return "/child-portal"
        return "/portal"
    if user.role == User.Role.NORMAL:
        return "/portal"
    if user_has_family_portal_access(user):
        return "/family-portal"
    if user_allowed_for_portal_key(user, PORTAL_CHILD):
        return "/child-portal"
    return "/portal"


def user_allowed_for_portal_key(user: User, portal_key: str) -> bool:
    if portal_key == PORTAL_ADMIN:
        return user_allowed_for_admin_portal(user)
    if portal_key == PORTAL_VENDOR:
        return user_allowed_for_vendor_portal(user)
    if portal_key == PORTAL_MAIN:
        return user.role == User.Role.NORMAL
    if portal_key == PORTAL_FAMILY:
        if user.role == User.Role.CHILD:
            return False
        return user_has_family_portal_access(user)
    if portal_key == PORTAL_CHILD:
        if user.role != User.Role.CHILD:
            return False
        if getattr(settings, "CHILD_PORTAL_REQUIRE_MEMBERSHIP", False):
            return FamilyMember.objects.filter(
                user=user,
                role=FamilyMember.Role.CHILD,
                status=FamilyMember.Status.ACTIVE,
            ).exists()
        return True
    return False


def portal_role_error_response(portal_key: str, status: int = 403) -> Response:
    """Safe message when credentials are valid but role is wrong for this surface."""
    hint = _SUGGEST.get(portal_key, "")
    detail = (
        "This account does not have access to this portal. "
        + (hint + " " if hint else "")
    ).strip()
    return Response({"detail": detail, "portal": portal_key, "code": "wrong_portal_role"}, status=status)


def normalize_portal_key(raw: str | None) -> str | None:
    if not raw:
        return None
    k = str(raw).strip().lower().replace("_", "-")
    aliases = {
        "main": PORTAL_MAIN,
        "customer": PORTAL_MAIN,
        "family": PORTAL_FAMILY,
        "familyportal": PORTAL_FAMILY,
        "child": PORTAL_CHILD,
        "childportal": PORTAL_CHILD,
    }
    k = aliases.get(k, k)
    return k if k in VALID_PORTAL_KEYS else None


def infer_portal_key_from_frontend_path(path: str) -> str:
    """Map SPA path (next= query) to portal key for OAuth / redirects."""
    p = (path or "").strip() or "/portal"
    low = p.lower()
    if low.startswith("/admin"):
        return PORTAL_ADMIN
    if low.startswith("/vendor"):
        return PORTAL_VENDOR
    if low.startswith("/family-portal"):
        return PORTAL_FAMILY
    if low.startswith("/child-portal"):
        return PORTAL_CHILD
    return PORTAL_MAIN


def assert_portal_login_allowed(user: User | None, portal_key: str) -> Response | None:
    """
    After credentials are verified, return an error Response if this surface is not allowed.
    """
    if user is None:
        return None
    if not user.is_active:
        return Response({"detail": "This account is disabled.", "code": "inactive"}, status=400)
    if user_allowed_for_portal_key(user, portal_key):
        return None
    return portal_role_error_response(portal_key)
