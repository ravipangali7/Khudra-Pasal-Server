from __future__ import annotations

from django.db import transaction

from core.models import KYCDocument, User


# Minimum doc type required before user can be marked verified.
REQUIRED_KYC_TYPES = frozenset({KYCDocument.DocumentType.CITIZENSHIP})


@transaction.atomic
def sync_user_kyc_status(user: User) -> None:
    """
    Derive User.kyc_status from KYCDocument rows.
    - Any rejected doc -> rejected
    - Else if every required type has at least one approved doc, and no pending/review -> verified
    - Else if any pending/review -> review
    - Else -> pending
    """
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
        required_ok = REQUIRED_KYC_TYPES.issubset(types_with_approval)
        if required_ok and not pending_like:
            new_status = User.KYCStatus.VERIFIED
        elif pending_like or not required_ok:
            new_status = User.KYCStatus.REVIEW
        else:
            new_status = User.KYCStatus.PENDING

    User.objects.filter(pk=user.pk).update(kyc_status=new_status)
