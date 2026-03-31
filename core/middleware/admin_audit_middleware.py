"""Log successful mutating requests to /api/admin/ for a complete activity trail."""

from django.utils.deprecation import MiddlewareMixin

from core.models import AuditLog
from core.portal_roles import user_allowed_for_admin_portal
from core.services import audit_service
from core.views.admin.admin_access import admin_module_key_from_path
from core.views.admin.admin_write_utils import client_ip_from_request

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AdminApiAuditMiddleware(MiddlewareMixin):
    """Create AuditLog rows for authenticated admin API writes (2xx only)."""

    def process_response(self, request, response):
        if request.method not in _MUTATING:
            return response
        path = request.path
        if not path.startswith("/api/admin/"):
            return response
        rest = path[len("/api/admin/") :]
        if rest.startswith("auth/"):
            return response
        status = getattr(response, "status_code", 0)
        if not (200 <= status < 400):
            return response

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return response
        if not user_allowed_for_admin_portal(user):
            return response

        mk = admin_module_key_from_path(path) or "admin_api"
        module = (mk or "admin_api")[:64]
        if request.method == "POST":
            action_kind = AuditLog.ActionKind.CREATE
        elif request.method == "DELETE":
            action_kind = AuditLog.ActionKind.DELETE
        else:
            action_kind = AuditLog.ActionKind.UPDATE

        action = f"{request.method} {path}"[:500]
        audit_service.log(
            action,
            log_type=AuditLog.Type.SETTINGS,
            performed_by=user,
            object_type="AdminAPI",
            object_id="",
            ip_address=client_ip_from_request(request),
            action_kind=action_kind,
            module=module,
            metadata={"path": path[:300], "method": request.method, "status": status},
        )
        return response
