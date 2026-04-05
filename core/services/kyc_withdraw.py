"""KYC gating for wallet withdrawals (portal)."""

from __future__ import annotations

from core.models import KYCDocument, SiteSettings, User, Vendor


def latest_kyc_rejection_reason(user: User) -> str:
    doc = (
        KYCDocument.objects.filter(user=user, status=KYCDocument.Status.REJECTED)
        .order_by("-reviewed_at", "-submitted_at", "-id")
        .first()
    )
    return (doc.rejection_reason or "").strip() if doc else ""


def kyc_withdraw_block_payload(
    user: User, *, vendor: Vendor | None = None
) -> dict | None:
    """
    If withdraw must be blocked, return error dict for JSON Response.
    None means withdraw may proceed (subject to wallet rules).

    When ``vendor`` is passed and the vendor is admin-approved, portal KYC is not
    required: vendor onboarding already covers identity checks for that wallet.
    """
    if not SiteSettings.load().kyc_required:
        return None
    if vendor is not None and vendor.status == Vendor.Status.APPROVED:
        return None
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
        "detail": "Complete KYC verification before withdrawing.",
        "rejection_reason": "",
    }
