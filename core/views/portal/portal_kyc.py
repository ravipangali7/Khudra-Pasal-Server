"""Portal KYC schema, status, and submission (customer / parent / child)."""

from __future__ import annotations

from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
)
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import KYCDocument, User
from core.services.kyc_portal import (
    kyc_allowed_extensions,
    kyc_upload_max_bytes,
    supersede_non_approved_kyc,
    validate_kyc_upload_file,
)
from core.services.kyc_service import sync_user_kyc_status
from core.services.kyc_withdraw import latest_kyc_rejection_reason
from core.services.site_settings_policy import user_kyc_required
from core.views.admin.admin_write_utils import absolute_media_url, validation_error
from core.views.portal.portal_views import IsPortalSelf


def _kyc_schema_payload():
    type_choices = [{"value": c[0], "label": c[1]} for c in KYCDocument.DocumentType.choices]
    return {
        "document_type_choices": type_choices,
        "fields": [
            {
                "name": "document_type",
                "type": "choice",
                "required": True,
                "choices_key": "document_type_choices",
            },
            {
                "name": "document_id_number",
                "type": "text",
                "required": False,
                "max_length": 100,
                "label": "ID / document number",
            },
            {
                "name": "document_image",
                "type": "image",
                "required": False,
                "label": "Front (image)",
            },
            {
                "name": "document_back",
                "type": "image",
                "required": False,
                "label": "Back (image, optional)",
            },
            {
                "name": "document_file",
                "type": "file",
                "required": False,
                "label": "Document PDF (optional if image provided)",
            },
        ],
        "allowed_extensions": list(kyc_allowed_extensions()),
        "max_upload_bytes": kyc_upload_max_bytes(),
    }


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_kyc_schema(request):
    return Response(_kyc_schema_payload())


def _kyc_status_payload(request, user: User) -> dict:
    docs = []
    for d in user.kyc_documents.all().order_by("-submitted_at")[:20]:
        docs.append(
            {
                "id": str(d.pk),
                "document_type": d.document_type,
                "status": d.status,
                "submitted_at": d.submitted_at.isoformat(),
                "rejection_reason": (d.rejection_reason or "").strip(),
            }
        )
    msg_key = "ok"
    if user.kyc_status == User.KYCStatus.VERIFIED:
        msg_key = "verified"
    elif user.kyc_status == User.KYCStatus.REJECTED:
        msg_key = "rejected"
    elif user.kyc_status == User.KYCStatus.REVIEW:
        msg_key = "pending_review"
    else:
        msg_key = "needs_submission"

    can_submit = user.kyc_status not in (
        User.KYCStatus.VERIFIED,
        User.KYCStatus.REVIEW,
    )

    return {
        "kyc_status": user.kyc_status,
        "kyc_required": user_kyc_required(user),
        "can_submit": can_submit,
        "message_key": msg_key,
        "rejection_reason": latest_kyc_rejection_reason(user),
        "documents": docs,
    }


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_kyc_status(request):
    sync_user_kyc_status(request.user)
    request.user.refresh_from_db()
    return Response(_kyc_status_payload(request, request.user))


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalSelf])
@parser_classes([MultiPartParser, FormParser])
def portal_kyc_submit(request):
    user = request.user
    doc_type = (request.data.get("document_type") or "").strip()
    if doc_type not in {c[0] for c in KYCDocument.DocumentType.choices}:
        return validation_error("document_type is required and must be valid", field="document_type")

    id_num = (request.data.get("document_id_number") or "").strip()[:100]
    img = request.FILES.get("document_image") or request.FILES.get("image")
    pdf = request.FILES.get("document_file") or request.FILES.get("file")
    back = request.FILES.get("document_back")

    for f, name in ((img, "document_image"), (back, "document_back"), (pdf, "document_file")):
        if f:
            err = validate_kyc_upload_file(f, name)
            if err:
                return err

    if not img and not pdf:
        return validation_error("Provide document_image or document_file", field="document_image")

    supersede_non_approved_kyc(user, doc_type)

    row = KYCDocument(
        user=user,
        document_type=doc_type,
        status=KYCDocument.Status.PENDING,
        document_id_number=id_num,
    )
    if img:
        row.document_image = img
    if pdf:
        row.document_file = pdf
    if back:
        row.document_back = back
    row.save()
    sync_user_kyc_status(user)
    user.refresh_from_db()
    return Response(
        {
            "id": str(row.pk),
            "kyc_status": user.kyc_status,
            "document": {
                "id": str(row.pk),
                "document_type": row.document_type,
                "status": row.status,
                "document_image": absolute_media_url(request, row.document_image)
                if row.document_image
                else "",
                "document_back": absolute_media_url(request, row.document_back)
                if row.document_back
                else "",
                "document_file": absolute_media_url(request, row.document_file) if row.document_file else "",
                "submitted_at": row.submitted_at.isoformat(),
            },
        },
        status=201,
    )
