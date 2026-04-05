"""Dynamic sidebar trees from NavigationItem + live badge counts."""

from collections import defaultdict

from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import (
    NavigationItem,
    Notification,
    Order,
    ProductApproval,
    Refund,
    User,
    Vendor,
    WalletWithdrawal,
)
from core.portal_roles import (
    user_allowed_for_admin_portal,
    user_allowed_for_vendor_portal,
    user_has_family_portal_access,
)
from core.views.admin.admin_access import admin_allowed_nav_keys, user_can_access_audit_logs


def _admin_nav_badges():
    pr = Refund.objects.filter(status=Refund.Status.PENDING).count()
    pw = WalletWithdrawal.objects.filter(status=WalletWithdrawal.Status.PENDING).count()
    return {
        "admin_pending_orders": Order.objects.filter(status=Order.Status.PENDING).count(),
        "admin_pending_refunds": pr,
        "admin_pending_withdrawals": pw,
        "admin_finance_attention": pr + pw,
    }


def _role_visible(row, user: User) -> bool:
    rf = (row.roles_filter or "").strip()
    if not rf:
        return True
    allowed = {x.strip() for x in rf.split(",") if x.strip()}
    return user.role in allowed


def _filter_rows_for_portal_user(rows: list, user: User) -> list:
    """Drop items that fail roles_filter; drop descendants if ancestor is hidden."""
    by_key = {r.key: r for r in rows}

    def chain_ok(key: str) -> bool:
        r = by_key.get(key)
        if not r or not _role_visible(r, user):
            return False
        pk = (r.parent_key or "").strip()
        if not pk:
            return True
        return chain_ok(pk)

    return [r for r in rows if chain_ok(r.key)]


def _build_tree_from_rows(rows: list, badge_map: dict) -> list:
    by_parent: dict[str, list] = defaultdict(list)
    for r in rows:
        by_parent[r.parent_key or ""].append(r)

    def build(parent: str) -> list:
        out = []
        for r in sorted(by_parent[parent], key=lambda x: (x.sort_order, x.key)):
            # `viewKey` maps to the frontend view registry; `id` is the URL segment (nav key).
            vk = (getattr(r, "view_key", None) or "").strip()
            node = {"id": r.key, "viewKey": vk or r.key, "label": r.label, "icon": r.icon}
            if r.badge_key:
                v = badge_map.get(r.badge_key)
                if v is not None and v > 0:
                    node["badge"] = int(v)
            children = build(r.key)
            if children:
                node["children"] = children
            out.append(node)
        return out

    return build("")


def _admin_rows_filtered(rows: list, user: User | None) -> list:
    if user is None:
        return rows
    allowed = admin_allowed_nav_keys(user)
    if allowed is None:
        result = list(rows)
    else:
        by_key = {r.key: r for r in rows}
        expanded = set(allowed)
        for k in list(allowed):
            cur = k
            while cur:
                r = by_key.get(cur)
                if not r:
                    break
                pk = (r.parent_key or "").strip()
                if pk:
                    expanded.add(pk)
                cur = pk or None
        if "orders" in allowed:
            expanded.add("purchase")
            expanded.add("purchase-insights")
        result = [r for r in rows if r.key in expanded]
    return [
        r
        for r in result
        if r.key != "audit-logs" or user_can_access_audit_logs(user)
    ]


def _build_tree(surface: str, badge_map: dict, user: User | None = None) -> list:
    rows = list(
        NavigationItem.objects.filter(surface=surface).order_by("sort_order", "key")
    )
    if user is not None and surface in (
        NavigationItem.Surface.PORTAL_MAIN,
        NavigationItem.Surface.PORTAL_FAMILY,
        NavigationItem.Surface.PORTAL_CHILD,
    ):
        rows = _filter_rows_for_portal_user(rows, user)
    elif user is not None and surface == NavigationItem.Surface.ADMIN:
        rows = _admin_rows_filtered(rows, user)
    return _build_tree_from_rows(rows, badge_map)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_navigation(request):
    if not user_allowed_for_admin_portal(request.user):
        return Response({"detail": "Admin access required."}, status=403)
    badges = _admin_nav_badges()
    return Response(
        {"items": _build_tree(NavigationItem.Surface.ADMIN, badges, user=request.user)}
    )


def _vendor_nav_badges(vendor: Vendor):
    pending_orders = Order.objects.filter(
        seller=vendor, status=Order.Status.PENDING
    ).count()
    pending_products = ProductApproval.objects.filter(
        vendor=vendor, status=ProductApproval.Status.PENDING
    ).count()
    return {
        "vendor_pending_orders": pending_orders,
        "vendor_pending_products": pending_products,
    }


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_navigation(request):
    u = request.user
    if not user_allowed_for_vendor_portal(u):
        return Response({"detail": "Vendor portal access required."}, status=403)
    vendor = getattr(u, "vendor_profile", None)
    if not vendor:
        return Response({"items": _build_tree(NavigationItem.Surface.VENDOR, {}, user=None)})
    badges = _vendor_nav_badges(vendor)
    return Response({"items": _build_tree(NavigationItem.Surface.VENDOR, badges, user=None)})


def _portal_nav_badges(user: User):
    return {
        "portal_notifications": Notification.objects.filter(
            recipient=user, is_read=False
        ).count(),
    }


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def portal_navigation(request):
    surface = (request.query_params.get("surface") or "main").strip().lower()
    u = request.user
    if surface == "family":
        if not user_has_family_portal_access(u):
            return Response(
                {"detail": "Family portal requires a parent or family admin account."},
                status=403,
            )
        nav_surface = NavigationItem.Surface.PORTAL_FAMILY
    elif surface == "child":
        if u.role != User.Role.CHILD:
            return Response({"detail": "Child portal requires a child account."}, status=403)
        nav_surface = NavigationItem.Surface.PORTAL_CHILD
    else:
        if u.role != User.Role.NORMAL:
            return Response(
                {"detail": "Customer portal requires a normal (customer) account."},
                status=403,
            )
        nav_surface = NavigationItem.Surface.PORTAL_MAIN

    badges = _portal_nav_badges(u)
    return Response({"items": _build_tree(nav_surface, badges, user=u)})
