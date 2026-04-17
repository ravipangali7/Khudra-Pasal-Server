"""Super-admin APIs to manage vendor suppliers and stock purchases (procurement), mirroring vendor portal behavior."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Product, Supplier, Vendor, VendorLedgerEntry, VendorStockPurchase, VendorStockPurchaseLine
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
    _vendor_ledger_create,
    _vendor_ledger_row,
)


def _vendor_or_404(vendor_pk: int) -> tuple[Vendor | None, Response | None]:
    v = Vendor.objects.filter(pk=vendor_pk).first()
    if not v:
        return None, Response({"detail": "Vendor not found."}, status=404)
    return v, None


def _vendor_ledger_amount_totals(qs):
    """Sum credits (amount > 0) and debits (amount < 0) over the full queryset (not one page)."""
    credit_sum = qs.filter(amount__gt=0).aggregate(s=Sum("amount"))["s"]
    debit_sum = qs.filter(amount__lt=0).aggregate(s=Sum("amount"))["s"]
    credit = Decimal(credit_sum or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    neg_total = Decimal(debit_sum or 0)
    debit = (-neg_total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    balance = (credit - debit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {
        "credit": float(credit),
        "debit": float(debit),
        "balance": float(balance),
    }


def _admin_suppliers_list_response(request, vendor: Vendor | None) -> Response:
    q = (request.query_params.get("q") or "").strip()
    qs = _supplier_queryset_with_ledger(vendor).order_by("name")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))

    totals = qs.aggregate(
        supplier_count=Count("pk", distinct=True),
        total_ledger_credit=Coalesce(
            Sum("ledger_credit"),
            Value(Decimal("0"), output_field=DecimalField(max_digits=14, decimal_places=2)),
        ),
    )
    credit_raw = totals.get("total_ledger_credit") or Decimal("0")
    credit = credit_raw if isinstance(credit_raw, Decimal) else Decimal(str(credit_raw))
    credit = credit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    summary = {
        "supplier_count": int(totals.get("supplier_count") or 0),
        "total_ledger_credit": float(credit),
        "total_ledger_debit": 0.0,
        "total_ledger_balance": float(credit),
    }

    paginator, page = _paginate(request, qs)
    rows = [_supplier_payload(s) for s in page]
    if vendor is None:
        for row, supplier in zip(rows, page):
            row["vendor_id"] = str(supplier.vendor_id)
            row["vendor_name"] = supplier.vendor.store_name
    resp = paginator.get_paginated_response(rows)
    resp.data["summary"] = summary
    return resp


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_suppliers(request, vendor_pk: int):
    if err := _forbidden(request):
        return err
    vendor, not_found = _vendor_or_404(vendor_pk)
    if not_found:
        return not_found
    if request.method == "GET":
        return _admin_suppliers_list_response(request, vendor)
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required", field="name")
    s = Supplier.objects.create(
        vendor=vendor,
        name=name[:200],
        supplier_code="",
        phone=(request.data.get("phone") or "").strip()[:20],
        email=(request.data.get("email") or "").strip()[:254],
        address=(request.data.get("address") or "")[:2000],
        notes=(request.data.get("notes") or "")[:2000],
        is_active=bool(request.data.get("is_active", True)),
    )
    return Response(_supplier_payload(s), status=201)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_suppliers_all(request):
    if err := _forbidden(request):
        return err
    return _admin_suppliers_list_response(request, None)


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_supplier_detail(request, vendor_pk: int, pk: int):
    if err := _forbidden(request):
        return err
    vendor, not_found = _vendor_or_404(vendor_pk)
    if not_found:
        return not_found
    s = _supplier_queryset_with_ledger(vendor).filter(pk=pk).first()
    if not s:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(_supplier_payload(s))
    if request.method == "DELETE":
        if s.stock_purchases.exists():
            return Response(
                {"detail": "Cannot delete supplier with purchase history."},
                status=400,
            )
        s.delete()
        return Response({"ok": True})
    if "name" in request.data:
        s.name = (request.data.get("name") or "").strip()[:200]
    if "phone" in request.data:
        s.phone = (request.data.get("phone") or "").strip()[:20]
    if "email" in request.data:
        s.email = (request.data.get("email") or "").strip()[:254]
    if "address" in request.data:
        s.address = (request.data.get("address") or "")[:2000]
    if "notes" in request.data:
        s.notes = (request.data.get("notes") or "")[:2000]
    if "is_active" in request.data:
        s.is_active = bool(request.data.get("is_active"))
    s.save()
    return Response(_supplier_payload(s))


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_ledger(request, vendor_pk: int):
    if err := _forbidden(request):
        return err
    vendor, not_found = _vendor_or_404(vendor_pk)
    if not_found:
        return not_found
    if request.method == "POST":
        return _vendor_ledger_create(request, vendor)

    qs = VendorLedgerEntry.objects.filter(vendor=vendor).order_by("-created_at")
    et = (request.query_params.get("entry_type") or "").strip()
    if et:
        qs = qs.filter(entry_type=et)
    totals = _vendor_ledger_amount_totals(qs)
    paginator, page = _paginate(request, qs)
    rows = [_vendor_ledger_row(e) for e in page]
    resp = paginator.get_paginated_response(rows)
    resp.data["ledger_totals"] = totals
    return resp


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_ledger_all(request):
    if err := _forbidden(request):
        return err
    qs = VendorLedgerEntry.objects.select_related("vendor").order_by("vendor__store_name", "-created_at")
    et = (request.query_params.get("entry_type") or "").strip()
    if et:
        qs = qs.filter(entry_type=et)
    totals = _vendor_ledger_amount_totals(qs)
    paginator, page = _paginate(request, qs)
    rows: list[dict] = []
    for e in page:
        row = _vendor_ledger_row(e)
        row["vendor_id"] = str(e.vendor_id)
        row["vendor_name"] = e.vendor.store_name
        rows.append(row)
    resp = paginator.get_paginated_response(rows)
    resp.data["ledger_totals"] = totals
    return resp


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_supplier_ledger(request, vendor_pk: int, sp_pk: int):
    """Posted stock purchases for one supplier (same payload as vendor portal)."""
    if err := _forbidden(request):
        return err
    vendor, not_found = _vendor_or_404(vendor_pk)
    if not_found:
        return not_found
    s = Supplier.objects.filter(pk=sp_pk, vendor=vendor).first()
    if not s:
        return Response({"detail": "Not found."}, status=404)
    purchases = (
        VendorStockPurchase.objects.filter(
            supplier=s, vendor=vendor, status=VendorStockPurchase.Status.POSTED
        )
        .order_by("posted_at", "created_at", "pk")
    )
    rows: list[dict] = []
    balance = Decimal("0")
    total_credit = Decimal("0")
    for p in purchases:
        credit = p.total
        debit = Decimal("0")
        total_credit += credit
        balance = balance + credit - debit
        ts = p.posted_at or p.created_at
        rows.append(
            {
                "date": ts.isoformat(),
                "reference": p.reference,
                "description": f"Stock purchase {p.reference}",
                "debit": float(debit),
                "credit": float(credit),
                "balance": float(balance),
            }
        )
    return Response(
        {
            "supplier": {"id": str(s.pk), "name": s.name},
            "rows": rows,
            "totals": {
                "debit": 0.0,
                "credit": float(total_credit),
                "balance": float(balance),
            },
        }
    )


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


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_stock_purchase_detail(request, vendor_pk: int, pk: int):
    if err := _forbidden(request):
        return err
    vendor, not_found = _vendor_or_404(vendor_pk)
    if not_found:
        return not_found
    p = (
        VendorStockPurchase.objects.filter(pk=pk, vendor=vendor)
        .select_related("supplier")
        .prefetch_related("lines__product")
        .first()
    )
    if not p:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(_purchase_payload(p))
    if request.method == "DELETE":
        if p.status != VendorStockPurchase.Status.DRAFT:
            return Response({"detail": "Only draft purchases can be deleted."}, status=400)
        p.delete()
        return Response({"ok": True})
    if p.status != VendorStockPurchase.Status.DRAFT:
        return Response({"detail": "Only draft purchases can be edited."}, status=400)
    if "supplier_id" in request.data:
        try:
            sid = int(request.data.get("supplier_id"))
        except (TypeError, ValueError):
            return validation_error("invalid supplier_id", field="supplier_id")
        supplier = Supplier.objects.filter(pk=sid, vendor=vendor).first()
        if not supplier:
            return validation_error("supplier not found", field="supplier_id")
        p.supplier = supplier
    if "notes" in request.data:
        p.notes = (request.data.get("notes") or "")[:2000]
    if "tax" in request.data:
        p.tax = Decimal(str(request.data.get("tax") or "0")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    if "lines" in request.data:
        try:
            lines_spec = _parse_lines(request.data.get("lines"), vendor)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        p.lines.all().delete()
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
    p.save(update_fields=["subtotal", "total", "tax", "supplier", "notes"])
    p = VendorStockPurchase.objects.select_related("supplier").prefetch_related("lines__product").get(
        pk=p.pk
    )
    return Response(_purchase_payload(p))


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_stock_purchases_all(request):
    if err := _forbidden(request):
        return err
    qs = VendorStockPurchase.objects.select_related("supplier", "vendor").order_by("-created_at")
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
                "vendor_id": str(p.vendor_id),
                "vendor_name": p.vendor.store_name,
                "total": float(p.total),
                "created_at": p.created_at.isoformat(),
                "posted_at": p.posted_at.isoformat() if p.posted_at else None,
            }
        )
    return paginator.get_paginated_response(rows)


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
