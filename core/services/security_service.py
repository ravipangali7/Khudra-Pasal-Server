from __future__ import annotations

from typing import Any

from core.models import AuditLog, FlaggedActivity
from core.services import audit_service


def create_flagged_activity(
    *,
    activity_type: str,
    detail: str = "",
    severity: str = FlaggedActivity.Severity.MEDIUM,
    status: str = FlaggedActivity.Status.OPEN,
    user=None,
    ip_address: str | None = None,
    reviewed_by=None,
) -> FlaggedActivity:
    """Create a flagged activity row with safe defaults."""
    allowed_severity = {choice[0] for choice in FlaggedActivity.Severity.choices}
    allowed_status = {choice[0] for choice in FlaggedActivity.Status.choices}
    return FlaggedActivity.objects.create(
        user=user,
        activity_type=(activity_type or "Security event")[:150],
        detail=(detail or "")[:2000],
        severity=severity if severity in allowed_severity else FlaggedActivity.Severity.MEDIUM,
        status=status if status in allowed_status else FlaggedActivity.Status.OPEN,
        ip_address=ip_address,
        reviewed_by=reviewed_by,
    )


def flag_and_log_security_event(
    *,
    activity_type: str,
    detail: str = "",
    severity: str = FlaggedActivity.Severity.MEDIUM,
    user=None,
    ip_address: str | None = None,
    performed_by=None,
    object_type: str = "FlaggedActivity",
    object_id: str = "",
    action_kind: str = AuditLog.ActionKind.OTHER,
    module: str = "security",
    metadata: dict[str, Any] | None = None,
) -> FlaggedActivity:
    """
    Persist a security alert (FlaggedActivity) and companion SECURITY audit log.
    """
    flag = create_flagged_activity(
        activity_type=activity_type,
        detail=detail,
        severity=severity,
        user=user,
        ip_address=ip_address,
    )
    meta = dict(metadata) if metadata else {}
    meta.setdefault("flag_id", str(flag.pk))
    audit_service.log(
        activity_type,
        log_type=AuditLog.Type.SECURITY,
        performed_by=performed_by,
        object_type=object_type or "FlaggedActivity",
        object_id=object_id or str(flag.pk),
        ip_address=ip_address,
        action_kind=action_kind,
        module=module or "security",
        metadata=meta,
    )
    return flag


def validate_flag_resolution_note(severity: str, note: str) -> str | None:
    """Return an error message when a resolution note is required but missing/short."""
    trimmed = (note or "").strip()
    if severity == FlaggedActivity.Severity.HIGH and len(trimmed) < 10:
        return "resolution_note required (min 10 characters) for high severity."
    if severity == FlaggedActivity.Severity.MEDIUM and len(trimmed) < 5:
        return "resolution_note required (min 5 characters) for medium severity."
    return None


def append_resolution_note(flag: FlaggedActivity, note: str) -> None:
    """Append an admin resolution note to the flag detail field (max 2000 chars)."""
    trimmed = (note or "").strip()
    if not trimmed:
        return
    from django.utils import timezone

    stamp = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    block = f"[Resolution {stamp}]\n{trimmed}"
    combined = f"{flag.detail}\n\n{block}".strip() if flag.detail else block
    flag.detail = combined[:2000]


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
