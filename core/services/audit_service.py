from __future__ import annotations

from typing import Any

from core.models import AuditLog, User


def log(
    action: str,
    *,
    log_type: str,
    performed_by: User | None = None,
    object_type: str = "",
    object_id: str = "",
    ip_address: str | None = None,
    action_kind: str = AuditLog.ActionKind.OTHER,
    module: str = "",
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    meta: dict[str, Any] = dict(metadata) if metadata else {}
    return AuditLog.objects.create(
        action=action,
        type=log_type,
        performed_by=performed_by,
        object_type=object_type,
        object_id=object_id,
        ip_address=ip_address,
        action_kind=action_kind,
        module=(module or "")[:64],
        metadata=meta,
    )
