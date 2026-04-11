"""Vendor suppliers, stock purchases (procurement), and ledger APIs."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Product, Supplier, VendorLedgerEntry, VendorStockPurchase, VendorStockPurchaseLine
from core.services.stock_purchase_service import (
    generate_purchase_reference,
    post_stock_purchase,
    recompute_purchase_totals,
)
from core.views.admin.admin_write_utils import validation_error
from core.views.vendor.common import vendor_or_error
from core.views.vendor.vendor_resources import _paginate
def _supplier_payload(s: Supplier) -> dict:
    row: dict = {
        "id": str(s.pk),
        "name": s.name,
        "phone": s.phone or "",
        "email": s.email or "",
        "address": s.address or "",
        "notes": s.notes or "",
        "is_active": s.is_active,
        "created_at": s.created_at.isoformat(),
    }
    if hasattr(s, "ledger_credit"):
        credit = getattr(s, "ledger_credit") or Decimal("0")
        debit = Decimal("0")
        row["ledger_credit"] = float(credit)
        row["ledger_debit"] = float(debit)
        row["ledger_balance"] = float(credit - debit)
    return row


def _line_payload(line: VendorStockPurchaseLine) -> dict:
    return {
        "id": str(line.pk),
        "product_id": str(line.product_id),
        "product_name": line.product.name,
        "sku": line.product.sku,
        "quantity": line.quantity,
        "unit_cost": float(line.unit_cost),
        "line_total": float(line.line_total),
    }


def _purchase_payload(p: VendorStockPurchase) -> dict:
    return {
        "id": str(p.pk),
        "reference": p.reference,
        "status": p.status,
        "supplier_id": str(p.supplier_id),
        "supplier_name": p.supplier.name,
        "subtotal": float(p.subtotal),
        "tax": float(p.tax),
        "total": float(p.total),
        "notes": p.notes or "",
        "created_at": p.created_at.isoformat(),
        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
        "lines": [_line_payload(x) for x in p.lines.select_related("product").all()],
    }


def _parse_lines(raw, vendor) -> list[tuple[int, int, Decimal]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("lines must be an array")
    if not raw:
        return []
    out: list[tuple[int, int, Decimal]] = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("each line must be an object")
        try:
            pid = int(row.get("product_id"))
            qty = int(row.get("quantity"))
        except (TypeError, ValueError) as e:
            raise ValueError("line needs product_id and quantity") from e
        uc_raw = row.get("unit_cost")
        if uc_raw is None:
            raise ValueError("line needs unit_cost")
        unit_cost = Decimal(str(uc_raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if qty < 1:
            raise ValueError("quantity must be >= 1")
        p = Product.objects.filter(pk=pid, seller=vendor).first()
        if not p:
            raise ValueError(f"Product {pid} not found for this vendor")
        if p.type == Product.Type.DIGITAL:
            raise ValueError(f"Product {p.name} is digital")
        out.append((pid, qty, unit_cost))
    return out


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_suppliers(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if request.method == "GET":
        q = (request.query_params.get("q") or "").strip()
        qs = _supplier_queryset_with_ledger(vendor).order_by("name")
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q))
        paginator, page = _paginate(request, qs)
        rows = [_supplier_payload(s) for s in page]
        return paginator.get_paginated_response(rows)
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


def _supplier_queryset_with_ledger(vendor):
    posted_filter = Q(stock_purchases__status=VendorStockPurchase.Status.POSTED)
    return Supplier.objects.filter(vendor=vendor).annotate(
        ledger_credit=Coalesce(
            Sum("stock_purchases__total", filter=posted_filter),
            Value(Decimal("0"), output_field=DecimalField(max_digits=12, decimal_places=2)),
        ),
    )


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_supplier_detail(request, pk: int):
    vendor, err = vendor_or_error(request)
    if err:
        return err
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
def vendor_stock_purchases(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
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


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_stock_purchase_detail(request, pk: int):
    vendor, err = vendor_or_error(request)
    if err:
        return err
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


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_stock_purchase_post(request, pk: int):
    vendor, err = vendor_or_error(request)
    if err:
        return err
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


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_ledger(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = VendorLedgerEntry.objects.filter(vendor=vendor).order_by("-created_at")
    et = (request.query_params.get("entry_type") or "").strip()
    if et:
        qs = qs.filter(entry_type=et)
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(e.pk),
            "entry_type": e.entry_type,
            "amount": float(e.amount),
            "description": e.description,
            "reference_type": e.reference_type or "",
            "reference_id": e.reference_id or "",
            "created_at": e.created_at.isoformat(),
        }
        for e in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_supplier_ledger(request, pk: int):
    """Posted stock purchases for one supplier: debit/credit columns and running balance (payables)."""
    vendor, err = vendor_or_error(request)
    if err:
        return err
    s = Supplier.objects.filter(pk=pk, vendor=vendor).first()
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
