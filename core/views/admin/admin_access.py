"""Admin navigation filtering and module access for employees vs superusers."""

from rest_framework.response import Response

from core.models import EmployeeProfile, User
from core.portal_roles import user_allowed_for_admin_portal


def admin_allowed_nav_keys(user: User) -> frozenset[str] | None:
    """
    Return allowed admin nav keys, or None if all keys allowed.
    Superuser: full access. Staff without EmployeeProfile: full access.
    EmployeeProfile: union of modules_access list and Role.permissions keys that are truthy.
    Empty union after load: full access (backward compatible).
    """
    if not user or not user.is_authenticated:
        return frozenset()
    if getattr(user, "is_superuser", False):
        return None
    ep = (
        EmployeeProfile.objects.filter(user=user)
        .select_related("role")
        .first()
    )
    if not ep:
        return None
    keys: set[str] = set()
    if isinstance(ep.modules_access, list):
        keys.update(str(x) for x in ep.modules_access if x)
    role = ep.role
    if role and isinstance(role.permissions, dict):
        keys.update(k for k, v in role.permissions.items() if v)
    if not keys:
        return None
    keys.add("dashboard")
    return frozenset(keys)


def user_can_access_admin_module(user: User, module_key: str) -> bool:
    allowed = admin_allowed_nav_keys(user)
    if allowed is None:
        return True
    return module_key in allowed


def is_admin_request_user(user) -> bool:
    return bool(
        user
        and user.is_authenticated
        and user_allowed_for_admin_portal(user)
    )


def user_can_access_audit_logs(user: User) -> bool:
    """Superuser (Django) or app role super_admin may read audit APIs and nav item."""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return getattr(user, "role", None) == User.Role.SUPER_ADMIN


def user_can_access_super_admin_database_cleanup(user: User) -> bool:
    """Same privilege gate as audit logs: Django superuser or app super_admin role."""
    return user_can_access_audit_logs(user)


def enforce_audit_log_access(request):
    """
    Admin portal + super-admin only. Use for audit log list/detail instead of enforce_admin_api_access.
    """
    if not is_admin_request_user(request.user):
        return Response({"detail": "Admin access required."}, status=403)
    if not user_can_access_audit_logs(request.user):
        return Response(
            {"detail": "Audit log access requires super admin privileges."},
            status=403,
        )
    return None


def admin_module_key_from_path(path: str) -> str | None:
    """
    Map /api/admin/... to coarse nav permission keys (same family as EmployeeModule PERM_NAV_KEYS).
    None => no extra RBAC (staff/superuser only).
    """
    prefix = "/api/admin/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :]
    if rest.startswith("navigation/") or rest.startswith("auth/"):
        return None
    rules: list[tuple[str, str]] = [
        ("dashboard/", "dashboard"),
        ("users/staff/", "employees"),
        ("kyc-submissions/", "customers"),
        ("users/", "customers"),
        ("order-settings/", "orders"),
        ("orders/", "orders"),
        ("products/", "products"),
        ("vendors/", "sellers"),
        ("categories/", "products"),
        ("brands/", "products"),
        ("attributes/", "products"),
        ("attribute-values/", "products"),
        ("units/", "products"),
        ("reviews/", "products"),
        ("product-approvals/", "products"),
        ("coupons/", "marketing"),
        ("flash-deals/", "marketing"),
        ("banners/", "marketing"),
        ("cms-pages/", "marketing"),
        ("notifications/", "marketing"),
        ("refunds/", "settings"),
        ("payments/", "settings"),
        ("ledger-transactions/", "settings"),
        ("site-settings/", "settings"),
        ("payment-gateways/", "settings"),
        ("system/cleanup-modules/", "settings"),
        ("system/cleanup/", "settings"),
        ("system/db-stats/", "settings"),
        ("withdrawals/summary/", "settings"),
        ("withdrawals/", "settings"),
        ("payout-accounts/", "settings"),
        ("wallets/summary/", "wallet-master"),
        ("wallets/adjust/", "wallet-master"),
        ("wallets/", "wallet-master"),
        ("wallet-settings/", "wallet-master"),
        ("wallet-transactions/", "wallet-master"),
        ("wallet-bonuses/", "wallet-master"),
        ("loyalty-rules/", "wallet-master"),
        ("families/", "families"),
        ("family-members/", "families"),
        ("purchase-orders/", "settings"),
        ("delivery-men/", "delivery"),
        ("tickets/", "settings"),
        ("flagged/", "settings"),
        ("shipping-methods/", "settings"),
        ("shipping-zones/", "settings"),
        ("weight-rules/", "settings"),
        ("roles/", "employees"),
        ("employees/", "employees"),
        ("reels/", "reels-admin"),
    ]
    rules.sort(key=lambda x: len(x[0]), reverse=True)
    for pfx, key in rules:
        if rest.startswith(pfx):
            return key
    return None


def enforce_admin_api_access(request):
    """
    Staff/superuser check plus EmployeeProfile + Role module permissions when configured.
    Returns a DRF Response (403) if denied, else None.
    """
    if not is_admin_request_user(request.user):
        return Response({"detail": "Admin access required."}, status=403)
    mk = admin_module_key_from_path(request.path)
    if mk and not user_can_access_admin_module(request.user, mk):
        return Response(
            {"detail": "You do not have access to this admin module."},
            status=403,
        )
    return None
