from __future__ import annotations

from core.models import AuditLog, FlaggedActivity
from core.services import audit_service


def record_resolution(flag: FlaggedActivity) -> None:
    if flag.status not in (
        FlaggedActivity.Status.REVIEWED,
        FlaggedActivity.Status.RESOLVED,
    ):
        return
    audit_service.log(
        f"Flagged activity {flag.pk} → {flag.status}",
        log_type=AuditLog.Type.SECURITY,
        performed_by=flag.reviewed_by,
        object_type="FlaggedActivity",
        object_id=str(flag.pk),
        ip_address=str(flag.ip_address) if flag.ip_address else None,
        action_kind=AuditLog.ActionKind.UPDATE,
        module="security",
        metadata={"flag_id": str(flag.pk), "status": str(flag.status)},
    )
