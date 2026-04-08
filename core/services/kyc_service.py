from __future__ import annotations

from django.db import transaction

from core.models import KYCDocument, User


# At least one approved doc of these types is enough for verified (gov ID).
PRIMARY_KYC_TYPES = frozenset(
    {
        KYCDocument.DocumentType.CITIZENSHIP,
        KYCDocument.DocumentType.PASSPORT,
    }
)


@transaction.atomic
def sync_user_kyc_status(user: User) -> None:
    """
    Derive User.kyc_status from KYCDocument rows.
    - Any rejected doc -> rejected
    - Else if at least one primary ID type is approved, and no pending/review -> verified
    - Else if any pending/review -> review
    - Else -> pending

    No-op when the user has no KYC rows (keeps manual admin / legacy verified without uploads).
    """
    if not KYCDocument.objects.filter(user=user).exists():
        return
    docs = list(
        KYCDocument.objects.filter(user=user).values_list("document_type", "status")
    )
    if not docs:
        new_status = User.KYCStatus.PENDING
    elif any(s == KYCDocument.Status.REJECTED for _, s in docs):
        new_status = User.KYCStatus.REJECTED
    else:
        types_with_approval = {t for t, s in docs if s == KYCDocument.Status.APPROVED}
        pending_like = any(
            s in (KYCDocument.Status.PENDING, KYCDocument.Status.REVIEW)
            for _, s in docs
        )
        identity_ok = bool(types_with_approval & PRIMARY_KYC_TYPES)
        if identity_ok and not pending_like:
            new_status = User.KYCStatus.VERIFIED
        elif pending_like or not identity_ok:
            new_status = User.KYCStatus.REVIEW
        else:
            new_status = User.KYCStatus.PENDING

    current = User.objects.filter(pk=user.pk).values_list("kyc_status", flat=True).first()
    if current == new_status:
        return
    u = User.objects.get(pk=user.pk)
    u.kyc_status = new_status
    u.save(update_fields=["kyc_status"])
