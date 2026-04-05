"""Vendor portal CRUD and extended read APIs."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.db.models import Count, Max, Q, Sum
from django.db.models.deletion import ProtectedError
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import (
    Attribute,
    AttributeValue,
    Brand,
    Category,
    Coupon,
    FAQ,
    FlashDeal,
    FlashDealProduct,
    Order,
    OrderCommissionSettlement,
    OrderItem,
    Product,
    ProductApproval,
    ProductReview,
    Refund,
    Reel,
    SupportTicket,
    SupportTicketMessage,
    Unit,
    User,
    VendorBankDetail,
    WalletWithdrawal,
)
from core.serializers import ReelPublicSerializer
from core.services import product_service, support_notification_service, support_ticket_service
from core.services.reel_boost_patch import apply_reel_boost_from_data
from core.services.vendor_service import ensure_vendor_wallet
from core.views.admin.admin_write_utils import absolute_media_url, validation_error
from core.views.admin.resource_views import _make_unique_slug, _to_decimal
from core.views.vendor.common import (
    get_or_create_pos_walkin_user,
    media_url,
    parse_reel_tags,
    vendor_or_error,
)
from core.views.vendor.vendor_views import VendorPagination


def _paginate(request, queryset):
    paginator = VendorPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator, page


def _gen_order_number():
    for _ in range(20):
        cand = f"KP-{uuid4().hex[:12].upper()}"
        if len(cand) <= 20 and not Order.objects.filter(order_number=cand).exists():
            return cand
    return f"KP-{uuid4().hex[:12].upper()}"[:20]


def _gen_withdrawal_number():
    return f"WTH-{timezone.now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"


def _gen_ticket_number():
    return f"TKT-{uuid4().hex[:8].upper()}"


def _flash_deal_refresh_status(row: FlashDeal) -> None:
    now = timezone.now()
    if row.end_at <= now:
        row.status = FlashDeal.Status.EXPIRED
    elif row.start_at > now:
        row.status = FlashDeal.Status.SCHEDULED
    else:
        row.status = FlashDeal.Status.ACTIVE


def _vendor_flash_deal_set_products(vendor, deal: FlashDeal, product_ids) -> None:
    if product_ids is None:
        return
    if not isinstance(product_ids, list):
        return
    deal.deal_products.all().delete()
    for raw in product_ids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if Product.objects.filter(pk=pid, seller=vendor).exists():
            FlashDealProduct.objects.get_or_create(
                flash_deal=deal, product_id=pid, defaults={"override_price": None}
            )


# --- Profile ---


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_profile(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if request.method == "GET":
        product_count = Product.objects.filter(seller=vendor).count()
        review_count = ProductReview.objects.filter(
            product__seller=vendor,
            status=ProductReview.Status.APPROVED,
        ).count()
        return Response(
            {
                "id": str(vendor.pk),
                "store_name": vendor.store_name,
                "store_slug": vendor.store_slug,
                "description": vendor.description,
                "contact_email": vendor.contact_email,
                "phone": vendor.phone,
                "address": vendor.address,
                "logo_url": media_url(request, vendor.logo),
                "banner_url": media_url(request, vendor.banner),
                "status": vendor.status,
                "rating": float(vendor.rating),
                "is_verified": vendor.is_verified,
                "product_count": product_count,
                "review_count": review_count,
            }
        )
    if "store_name" in request.data:
        vendor.store_name = (request.data.get("store_name") or "").strip()[:150]
    if "description" in request.data:
        vendor.description = request.data.get("description") or ""
    if "contact_email" in request.data:
        vendor.contact_email = (request.data.get("contact_email") or "").strip()[:254]
    if "phone" in request.data:
        vendor.phone = (request.data.get("phone") or "").strip()[:15]
    if "address" in request.data:
        vendor.address = request.data.get("address") or ""
    if request.FILES.get("logo"):
        vendor.logo = request.FILES["logo"]
    if request.FILES.get("banner"):
        vendor.banner = request.FILES["banner"]
    vendor.save()
    return Response(
        {
            "id": str(vendor.pk),
            "store_name": vendor.store_name,
            "logo_url": media_url(request, vendor.logo),
            "banner_url": media_url(request, vendor.banner),
        }
    )


# --- Catalog (read-only) ---


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_catalog_categories(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    qs = Category.objects.filter(status=Category.Status.ACTIVE).order_by("sort_order", "name")
    rows = [
        {
            "id": str(c.pk),
            "name": c.name,
            "slug": c.slug,
            "parent_id": str(c.parent_id) if c.parent_id else None,
            "level": c.level,
        }
        for c in qs
    ]
    return Response({"results": rows})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_catalog_brands(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    qs = Brand.objects.filter(status=Brand.Status.ACTIVE).order_by("name")
    return Response(
        {
            "results": [
                {"id": str(b.pk), "name": b.name, "logo_url": media_url(request, b.logo)}
                for b in qs
            ]
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_catalog_units(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    qs = Unit.objects.filter(status=Unit.Status.ACTIVE).order_by("name")
    return Response(
        {"results": [{"id": str(u.pk), "name": u.name, "short_name": u.short_name} for u in qs]}
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_catalog_attributes(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    qs = Attribute.objects.filter(status=Attribute.Status.ACTIVE).prefetch_related("values")
    out = []
    for a in qs:
        vals = [
            {"id": str(v.pk), "value": v.value}
            for v in a.values.filter(status=AttributeValue.Status.ACTIVE)
        ]
        out.append({"id": str(a.pk), "name": a.name, "type": a.type, "values": vals})
    return Response({"results": out})


# --- Products ---


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_product_slug_preview(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    name = (request.query_params.get("name") or "").strip()
    if not name:
        return validation_error("name query required", field="name")
    slug = _make_unique_slug(Product, name)
    return Response({"slug": slug})


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_product_detail(request, pk):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    row = Product.objects.filter(pk=pk, seller=vendor).select_related("category", "brand", "unit").first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(
            {
                "id": str(row.pk),
                "name": row.name,
                "slug": row.slug,
                "sku": row.sku,
                "description": row.description,
                "short_description": row.short_description,
                "price": str(row.price),
                "discount_price": str(row.discount_price) if row.discount_price is not None else None,
                "tax_percent": str(row.tax_percent),
                "category_id": str(row.category_id),
                "brand_id": str(row.brand_id) if row.brand_id else None,
                "unit_id": str(row.unit_id) if row.unit_id else None,
                "type": row.type,
                "stock": row.stock,
                "status": row.status,
                "image_url": media_url(request, row.image),
                "attributes": row.attributes or {},
                "seo_title": row.seo_title,
                "seo_description": row.seo_description,
                "seo_keywords": row.seo_keywords,
            }
        )
    if request.method == "DELETE":
        try:
            row.delete()
            return Response({"ok": True, "deleted": True})
        except ProtectedError as exc:
            # Product is referenced by historical orders (on_delete=PROTECT).
            # Fallback to soft-delete semantics: hide from vendor/frontend listings
            # while preserving historical order integrity.
            row.status = Product.Status.DRAFT
            row.stock = 0
            row.enable_pos = False
            row.enable_reels = False
            row.seller = None
            row.save(
                update_fields=[
                    "status",
                    "stock",
                    "enable_pos",
                    "enable_reels",
                    "seller",
                    "updated_at",
                ]
            )
            return Response(
                {
                    "ok": True,
                    "deleted": False,
                    "soft_deleted": True,
                    "detail": "Product is used in existing orders, so it was removed from listings instead of hard deleted.",
                    "code": "product_soft_deleted_in_use",
                    "protected_count": len(exc.protected_objects),
                },
                status=200,
            )
    for field in (
        "name",
        "description",
        "short_description",
        "sku",
        "status",
        "seo_title",
        "seo_description",
        "seo_keywords",
    ):
        if field in request.data:
            setattr(row, field, request.data.get(field))
    if "slug" in request.data or "name" in request.data:
        row.slug = _make_unique_slug(
            Product, request.data.get("slug") or request.data.get("name") or row.name, instance_pk=row.pk
        )
    if "price" in request.data:
        row.price = _to_decimal(request.data.get("price"), "0")
    if "discount_price" in request.data:
        row.discount_price = (
            _to_decimal(request.data.get("discount_price"), "0") if request.data.get("discount_price") else None
        )
    if "tax_percent" in request.data:
        row.tax_percent = _to_decimal(request.data.get("tax_percent"), "13")
    if "stock" in request.data:
        row.stock = int(request.data.get("stock") or 0)
    if "category_id" in request.data:
        row.category = Category.objects.filter(pk=request.data.get("category_id")).first() or row.category
    if "brand_id" in request.data:
        row.brand = Brand.objects.filter(pk=request.data.get("brand_id")).first()
    if "unit_id" in request.data:
        row.unit = Unit.objects.filter(pk=request.data.get("unit_id")).first()
    if "type" in request.data:
        row.type = request.data.get("type") or row.type
    if "attributes" in request.data:
        raw = request.data.get("attributes")
        if isinstance(raw, dict):
            row.attributes = raw
        elif isinstance(raw, str):
            try:
                row.attributes = json.loads(raw)
            except json.JSONDecodeError:
                pass
    for bfield in ("is_featured", "has_variations", "enable_reels", "enable_pos"):
        if bfield in request.data:
            setattr(row, bfield, str(request.data.get(bfield)).lower() == "true")
    image = request.FILES.get("image")
    if image:
        row.image = image
    row.save()
    product_service.sync_stock_status(row)
    return Response({"id": str(row.pk), "slug": row.slug})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_product_create(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    name = (request.data.get("name") or "").strip()
    sku = (request.data.get("sku") or "").strip()
    category_id = request.data.get("category_id")
    image = request.FILES.get("image")
    if not name or not sku or not category_id or not image:
        return Response({"detail": "name, sku, category_id and image are required"}, status=400)
    category = Category.objects.filter(pk=category_id).first()
    if not category:
        return Response({"detail": "invalid category_id"}, status=400)
    if Product.objects.filter(sku=sku).exists():
        return Response({"detail": "sku must be unique"}, status=400)
    raw_attrs = request.data.get("attributes")
    attrs: dict = {}
    if isinstance(raw_attrs, dict):
        attrs = raw_attrs
    elif isinstance(raw_attrs, str) and raw_attrs.strip():
        try:
            attrs = json.loads(raw_attrs)
        except json.JSONDecodeError:
            attrs = {}
    row = Product.objects.create(
        name=name,
        slug=_make_unique_slug(Product, request.data.get("slug") or name),
        description=request.data.get("description") or "",
        short_description=request.data.get("short_description") or "",
        sku=sku,
        price=_to_decimal(request.data.get("price"), "0"),
        discount_price=_to_decimal(request.data.get("discount_price"), "0")
        if request.data.get("discount_price")
        else None,
        tax_percent=_to_decimal(request.data.get("tax_percent"), "13"),
        category=category,
        brand=Brand.objects.filter(pk=request.data.get("brand_id")).first()
        if request.data.get("brand_id")
        else None,
        unit=Unit.objects.filter(pk=request.data.get("unit_id")).first()
        if request.data.get("unit_id")
        else None,
        image=image,
        type=request.data.get("type") or Product.Type.PHYSICAL,
        stock=int(request.data.get("stock") or 0),
        seller=vendor,
        status=Product.Status.DRAFT,
        is_featured=str(request.data.get("is_featured", "")).lower() == "true",
        has_variations=str(request.data.get("has_variations", "")).lower() == "true",
        seo_title=request.data.get("seo_title") or "",
        seo_description=request.data.get("seo_description") or "",
        seo_keywords=request.data.get("seo_keywords") or "",
        enable_reels=str(request.data.get("enable_reels", "")).lower() == "true",
        enable_pos=str(request.data.get("enable_pos", "")).lower() == "true",
        attributes=attrs if isinstance(attrs, dict) else {},
    )
    product_service.sync_stock_status(row)
    ProductApproval.objects.create(
        product=row,
        vendor=vendor,
        type=ProductApproval.Type.NEW,
        status=ProductApproval.Status.PENDING,
    )
    return Response({"id": str(row.pk), "name": row.name, "slug": row.slug}, status=201)


# --- Reviews ---


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_review_update(request, pk):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    r = ProductReview.objects.filter(pk=pk, product__seller=vendor).first()
    if not r:
        return Response({"detail": "Not found."}, status=404)
    if "reply_text" in request.data:
        r.reply_text = (request.data.get("reply_text") or "").strip()
        if r.reply_text:
            r.replied_at = timezone.now()
    if request.data.get("mark_read") in (True, "true", "1", 1):
        r.vendor_read_at = timezone.now()
    r.save()
    return Response({"id": str(r.pk)})


# --- Orders ---


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_order_detail(request, order_number):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    o = (
        Order.objects.filter(order_number=order_number, seller=vendor)
        .select_related("customer", "delivery_address")
        .prefetch_related("items__product")
        .first()
    )
    if not o:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        addr = getattr(o, "delivery_address", None)
        refunds = [
            {
                "refund_number": r.refund_number,
                "status": r.status,
                "amount": float(r.amount),
                "reason": r.reason,
                "created_at": r.created_at.isoformat(),
            }
            for r in Refund.objects.filter(order=o).order_by("-created_at")
        ]
        lines = []
        for it in o.items.all():
            p = it.product
            img = getattr(p, "image", None)
            lines.append(
                {
                    "name": p.name,
                    "sku": p.sku,
                    "qty": it.quantity,
                    "unit_price": float(it.unit_price),
                    "total": float(it.total_price),
                    "image_url": media_url(request, img) if img else "",
                }
            )
        address_payload = None
        if addr:
            lat = addr.latitude
            lng = addr.longitude
            address_payload = {
                "full_name": addr.full_name,
                "mobile": addr.mobile,
                "secondary_contact": addr.secondary_contact or "",
                "area_location": addr.area_location,
                "landmark": addr.landmark or "",
                "google_map_link": addr.google_map_link or "",
                "latitude": str(lat) if lat is not None else None,
                "longitude": str(lng) if lng is not None else None,
                "delivery_notes": addr.delivery_notes or "",
            }
        return Response(
            {
                "id": o.order_number,
                "pk": o.pk,
                "status": o.status,
                "payment_method": o.payment_method,
                "payment_status": o.payment_status,
                "subtotal": float(o.subtotal),
                "delivery_fee": float(o.delivery_fee),
                "discount_amount": float(o.discount_amount),
                "total": float(o.total),
                "notes": o.notes,
                "tracking_number": o.tracking_number,
                "carrier": o.carrier,
                "customer": o.customer.name,
                "customer_phone": o.customer.phone,
                "address": address_payload,
                "item_lines": lines,
                "refunds": refunds,
            }
        )
    if "status" in request.data:
        st = request.data.get("status")
        if st in dict(Order.Status.choices):
            o.status = st
    if "notes" in request.data:
        o.notes = request.data.get("notes") or ""
    if "tracking_number" in request.data:
        o.tracking_number = (request.data.get("tracking_number") or "")[:100]
    if "carrier" in request.data:
        o.carrier = (request.data.get("carrier") or "")[:100]
    o.save()
    return Response({"id": o.order_number, "status": o.status})


# --- Refunds (returns) ---


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_refunds_list(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = (
        Refund.objects.filter(order__seller=vendor)
        .select_related("order", "customer")
        .order_by("-created_at")
    )
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": r.refund_number,
            "order": r.order.order_number,
            "amount": float(r.amount),
            "reason": r.reason,
            "status": r.status,
            "date": r.created_at.date().isoformat(),
            "customer": r.customer.name,
        }
        for r in page
    ]
    return paginator.get_paginated_response(rows)


# --- POS ---


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_pos_checkout(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    items = request.data.get("items")
    if not isinstance(items, list) or not items:
        return validation_error("items must be a non-empty list", field="items")
    payment_method = request.data.get("payment_method") or Order.PaymentMethod.CASH
    if payment_method not in dict(Order.PaymentMethod.choices):
        return validation_error("invalid payment_method", field="payment_method")
    raw_cid = request.data.get("customer_id")
    if raw_cid in (None, ""):
        customer = get_or_create_pos_walkin_user()
    else:
        customer = User.objects.filter(pk=raw_cid).first()
        if not customer:
            return validation_error("customer not found", field="customer_id")

    tax_percent = _to_decimal(request.data.get("tax_percent"), "0")
    discount = _to_decimal(request.data.get("discount"), "0")

    try:
        with transaction.atomic():
            lines = []
            subtotal = Decimal("0")
            for raw in items:
                pid = raw.get("product_id")
                qty = int(raw.get("quantity") or 0)
                if not pid or qty < 1:
                    return validation_error("each item needs product_id and quantity", field="items")
                p = Product.objects.select_for_update().filter(pk=pid, seller=vendor).first()
                if not p:
                    return Response({"detail": f"Product {pid} not found for this vendor."}, status=400)
                if p.stock < qty:
                    return Response(
                        {"detail": f"Insufficient stock for {p.name}."},
                        status=400,
                    )
                unit_price = p.discount_price if p.discount_price is not None else p.price
                line_total = (unit_price * qty).quantize(Decimal("0.01"))
                subtotal += line_total
                lines.append((p, qty, unit_price, line_total))

            tax_amount = (subtotal * tax_percent / Decimal("100")).quantize(Decimal("0.01"))
            total = (subtotal + tax_amount - discount).quantize(Decimal("0.01"))
            if total < 0:
                total = Decimal("0")

            order = Order.objects.create(
                order_number=_gen_order_number(),
                customer=customer,
                seller=vendor,
                status=Order.Status.DELIVERED,
                payment_method=payment_method,
                payment_status=Order.PaymentStatus.PAID,
                subtotal=subtotal,
                delivery_fee=Decimal("0"),
                discount_amount=discount,
                total=total,
                want_delivery=False,
                notes=(request.data.get("notes") or "")[:500],
                is_pos_order=True,
            )
            for p, qty, unit_price, line_total in lines:
                oi = OrderItem.objects.create(
                    order=order,
                    product=p,
                    quantity=qty,
                    unit_price=unit_price,
                    total_price=line_total,
                )
                product_service.deduct_line_stock(oi)
                product_service.sync_stock_status(p)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)

    return Response({"order_number": order.order_number, "total": float(order.total)}, status=201)


# --- Coupons ---


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_coupons_list_create(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if request.method == "GET":
        qs = Coupon.objects.filter(vendor=vendor).select_related("category").order_by("-created_at")
        paginator, page = _paginate(request, qs)
        rows = [
            {
                "id": str(c.pk),
                "code": c.code,
                "type": c.type,
                "value": float(c.value),
                "min_order": float(c.min_order),
                "usage_limit": c.usage_limit,
                "used_count": c.used_count,
                "status": c.status,
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                "category_id": str(c.category_id) if c.category_id else None,
            }
            for c in page
        ]
        return paginator.get_paginated_response(rows)
    code = (request.data.get("code") or "").strip().upper()
    if not code:
        return validation_error("code required", field="code")
    if Coupon.objects.filter(code=code).exists():
        return validation_error("code already exists", field="code")
    exp_raw = request.data.get("expires_at")
    exp_dt = None
    if exp_raw:
        exp_dt = parse_datetime(str(exp_raw).replace("Z", "+00:00"))
        if exp_dt and timezone.is_naive(exp_dt):
            exp_dt = timezone.make_aware(exp_dt, timezone.get_current_timezone())
    c = Coupon.objects.create(
        code=code[:30],
        type=request.data.get("type") or Coupon.Type.PERCENTAGE,
        value=_to_decimal(request.data.get("value"), "0"),
        min_order=_to_decimal(request.data.get("min_order"), "0"),
        usage_limit=int(request.data.get("usage_limit") or 0) or None,
        status=request.data.get("status") or Coupon.Status.ACTIVE,
        expires_at=exp_dt,
        vendor=vendor,
        category=Category.objects.filter(pk=request.data.get("category_id")).first()
        if request.data.get("category_id")
        else None,
    )
    return Response({"id": str(c.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_coupon_detail(request, pk):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    c = Coupon.objects.filter(pk=pk, vendor=vendor).first()
    if not c:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        c.delete()
        return Response({"ok": True})
    if "value" in request.data:
        c.value = _to_decimal(request.data.get("value"), "0")
    if "min_order" in request.data:
        c.min_order = _to_decimal(request.data.get("min_order"), "0")
    if "usage_limit" in request.data:
        v = request.data.get("usage_limit")
        c.usage_limit = int(v) if v not in (None, "") else None
    if "status" in request.data:
        c.status = request.data.get("status")
    if "expires_at" in request.data:
        exp_raw = request.data.get("expires_at")
        if not exp_raw:
            c.expires_at = None
        else:
            exp_dt = parse_datetime(str(exp_raw).replace("Z", "+00:00"))
            if exp_dt and timezone.is_naive(exp_dt):
                exp_dt = timezone.make_aware(exp_dt, timezone.get_current_timezone())
            c.expires_at = exp_dt
    c.save()
    return Response({"id": str(c.pk)})


# --- Flash deals ---


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_flash_deals_list(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if request.method == "POST":
        name = (request.data.get("name") or "").strip()
        if not name:
            return validation_error("name required", field="name")
        start_raw = request.data.get("start_at") or request.data.get("startDate")
        end_raw = request.data.get("end_at") or request.data.get("endDate")
        start_at = parse_datetime(str(start_raw).replace("Z", "+00:00")) if start_raw else None
        end_at = parse_datetime(str(end_raw).replace("Z", "+00:00")) if end_raw else None
        if start_at and timezone.is_naive(start_at):
            start_at = timezone.make_aware(start_at, timezone.get_current_timezone())
        if end_at and timezone.is_naive(end_at):
            end_at = timezone.make_aware(end_at, timezone.get_current_timezone())
        if not start_at or not end_at:
            return validation_error("start_at and end_at are required", field="start_at")
        row = FlashDeal(
            name=name[:150],
            discount_percent=_to_decimal(request.data.get("discount_percent"), "0"),
            start_at=start_at,
            end_at=end_at,
            priority=int(request.data.get("priority") or 0),
            status=FlashDeal.Status.SCHEDULED,
            vendor=vendor,
        )
        _flash_deal_refresh_status(row)
        row.save()
        _vendor_flash_deal_set_products(vendor, row, request.data.get("product_ids"))
        return Response({"id": str(row.pk)}, status=201)

    qs = (
        FlashDeal.objects.filter(Q(vendor=vendor) | Q(deal_products__product__seller=vendor))
        .distinct()
        .annotate(product_count=Count("deal_products", distinct=True))
        .prefetch_related("deal_products")
        .order_by("-start_at")
    )
    paginator, page = _paginate(request, qs)
    rows = []
    for d in page:
        _flash_deal_refresh_status(d)
        FlashDeal.objects.filter(pk=d.pk).update(status=d.status)
        pids = [str(x.product_id) for x in d.deal_products.all()]
        rows.append(
            {
                "id": str(d.pk),
                "name": d.name,
                "discount_percent": float(d.discount_percent),
                "start_at": d.start_at.isoformat(),
                "end_at": d.end_at.isoformat(),
                "status": d.status,
                "priority": d.priority,
                "vendor_id": str(d.vendor_id) if d.vendor_id else None,
                "is_owner": d.vendor_id == vendor.pk,
                "product_count": int(getattr(d, "product_count", 0) or 0),
                "product_ids": pids,
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_flash_deal_detail(request, pk):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    deal = FlashDeal.objects.filter(pk=pk).first()
    if not deal:
        return Response({"detail": "Not found."}, status=404)
    if deal.vendor_id != vendor.pk:
        return Response({"detail": "You can only edit or delete flash deals you created."}, status=403)
    if request.method == "DELETE":
        deal.delete()
        return Response({"ok": True})
    if "name" in request.data:
        nm = (request.data.get("name") or "").strip()
        if nm:
            deal.name = nm[:150]
    if "discount_percent" in request.data or "discount" in request.data:
        deal.discount_percent = _to_decimal(
            request.data.get("discount_percent") or request.data.get("discount"), "0"
        )
    if "start_at" in request.data or "startDate" in request.data:
        raw = request.data.get("start_at") or request.data.get("startDate")
        v = parse_datetime(str(raw).replace("Z", "+00:00")) if raw else None
        if v and timezone.is_naive(v):
            v = timezone.make_aware(v, timezone.get_current_timezone())
        if v:
            deal.start_at = v
    if "end_at" in request.data or "endDate" in request.data:
        raw = request.data.get("end_at") or request.data.get("endDate")
        v = parse_datetime(str(raw).replace("Z", "+00:00")) if raw else None
        if v and timezone.is_naive(v):
            v = timezone.make_aware(v, timezone.get_current_timezone())
        if v:
            deal.end_at = v
    if "priority" in request.data:
        deal.priority = int(request.data.get("priority") or 0)
    _flash_deal_refresh_status(deal)
    deal.save()
    if "product_ids" in request.data:
        _vendor_flash_deal_set_products(vendor, deal, request.data.get("product_ids"))
    return Response({"id": str(deal.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_flash_deal_add_products(request, deal_id):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    deal = FlashDeal.objects.filter(pk=deal_id).first()
    if not deal:
        return Response({"detail": "Not found."}, status=404)
    _flash_deal_refresh_status(deal)
    deal.save(update_fields=["status"])
    if deal.status == FlashDeal.Status.EXPIRED:
        return Response({"detail": "Deal expired."}, status=400)
    pids = request.data.get("product_ids")
    if not isinstance(pids, list):
        return validation_error("product_ids must be a list", field="product_ids")
    added = 0
    for raw in pids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if not Product.objects.filter(pk=pid, seller=vendor).exists():
            continue
        FlashDealProduct.objects.get_or_create(
            flash_deal=deal, product_id=pid, defaults={"override_price": None}
        )
        added += 1
    return Response({"added": added})


@api_view(["DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_flash_deal_remove_product(request, deal_id, product_id):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if not Product.objects.filter(pk=product_id, seller=vendor).exists():
        return Response({"detail": "Not found."}, status=404)
    FlashDealProduct.objects.filter(flash_deal_id=deal_id, product_id=product_id).delete()
    return Response({"ok": True})


# --- Withdrawals ---


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_withdrawals(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    ensure_vendor_wallet(vendor)
    wallet = vendor.wallet
    if request.method == "GET":
        qs = WalletWithdrawal.objects.filter(wallet=wallet).order_by("-created_at")
        paginator, page = _paginate(request, qs)
        rows = [
            {
                "id": w.withdrawal_number,
                "amount": float(w.amount),
                "method": w.method,
                "status": w.status,
                "date": w.created_at.date().isoformat(),
            }
            for w in page
        ]
        return paginator.get_paginated_response(rows)
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    if wallet.balance < amount:
        return Response({"detail": "Insufficient balance."}, status=400)
    method = request.data.get("method") or WalletWithdrawal.Method.BANK_TRANSFER
    if method not in dict(WalletWithdrawal.Method.choices):
        return validation_error("invalid method", field="method")
    method_account = (request.data.get("method_account") or request.data.get("account_number") or "").strip()
    if not method_account:
        return validation_error("method_account (or account_number) required", field="method_account")
    w = WalletWithdrawal.objects.create(
        withdrawal_number=_gen_withdrawal_number(),
        wallet=wallet,
        amount=amount,
        method=method,
        method_account=method_account[:100],
        bank_name=(request.data.get("bank_name") or "")[:100],
        account_holder=(request.data.get("account_holder") or "")[:150],
        status=WalletWithdrawal.Status.PENDING,
    )
    return Response({"id": w.withdrawal_number}, status=201)


# --- Customers ---


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_customers_list(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = (
        Order.objects.filter(seller=vendor)
        .values("customer_id")
        .annotate(orders=Count("id"), spent=Sum("total"), last_at=Max("created_at"))
    )
    search = (request.query_params.get("search") or "").strip()
    customer_ids = [r["customer_id"] for r in qs]
    users = {u.pk: u for u in User.objects.filter(pk__in=customer_ids)}
    rows = []
    for r in qs:
        u = users.get(r["customer_id"])
        if not u:
            continue
        if search and search.lower() not in u.name.lower() and search not in (u.phone or ""):
            continue
        rows.append(
            {
                "id": str(u.pk),
                "name": u.name,
                "email": u.email or "",
                "phone": u.phone,
                "orders": r["orders"],
                "spent": float(r["spent"] or 0),
                "lastOrder": r["last_at"].date().isoformat() if r["last_at"] else "",
            }
        )
    rows.sort(key=lambda x: x["lastOrder"], reverse=True)
    page_size = int(request.query_params.get("page_size") or 30)
    page = int(request.query_params.get("page") or 1)
    start = (page - 1) * page_size
    chunk = rows[start : start + page_size]
    return Response(
        {
            "count": len(rows),
            "next": None,
            "previous": None,
            "results": chunk,
        }
    )


# --- Reports ---


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_reports_summary(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    end_d = parse_date(request.query_params.get("to") or "") or timezone.localdate()
    start_d = parse_date(request.query_params.get("from") or "") or (end_d - timedelta(days=30))
    start_dt = timezone.make_aware(datetime.combine(start_d, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end_d, datetime.max.time()))
    oq = Order.objects.filter(seller=vendor, created_at__gte=start_dt, created_at__lte=end_dt)
    daily = list(
        oq.annotate(d=TruncDate("created_at"))
        .values("d")
        .annotate(sales=Sum("total"), orders=Count("id"))
        .order_by("d")
    )
    daily_out = [
        {
            "day": (x["d"].isoformat() if x["d"] else ""),
            "sales": float(x["sales"] or 0),
            "orders": x["orders"],
        }
        for x in daily
    ]
    cat_rows = (
        OrderItem.objects.filter(
            order__seller=vendor,
            order__created_at__gte=start_dt,
            order__created_at__lte=end_dt,
        )
        .values("product__category__name")
        .annotate(revenue=Sum("total_price"))
    )
    category_breakdown = [
        {"name": r["product__category__name"] or "—", "value": float(r["revenue"] or 0)}
        for r in cat_rows
    ]
    status_rows = oq.values("status").annotate(c=Count("id"))
    order_status_data = [{"name": r["status"], "value": r["c"]} for r in status_rows]
    commission = float(vendor.commission_rate or 0)
    gross = float(oq.aggregate(t=Sum("total"))["t"] or 0)
    earnings_estimate = gross * (100 - commission) / 100.0 if gross else 0.0
    settled = OrderCommissionSettlement.objects.filter(
        vendor=vendor,
        created_at__gte=start_dt,
        created_at__lte=end_dt,
    ).aggregate(s=Sum("vendor_amount"))["s"] or Decimal("0")
    wallet_settled_total = float(settled)
    return Response(
        {
            "daily": daily_out,
            "category_breakdown": category_breakdown,
            "order_status": order_status_data,
            "gross_sales": gross,
            "earnings_estimate": earnings_estimate,
            "wallet_settled_total": wallet_settled_total,
            "from": start_d.isoformat(),
            "to": end_d.isoformat(),
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_reports_export_csv(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    end_d = parse_date(request.query_params.get("to") or "") or timezone.localdate()
    start_d = parse_date(request.query_params.get("from") or "") or (end_d - timedelta(days=30))
    start_dt = timezone.make_aware(datetime.combine(start_d, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end_d, datetime.max.time()))
    oq = Order.objects.filter(seller=vendor, created_at__gte=start_dt, created_at__lte=end_dt).order_by(
        "-created_at"
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["order_number", "date", "customer", "status", "total", "payment_status"])
    for o in oq.iterator(chunk_size=200):
        w.writerow(
            [
                o.order_number,
                o.created_at.isoformat(),
                o.customer.name,
                o.status,
                float(o.total),
                o.payment_status,
            ]
        )
    resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="vendor-orders-{start_d}-{end_d}.csv"'
    return resp


# --- Support tickets ---


def _vendor_support_attachment_url(att_id: int) -> str:
    return f"/vendor/support/attachments/{att_id}/"


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_support_tickets(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if request.method == "GET":
        qs = SupportTicket.objects.filter(submitter=request.user).order_by(
            "-last_activity_at", "-created_at"
        )
        paginator, page = _paginate(request, qs)
        rows = [
            {
                "id": t.ticket_number,
                "subject": t.subject,
                "status": t.status,
                "priority": t.priority,
                "category": t.category,
                "source_panel": t.source_panel,
                "created": t.created_at.date().isoformat(),
                "last_activity": (t.last_activity_at or t.created_at).date().isoformat(),
            }
            for t in page
        ]
        return paginator.get_paginated_response(rows)
    subj = (request.data.get("subject") or "").strip()
    desc = (request.data.get("description") or "").strip()
    if not subj or not desc:
        return validation_error("subject and description required")
    pr = request.data.get("priority") or SupportTicket.Priority.MEDIUM
    if pr not in dict(SupportTicket.Priority.choices):
        pr = SupportTicket.Priority.MEDIUM
    cat = (request.data.get("category") or "").strip() or SupportTicket.Category.OTHER
    if cat not in dict(SupportTicket.Category.choices):
        cat = SupportTicket.Category.OTHER
    try:
        with transaction.atomic():
            t = SupportTicket.objects.create(
                ticket_number=_gen_ticket_number(),
                submitter=request.user,
                subject=subj[:255],
                description=desc,
                priority=pr,
                source_panel=SupportTicket.SourcePanel.VENDOR,
                category=cat,
                last_activity_at=timezone.now(),
            )
            support_ticket_service.append_message(t, request.user, desc)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    support_notification_service.notify_admins_new_ticket(t)
    return Response({"id": t.ticket_number}, status=201)


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_support_ticket_detail(request, ticket_number):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    t = (
        SupportTicket.objects.filter(ticket_number=ticket_number, submitter=request.user)
        .prefetch_related("messages__sender", "messages__attachments")
        .first()
    )
    if not t:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        support_ticket_service.ensure_initial_message(t)
        _av = lambda u: absolute_media_url(request, u.avatar)
        msgs = [
            support_ticket_service.message_to_row(
                m, _vendor_support_attachment_url, sender_avatar_url_fn=_av
            )
            for m in t.messages.all()
        ]
        return Response(
            {
                "id": t.ticket_number,
                "subject": t.subject,
                "description": t.description,
                "status": t.status,
                "priority": t.priority,
                "category": t.category,
                "source_panel": t.source_panel,
                "created": t.created_at.isoformat(),
                "last_activity_at": (t.last_activity_at or t.created_at).isoformat(),
                "messages": msgs,
            }
        )
    if request.method == "DELETE":
        t.delete()
        return Response({"ok": True})
    if "priority" in request.data:
        pr = request.data.get("priority")
        if pr in dict(SupportTicket.Priority.choices):
            t.priority = pr
    if "subject" in request.data:
        subj = (request.data.get("subject") or "").strip()
        if subj:
            t.subject = subj[:255]
    if "description" in request.data:
        t.description = request.data.get("description") or t.description
    t.save()
    return Response({"id": t.ticket_number})


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def vendor_support_ticket_messages(request, ticket_number):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    t = SupportTicket.objects.filter(ticket_number=ticket_number, submitter=request.user).first()
    if not t:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        before = request.query_params.get("before")
        if not before:
            return Response({"detail": "Query parameter 'before' (message id) is required."}, status=400)
        try:
            before_id = int(before)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid 'before'."}, status=400)
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        results, has_more = support_ticket_service.messages_page_before(
            t,
            before_id,
            limit,
            _vendor_support_attachment_url,
            sender_avatar_url_fn=lambda u: absolute_media_url(request, u.avatar),
        )
        return Response({"results": results, "has_more": has_more})

    body, files = support_ticket_service.extract_message_body_and_files_from_request(request)
    try:
        msg = support_ticket_service.append_message(t, request.user, body, files)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    preview = body or ("sent an attachment" if files else "")
    support_notification_service.notify_admins_ticket_activity(t, preview)
    msg = (
        SupportTicketMessage.objects.filter(pk=msg.pk)
        .select_related("sender")
        .prefetch_related("attachments")
        .first()
    )
    return Response(
        {
            "ok": True,
            "message": support_ticket_service.message_to_row(
                msg,
                _vendor_support_attachment_url,
                sender_avatar_url_fn=lambda u: absolute_media_url(request, u.avatar),
            ),
        },
        status=201,
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_support_attachment(request, attachment_id: int):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    att = support_ticket_service.get_attachment_or_none(attachment_id)
    if not att or not support_ticket_service.user_may_access_attachment_for_submitter(
        request.user, att
    ):
        return Response({"detail": "Not found."}, status=404)
    return support_ticket_service.attachment_file_response(att)


# --- FAQs ---


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_faqs_list(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    qs = FAQ.objects.filter(
        is_published=True,
        surface__in=[FAQ.Surface.VENDOR, FAQ.Surface.GENERAL],
    ).order_by("sort_order", "id")
    return Response(
        {
            "results": [
                {
                    "id": str(f.pk),
                    "question": f.question,
                    "answer": f.answer,
                    "surface": f.surface,
                }
                for f in qs
            ]
        }
    )


# --- Settings & bank ---


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_settings(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if request.method == "GET":
        return Response(
            {
                "email_notifications": vendor.portal_email_notifications,
                "sms_notifications": vendor.portal_sms_notifications,
                "language": vendor.portal_language,
            }
        )
    if "email_notifications" in request.data:
        vendor.portal_email_notifications = str(request.data.get("email_notifications")).lower() in (
            "1",
            "true",
            "yes",
        )
    if "sms_notifications" in request.data:
        vendor.portal_sms_notifications = str(request.data.get("sms_notifications")).lower() in (
            "1",
            "true",
            "yes",
        )
    if "language" in request.data:
        vendor.portal_language = (request.data.get("language") or "en")[:10]
    vendor.save()
    return Response(
        {
            "email_notifications": vendor.portal_email_notifications,
            "sms_notifications": vendor.portal_sms_notifications,
            "language": vendor.portal_language,
        }
    )


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_bank_detail(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    row, _created = VendorBankDetail.objects.get_or_create(
        vendor=vendor,
        defaults={
            "bank_name": "",
            "account_number": "",
            "account_holder": "",
        },
    )
    if request.method == "GET":
        return Response(
            {
                "bank_name": row.bank_name,
                "account_number": row.account_number,
                "account_holder": row.account_holder,
                "esewa_id": row.esewa_id,
                "khalti_id": row.khalti_id,
            }
        )
    if "bank_name" in request.data:
        row.bank_name = (request.data.get("bank_name") or "")[:100]
    if "account_number" in request.data:
        row.account_number = (request.data.get("account_number") or "")[:50]
    if "account_holder" in request.data:
        row.account_holder = (request.data.get("account_holder") or "")[:150]
    if "esewa_id" in request.data:
        row.esewa_id = (request.data.get("esewa_id") or "")[:20]
    if "khalti_id" in request.data:
        row.khalti_id = (request.data.get("khalti_id") or "")[:20]
    row.save()
    return Response({"ok": True})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_change_password(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    old_p = request.data.get("old_password") or ""
    new_p = request.data.get("new_password") or ""
    if not new_p or len(new_p) < 6:
        return validation_error("new_password must be at least 6 characters", field="new_password")
    u = request.user
    if not u.check_password(old_p):
        return Response({"detail": "Current password is incorrect."}, status=400)
    u.set_password(new_p)
    u.save(update_fields=["password"])
    return Response({"ok": True})


# --- Reels ---


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_reels_list_create(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if request.method == "GET":
        qs = (
            Reel.objects.filter(vendor=vendor)
            .select_related("product", "product__category")
            .annotate(comments_count=Count("comments"))
            .order_by("-created_at")
        )
        paginator, page = _paginate(request, qs)
        data = ReelPublicSerializer(page, many=True, context={"request": request}).data
        return paginator.get_paginated_response(data)
    video_url = (request.data.get("video_url") or "").strip()
    video_file = request.FILES.get("video")
    if video_file:
        from django.core.files.storage import default_storage

        path = default_storage.save(f"reels/uploads/{uuid4().hex}_{video_file.name[:80]}", video_file)
        video_url = request.build_absolute_uri(default_storage.url(path))
    if not video_url:
        return validation_error("video_url or video file required", field="video_url")
    platform = request.data.get("platform") or Reel.Platform.DIRECT_MP4
    if platform not in {c[0] for c in Reel.Platform.choices}:
        return validation_error("invalid platform", field="platform")
    product_id = request.data.get("product_id")
    product = None
    if product_id:
        product = Product.objects.filter(pk=product_id, seller=vendor).first()
    tags = parse_reel_tags(request.data.get("tags"))
    row = Reel.objects.create(
        vendor=vendor,
        video_url=video_url[:200],
        platform=platform,
        product=product,
        caption=(request.data.get("caption") or "")[:200],
        tags=tags,
        status=request.data.get("status") or Reel.Status.PENDING,
        is_sponsored=request.data.get("is_sponsored") in (True, "true", "1", 1),
    )
    if request.FILES.get("thumbnail"):
        row.thumbnail = request.FILES["thumbnail"]
        row.save(update_fields=["thumbnail"])
    return Response({"id": str(row.pk)}, status=201)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_reels_favourites(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = (
        Reel.objects.filter(
            vendor=vendor,
            interactions__user=request.user,
            interactions__type="bookmark",
        )
        .select_related("product", "product__category")
        .annotate(comments_count=Count("comments", distinct=True))
        .order_by("-created_at")
        .distinct()
    )
    paginator, page = _paginate(request, qs)
    data = ReelPublicSerializer(page, many=True, context={"request": request}).data
    return paginator.get_paginated_response(data)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_reel_detail(request, pk):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    row = Reel.objects.filter(pk=pk, vendor=vendor).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "video_url" in request.data:
        row.video_url = (request.data.get("video_url") or "").strip()[:200] or row.video_url
    if "platform" in request.data:
        pl = request.data.get("platform")
        if pl not in {c[0] for c in Reel.Platform.choices}:
            return validation_error("invalid platform", field="platform")
        row.platform = pl
    if "caption" in request.data:
        row.caption = (request.data.get("caption") or "")[:200]
    if "tags" in request.data:
        row.tags = parse_reel_tags(request.data.get("tags"))
    if "status" in request.data:
        row.status = request.data.get("status")
    if "product_id" in request.data:
        pid = request.data.get("product_id")
        row.product = Product.objects.filter(pk=pid, seller=vendor).first() if pid else None
    boost_err = apply_reel_boost_from_data(row, request.data)
    if boost_err:
        return validation_error(boost_err[0], field=boost_err[1])
    if request.FILES.get("thumbnail"):
        row.thumbnail = request.FILES["thumbnail"]
    row.save()
    return Response({"id": str(row.pk)})