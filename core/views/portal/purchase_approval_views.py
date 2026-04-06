"""Portal APIs for child purchase approval requests."""

from __future__ import annotations

from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import PurchaseApprovalRequest, User
from core.services.purchase_approval_service import (
    approve_or_reject_request,
    create_child_purchase_request,
)
from core.views.portal.portal_views import IsPortalChild, IsPortalParent


def _par_serializer(par: PurchaseApprovalRequest) -> dict:
    p = par.product
    return {
        "id": par.pk,
        "product_id": par.product_id,
        "product_name": p.name,
        "product_slug": p.slug,
        "product_image_url": "",
        "amount": float(par.amount),
        "note": par.note or "",
        "status": par.status,
        "parent_note": par.parent_note or "",
        "created_at": par.created_at.isoformat() if par.created_at else None,
        "responded_at": par.responded_at.isoformat() if par.responded_at else None,
        "consumed_at": par.consumed_at.isoformat() if par.consumed_at else None,
        "child_id": par.child_id,
        "child_name": (par.child.name or par.child.phone or "").strip() or str(par.child_id),
    }


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_purchase_approval_requests(request):
    u = request.user
    if request.method == "GET":
        qs = (
            PurchaseApprovalRequest.objects.filter(child=u)
            .select_related("product", "child")
            .order_by("-created_at")[:100]
        )
        return Response({"results": [_par_serializer(x) for x in qs]})

    product_id = request.data.get("product_id")
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return Response({"detail": "product_id is required."}, status=400)
    note = str(request.data.get("note") or "")
    try:
        par = create_child_purchase_request(child=u, product_id=pid, note=note)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(_par_serializer(par), status=201)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_purchase_approval_requests(request):
    """Pending (and recent) purchase approval requests addressed to this parent (group leader)."""
    u = request.user
    status_filter = (request.query_params.get("status") or "pending").strip().lower()
    qs = PurchaseApprovalRequest.objects.filter(parent=u).select_related(
        "product", "child"
    )
    if status_filter == "pending":
        qs = qs.filter(status=PurchaseApprovalRequest.Status.PENDING)
    qs = qs.order_by("-created_at")[:100]
    return Response({"results": [_par_serializer(x) for x in qs]})


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_purchase_approval_request_detail(request, pk: int):
    u = request.user
    raw_status = (request.data.get("status") or "").strip().lower()
    if raw_status == "approve" or raw_status == "approved":
        st = PurchaseApprovalRequest.Status.APPROVED
    elif raw_status == "reject" or raw_status == "rejected":
        st = PurchaseApprovalRequest.Status.REJECTED
    else:
        return Response(
            {"detail": "status must be approved or rejected."},
            status=400,
        )
    note = str(request.data.get("parent_note") or "")
    try:
        par = approve_or_reject_request(
            acting_parent=u,
            request_id=pk,
            status=st,
            parent_note=note,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(_par_serializer(par))
