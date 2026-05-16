"""KYC gating for wallet withdrawals and mandatory parent verification (portal)."""

from __future__ import annotations

from core.models import KYCDocument, User
from core.services.site_settings_policy import user_kyc_required


def latest_kyc_rejection_reason(user: User) -> str:
    doc = (
        KYCDocument.objects.filter(user=user, status=KYCDocument.Status.REJECTED)
        .order_by("-reviewed_at", "-submitted_at", "-id")
        .first()
    )
    return (doc.rejection_reason or "").strip() if doc else ""


def _kyc_not_verified_block_payload(user: User, *, action: str) -> dict | None:
    if user.kyc_status == User.KYCStatus.VERIFIED:
        return None

    reason = latest_kyc_rejection_reason(user)
    if user.kyc_status == User.KYCStatus.REJECTED:
        return {
            "code": "kyc_rejected",
            "detail": "KYC was rejected. Update your submission to continue.",
            "rejection_reason": reason,
        }
    if user.kyc_status == User.KYCStatus.REVIEW:
        return {
            "code": "kyc_pending",
            "detail": "KYC is under review.",
            "rejection_reason": "",
        }
    return {
        "code": "kyc_required",
        "detail": f"Complete KYC verification before {action}.",
        "rejection_reason": "",
    }


def kyc_withdraw_block_payload(user: User) -> dict | None:
    """
    If withdraw must be blocked, return error dict for JSON Response.
    None means withdraw may proceed (subject to wallet rules).

    Withdrawals always require portal user KYC verified status (no vendor or site bypass).
    """
    return _kyc_not_verified_block_payload(user, action="withdrawing")


def kyc_mandatory_block_payload(user: User) -> dict | None:
    """
    Block portal actions when KYC is required for this user (parents always; others per site).
    """
    if not user_kyc_required(user):
        return None
    return _kyc_not_verified_block_payload(user, action="continuing")


def becoming_parent_kyc_block_payload(user: User) -> dict | None:
    """Creating a family group requires verified KYC regardless of site settings."""
    return _kyc_not_verified_block_payload(user, action="creating a family group")
