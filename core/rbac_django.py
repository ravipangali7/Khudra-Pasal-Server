"""
Django auth Group / Permission RBAC for KhudraPasal portals.

Roles = django.contrib.auth.models.Group
Permissions = django.contrib.auth.models.Permission (custom codenames on SecuritySettings)

Codename pattern: {action}_{surface}_{nav_key}  e.g. view_admin_dashboard, change_vendor_products
"""

from __future__ import annotations

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

from core.models import SecuritySettings, User
from core.nav_seed import (
    ADMIN_NAV,
    PORTAL_CHILD_NAV,
    PORTAL_FAMILY_NAV,
    PORTAL_MAIN_NAV,
    VENDOR_NAV,
)

RBAC_CONTENT_TYPE_APP = "core"
RBAC_MODEL = "securitysettings"

SURFACE_ADMIN = "admin"
SURFACE_VENDOR = "vendor"
SURFACE_PORTAL_MAIN = "portal_main"
SURFACE_PORTAL_FAMILY = "portal_family"
SURFACE_PORTAL_CHILD = "portal_child"

ACTION_VIEW = "view"
ACTION_CHANGE = "change"

# Admin nav keys used for coarse API module checks (aligned with admin_access.py).
ADMIN_MODULE_KEYS = frozenset(
    {
        "dashboard",
        "pos",
        "catalog",
        "inventory",
        "products",
        "po-billing",
        "orders",
        "marketing",
        "cms",
        "finance",
        "users",
        "employees",
        "delivery",
        "families",
        "wallet-master",
        "support-tickets",
        "security",
        "reports",
        "reels-admin",
        "shipping",
        "settings",
        "sellers",
        "roles-permissions",
        "audit-logs",
    }
)

EXTRA_ADMIN_NAV_KEYS = frozenset({"roles-permissions"})


def _nav_rows_for_surface(surface: str) -> list[tuple]:
    if surface == SURFACE_ADMIN:
        return list(ADMIN_NAV)
    if surface == SURFACE_VENDOR:
        return list(VENDOR_NAV)
    if surface == SURFACE_PORTAL_MAIN:
        return list(PORTAL_MAIN_NAV)
    if surface == SURFACE_PORTAL_FAMILY:
        return list(PORTAL_FAMILY_NAV)
    if surface == SURFACE_PORTAL_CHILD:
        return list(PORTAL_CHILD_NAV)
    return []


def nav_keys_for_surface(surface: str) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for row in _nav_rows_for_surface(surface):
        key = row[1]
        if key not in seen:
            seen.add(key)
            keys.append(key)
    if surface == SURFACE_ADMIN:
        for k in sorted(EXTRA_ADMIN_NAV_KEYS):
            if k not in seen:
                keys.append(k)
    return keys


def nav_key_to_slug(nav_key: str) -> str:
    return nav_key.replace("-", "_")


def permission_codename(action: str, surface: str, nav_key: str) -> str:
    return f"{action}_{surface}_{nav_key_to_slug(nav_key)}"


def permission_full_name(codename: str) -> str:
    return f"{RBAC_CONTENT_TYPE_APP}.{codename}"


def _rbac_content_type() -> ContentType:
    return ContentType.objects.get_for_model(SecuritySettings)


def seed_rbac_permissions() -> tuple[int, int]:
    """Create or update custom Permission rows for all nav modules. Returns (created, updated)."""
    ct = _rbac_content_type()
    created = 0
    updated = 0
    surfaces = (
        SURFACE_ADMIN,
        SURFACE_VENDOR,
        SURFACE_PORTAL_MAIN,
        SURFACE_PORTAL_FAMILY,
        SURFACE_PORTAL_CHILD,
    )
    for surface in surfaces:
        for nav_key in nav_keys_for_surface(surface):
            for action, label in (
                (ACTION_VIEW, "View"),
                (ACTION_CHANGE, "Change"),
            ):
                codename = permission_codename(action, surface, nav_key)
                name = f"{label} {surface} / {nav_key}"
                perm, was_created = Permission.objects.get_or_create(
                    codename=codename,
                    content_type=ct,
                    defaults={"name": name},
                )
                if was_created:
                    created += 1
                elif perm.name != name:
                    perm.name = name
                    perm.save(update_fields=["name"])
                    updated += 1
    return created, updated


def rbac_permissions_queryset():
    ct = _rbac_content_type()
    return Permission.objects.filter(content_type=ct).order_by("codename")


def parse_permission_codename(codename: str) -> tuple[str, str, str] | None:
    """Return (action, surface, nav_key) or None if not an RBAC permission."""
    for surface in (
        SURFACE_PORTAL_FAMILY,
        SURFACE_PORTAL_CHILD,
        SURFACE_PORTAL_MAIN,
        SURFACE_ADMIN,
        SURFACE_VENDOR,
    ):
        for action in (ACTION_CHANGE, ACTION_VIEW):
            prefix = f"{action}_{surface}_"
            if codename.startswith(prefix):
                slug = codename[len(prefix) :]
                nav_key = slug.replace("_", "-")
                return action, surface, nav_key
    return None


def user_effective_permission_codenames(user: User) -> set[str]:
    if not user or not user.is_authenticated:
        return set()
    if getattr(user, "is_superuser", False):
        return {p.codename for p in rbac_permissions_queryset()}
    perms = set(user.user_permissions.values_list("codename", flat=True))
    for g in user.groups.prefetch_related("permissions").all():
        perms.update(g.permissions.values_list("codename", flat=True))
    return {c for c in perms if parse_permission_codename(c)}


def user_permission_strings(user: User) -> list[str] | None:
    """Full permission strings for API, or None if superuser (all allowed)."""
    if not user or not user.is_authenticated:
        return []
    if getattr(user, "is_superuser", False):
        return None
    codenames = user_effective_permission_codenames(user)
    return sorted(permission_full_name(c) for c in codenames)


def user_groups_payload(user: User) -> list[dict]:
    return [{"id": g.pk, "name": g.name} for g in user.groups.all().order_by("name")]


def user_uses_django_groups(user: User) -> bool:
    return bool(user and user.is_authenticated and user.groups.exists())


def allowed_nav_keys_for_surface(user: User, surface: str) -> frozenset[str] | None:
    """
    Nav keys allowed via Django groups for a surface.
    None = no group-based restriction (use legacy rules).
  Empty frozenset = groups assigned but no view permissions.
    """
    if not user or not user.is_authenticated:
        return frozenset()
    if getattr(user, "is_superuser", False):
        return None
    if not user_uses_django_groups(user):
        return None
    keys: set[str] = set()
    for codename in user_effective_permission_codenames(user):
        parsed = parse_permission_codename(codename)
        if not parsed:
            continue
        action, surf, nav_key = parsed
        if surf != surface or action != ACTION_VIEW:
            continue
        keys.add(nav_key)
    if surface == SURFACE_ADMIN and keys:
        keys.add("dashboard")
    return frozenset(keys)


def user_can_view_nav(user: User, surface: str, nav_key: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    allowed = allowed_nav_keys_for_surface(user, surface)
    if allowed is not None:
        return nav_key in allowed
    return True


def user_can_change_nav(user: User, surface: str, nav_key: str) -> bool:
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    codename = permission_codename(ACTION_CHANGE, surface, nav_key)
    return user.has_perm(permission_full_name(codename))


def merge_allowed_nav_keys(
    django_keys: frozenset[str] | None,
    legacy_keys: frozenset[str] | None,
) -> frozenset[str] | None:
    """Intersect restrictions when both apply; None means unrestricted for that source."""
    if django_keys is None and legacy_keys is None:
        return None
    if django_keys is None:
        return legacy_keys
    if legacy_keys is None:
        return django_keys
    merged = set(django_keys) & set(legacy_keys)
    if merged:
        merged.add("dashboard")
    return frozenset(merged)


def assign_user_groups(user: User, group_ids: list | None) -> None:
    if group_ids is None:
        return
    ids = []
    for raw in group_ids:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    groups = Group.objects.filter(pk__in=ids)
    user.groups.set(groups)


def serialize_group(group: Group, *, include_permissions: bool = False) -> dict:
    data = {
        "id": group.pk,
        "name": group.name,
        "user_count": group.user_set.count(),
    }
    if include_permissions:
        data["permission_ids"] = list(
            group.permissions.filter(content_type=_rbac_content_type()).values_list("pk", flat=True)
        )
    else:
        data["permission_count"] = group.permissions.filter(
            content_type=_rbac_content_type()
        ).count()
    return data


def serialize_permission(perm: Permission) -> dict:
    parsed = parse_permission_codename(perm.codename)
    surface = ""
    nav_key = ""
    action = ""
    if parsed:
        action, surface, nav_key = parsed
    return {
        "id": perm.pk,
        "codename": perm.codename,
        "name": perm.name,
        "full_name": permission_full_name(perm.codename),
        "action": action,
        "surface": surface,
        "nav_key": nav_key,
    }
