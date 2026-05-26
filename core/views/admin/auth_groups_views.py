"""Django auth Group (roles) and Permission management for admin SPA."""

from django.contrib.auth.models import Group, Permission
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core import rbac_django as rbac
from core.models import User
from core.services import audit_service
from core.models import AuditLog
from core.views.admin.admin_access import enforce_admin_api_access
from core.views.admin.admin_write_utils import client_ip_from_request, validation_error
from core.views.admin.resource_views import AdminPagination, _paginate


def _forbidden_manage(request):
    err = enforce_admin_api_access(request)
    if err:
        return err
    u = request.user
    if getattr(u, "is_superuser", False):
        return None
    if rbac.user_can_change_nav(u, rbac.SURFACE_ADMIN, "roles-permissions"):
        return None
    return Response({"detail": "You do not have permission to manage roles."}, status=403)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_auth_permissions_list(request):
    if err := enforce_admin_api_access(request):
        return err
    rbac.seed_rbac_permissions()
    surface = (request.query_params.get("surface") or "").strip()
    qs = rbac.rbac_permissions_queryset()
    rows = [rbac.serialize_permission(p) for p in qs]
    if surface:
        rows = [r for r in rows if r.get("surface") == surface]
    return Response({"results": rows})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_auth_groups_list(request):
    if err := _forbidden_manage(request):
        return err
    qs = Group.objects.all().order_by("name")
    paginator, page = _paginate(request, qs)
    rows = [rbac.serialize_group(g) for g in page]
    return paginator.get_paginated_response(rows)


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_auth_group_create(request):
    if err := _forbidden_manage(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required")
    if Group.objects.filter(name=name).exists():
        return validation_error("A group with this name already exists.")
    group = Group.objects.create(name=name)
    permission_ids = request.data.get("permission_ids")
    if isinstance(permission_ids, list):
        perms = rbac.rbac_permissions_queryset().filter(pk__in=permission_ids)
        group.permissions.set(perms)
    audit_service.log(
        f"Created role (group) {name!r} (id={group.pk})",
        log_type=AuditLog.Type.SETTINGS,
        performed_by=request.user,
        object_type="Group",
        object_id=str(group.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.CREATE,
        module="roles-permissions",
    )
    return Response(rbac.serialize_group(group, include_permissions=True), status=201)


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_auth_group_detail(request, pk):
    if err := _forbidden_manage(request):
        return err
    group = Group.objects.filter(pk=pk).first()
    if not group:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(rbac.serialize_group(group, include_permissions=True))
    if request.method == "DELETE":
        if group.user_set.exists():
            return validation_error("Cannot delete a role that is assigned to users.", status=400)
        gid, gname = group.pk, group.name
        group.delete()
        audit_service.log(
            f"Deleted role (group) {gname!r} (id={gid})",
            log_type=AuditLog.Type.SETTINGS,
            performed_by=request.user,
            object_type="Group",
            object_id=str(gid),
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="roles-permissions",
        )
        return Response({"ok": True})
    if "name" in request.data:
        name = (request.data.get("name") or "").strip()
        if name:
            if Group.objects.filter(name=name).exclude(pk=group.pk).exists():
                return validation_error("A group with this name already exists.")
            group.name = name
            group.save(update_fields=["name"])
    if "permission_ids" in request.data and isinstance(request.data.get("permission_ids"), list):
        perms = rbac.rbac_permissions_queryset().filter(
            pk__in=request.data.get("permission_ids")
        )
        group.permissions.set(perms)
    audit_service.log(
        f"Updated role (group) {group.name!r} (id={group.pk})",
        log_type=AuditLog.Type.SETTINGS,
        performed_by=request.user,
        object_type="Group",
        object_id=str(group.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="roles-permissions",
    )
    return Response(rbac.serialize_group(group, include_permissions=True))
