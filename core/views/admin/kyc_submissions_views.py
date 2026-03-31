"""Admin: list and approve/reject portal KYC documents."""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import KYCDocument, User
from core.services.kyc_service import sync_user_kyc_status
from core.views.admin.admin_write_utils import absolute_media_url, validation_error
from core.views.admin.user_views import UserPagination, _forbidden_if_not_admin


def _serialize_kyc_doc(request, d: KYCDocument) -> dict:
    u = d.user
    rev = d.reviewer
    reviewer_out = None
    if rev:
        reviewer_out = {"id": rev.pk, "name": rev.name, "phone": rev.phone}
    return {
        "id": str(d.pk),
        "document_type": d.document_type,
        "document_id_number": (d.document_id_number or "").strip(),
        "status": d.status,
        "rejection_reason": (d.rejection_reason or "").strip(),
        "submitted_at": d.submitted_at.isoformat(),
        "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else "",
        "document_image": absolute_media_url(request, d.document_image) if d.document_image else "",
        "document_back": absolute_media_url(request, d.document_back) if d.document_back else "",
        "document_file": absolute_media_url(request, d.document_file) if d.document_file else "",
        "reviewer": reviewer_out,
        "user": {
            "id": u.pk,
            "name": u.name,
            "phone": u.phone,
            "email": (u.email or "").strip(),
            "username": u.username,
            "kid": u.KID,
            "kyc_status": u.kyc_status,
        },
    }


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_kyc_submissions_list(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    qs = KYCDocument.objects.select_related("user", "reviewer").order_by("-submitted_at", "-id")
    st = request.query_params.get("status")
    if st and st in dict(KYCDocument.Status.choices):
        qs = qs.filter(status=st)
    doc_type = request.query_params.get("document_type")
    if doc_type and doc_type in {c[0] for c in KYCDocument.DocumentType.choices}:
        qs = qs.filter(document_type=doc_type)
    search = (request.query_params.get("search") or "").strip()
    if search:
        qs = qs.filter(Q(user__name__icontains=search) | Q(user__phone__icontains=search))

    paginator = UserPagination()
    page = paginator.paginate_queryset(qs, request)
    rows = [_serialize_kyc_doc(request, d) for d in page]
    return paginator.get_paginated_response(rows)


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_kyc_submission_write(request, pk: int):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    doc = KYCDocument.objects.select_related("user", "reviewer").filter(pk=pk).first()
    if not doc:
        return Response({"detail": "Not found."}, status=404)

    new_status = (request.data.get("status") or "").strip().lower()
    if new_status not in (KYCDocument.Status.APPROVED, KYCDocument.Status.REJECTED):
        return validation_error("status must be approved or rejected", field="status")

    reason = (request.data.get("rejection_reason") or "").strip()
    if new_status == KYCDocument.Status.REJECTED and not reason:
        return validation_error("rejection_reason is required when rejecting", field="rejection_reason")

    doc.status = new_status
    doc.reviewer = request.user
    doc.reviewed_at = timezone.now()
    if new_status == KYCDocument.Status.REJECTED:
        doc.rejection_reason = reason[:2000]
    else:
        doc.rejection_reason = ""
    doc.save(update_fields=["status", "reviewer", "reviewed_at", "rejection_reason"])
    sync_user_kyc_status(doc.user)
    doc.user.refresh_from_db()
    out = _serialize_kyc_doc(request, doc)
    out["user"]["kyc_status"] = doc.user.kyc_status
    return Response(out)
