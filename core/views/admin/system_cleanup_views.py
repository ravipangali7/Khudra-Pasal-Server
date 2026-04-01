"""Super-admin database cleanup API (module list + transactional delete)."""

from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.services.db_cleanup import list_cleanup_modules_payload, run_cleanup, validate_module_ids
from core.views.admin.admin_access import (
    enforce_admin_api_access,
    user_can_access_super_admin_database_cleanup,
)


def _cleanup_access_denied(request):
    if err := enforce_admin_api_access(request):
        return err
    if not user_can_access_super_admin_database_cleanup(request.user):
        return Response(
            {"detail": "Database cleanup requires super admin privileges."},
            status=403,
        )
    return None


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_cleanup_modules_list(request):
    if err := _cleanup_access_denied(request):
        return err
    return Response({"modules": list_cleanup_modules_payload()})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_cleanup_execute(request):
    if err := _cleanup_access_denied(request):
        return err
    raw = request.data.get("module_ids")
    if raw is None:
        return Response({"detail": "module_ids is required."}, status=400)
    ids, verr = validate_module_ids(raw)
    if verr:
        return Response({"detail": verr}, status=400)
    try:
        results = run_cleanup(ids)
    except Exception as exc:
        return Response({"detail": str(exc)}, status=400)
    return Response({"ok": True, "results": results})
