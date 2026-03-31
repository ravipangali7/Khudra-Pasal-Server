"""Portal KYC upload validation and document supersede helpers."""

from __future__ import annotations

import os

from django.conf import settings

from core.models import KYCDocument, User
from core.views.admin.admin_write_utils import validation_error


def kyc_allowed_extensions() -> tuple[str, ...]:
    return getattr(
        settings,
        "CUSTOMER_DOCUMENT_ALLOWED_EXTENSIONS",
        ("pdf", "png", "jpg", "jpeg", "webp"),
    )


def kyc_upload_max_bytes() -> int:
    return int(getattr(settings, "KYC_UPLOAD_MAX_BYTES", 8 * 1024 * 1024))


def validate_kyc_upload_file(uploaded, field_name: str):
    if not uploaded:
        return None
    ext = os.path.splitext(getattr(uploaded, "name", "") or "")[1].lower().lstrip(".")
    allowed = kyc_allowed_extensions()
    if ext not in allowed:
        return validation_error(
            f"File must be one of: {', '.join(sorted(allowed))}",
            field=field_name,
        )
    size = getattr(uploaded, "size", 0) or 0
    max_b = kyc_upload_max_bytes()
    if size <= 0:
        return validation_error("Empty file.", field=field_name)
    if size > max_b:
        return validation_error(f"File too large (max {max_b // (1024 * 1024)} MB).", field=field_name)
    return None


def supersede_non_approved_kyc(user: User, document_type: str) -> None:
    """Remove pending/rejected/review rows for this type so resubmission is possible."""
    KYCDocument.objects.filter(user=user, document_type=document_type).exclude(
        status=KYCDocument.Status.APPROVED
    ).delete()
