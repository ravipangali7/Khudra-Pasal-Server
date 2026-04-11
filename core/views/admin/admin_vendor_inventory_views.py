"""Super-admin APIs to manage vendor suppliers and stock purchases (procurement), mirroring vendor portal behavior."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Q
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Product, Supplier, Vendor, VendorStockPurchase, VendorStockPurchaseLine
from core.services.stock_purchase_service import (
    generate_purchase_reference,
    post_stock_purchase,
    recompute_purchase_totals,
)
from core.views.admin.admin_write_utils import validation_error
from core.views.admin.resource_views import _forbidden, _paginate
from core.views.vendor.vendor_inventory_views import (
    _parse_lines,
    _purchase_payload,
    _supplier_payload,
    _supplier_queryset_with_ledger,
)


def _vendor_or_404(vendor_pk: int) -> tuple[Vendor | None, Response | None]:
    v = Vendor.objects.filter(pk=vendor_pk).first()
    if not v:
        return None, Response({"detail": "Vendor not found."}, status=404)
    return v, None


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_suppliers_list(request, vendor_pk: int):
    if err := _forbidden(request):
        return err
    vendor, not_found = _vendor_or_404(vendor_pk)
    if not_found:
        return not_found
    q = (request.query_params.get("q") or "").strip()
    qs = _supplier_queryset_with_ledger(vendor).order_by("name")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
    paginator, page = _paginate(request, qs)
    rows = [_supplier_payload(s) for s in page]
    return paginator.get_paginated_response(rows)


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_stock_purchases(request, vendor_pk: int):
    if err := _forbidden(request):
        return err
    vendor, not_found = _vendor_or_404(vendor_pk)
    if not_found:
        return not_found
    if request.method == "GET":
        qs = VendorStockPurchase.objects.filter(vendor=vendor).select_related("supplier").order_by(
            "-created_at"
        )
        st = (request.query_params.get("status") or "").strip()
        if st in (VendorStockPurchase.Status.DRAFT, VendorStockPurchase.Status.POSTED):
            qs = qs.filter(status=st)
        paginator, page = _paginate(request, qs)
        rows = []
        for p in page:
            rows.append(
                {
                    "id": str(p.pk),
                    "reference": p.reference,
                    "status": p.status,
                    "supplier_id": str(p.supplier_id),
                    "supplier_name": p.supplier.name,
                    "total": float(p.total),
                    "created_at": p.created_at.isoformat(),
                    "posted_at": p.posted_at.isoformat() if p.posted_at else None,
                }
            )
        return paginator.get_paginated_response(rows)
    try:
        sid = int(request.data.get("supplier_id"))
    except (TypeError, ValueError):
        return validation_error("supplier_id required", field="supplier_id")
    supplier = Supplier.objects.filter(pk=sid, vendor=vendor).first()
    if not supplier:
        return validation_error("supplier not found", field="supplier_id")
    try:
        lines_spec = _parse_lines(request.data.get("lines"), vendor)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    notes = (request.data.get("notes") or "")[:2000]
    tax = Decimal("0")
    if request.data.get("tax") is not None:
        tax = Decimal(str(request.data.get("tax"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    ref = generate_purchase_reference(vendor.pk)
    with transaction.atomic():
        p = VendorStockPurchase.objects.create(
            vendor=vendor,
            supplier=supplier,
            reference=ref,
            status=VendorStockPurchase.Status.DRAFT,
            tax=tax,
            notes=notes,
        )
        for pid, qty, unit_cost in lines_spec:
            lt = (unit_cost * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            VendorStockPurchaseLine.objects.create(
                purchase=p,
                product_id=pid,
                quantity=qty,
                unit_cost=unit_cost,
                line_total=lt,
            )
        recompute_purchase_totals(p)
        p.save(update_fields=["subtotal", "total", "tax"])
    p = VendorStockPurchase.objects.select_related("supplier").prefetch_related("lines__product").get(
        pk=p.pk
    )
    return Response(_purchase_payload(p), status=201)


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_stock_purchase_post(request, vendor_pk: int, pk: int):
    if err := _forbidden(request):
        return err
    vendor, not_found = _vendor_or_404(vendor_pk)
    if not_found:
        return not_found
    p = VendorStockPurchase.objects.filter(pk=pk, vendor=vendor).first()
    if not p:
        return Response({"detail": "Not found."}, status=404)
    try:
        with transaction.atomic():
            post_stock_purchase(p, acting_user_id=request.user.pk)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    p = VendorStockPurchase.objects.select_related("supplier").prefetch_related("lines__product").get(
        pk=p.pk
    )
    return Response(_purchase_payload(p))
