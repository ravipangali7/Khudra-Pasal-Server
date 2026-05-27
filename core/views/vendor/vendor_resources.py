"""Vendor portal CRUD and extended read APIs."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.db.models import Count, Max, Prefetch, Sum
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
    OTPVerification,
    PayoutAccount,
    Attribute,
    AttributeValue,
    Brand,
    Category,
    FAQ,
    Order,
    OrderCommissionSettlement,
    OrderItem,
    Product,
    ProductApproval,
    ProductImage,
    ProductReview,
    Refund,
    Reel,
    SupportTicket,
    SupportTicketMessage,
    SupportTicketReaderState,
    Unit,
    User,
    VendorBankDetail,
    WalletWithdrawal,
)
from core.serializers import ReelPublicSerializer
from core.services.product_pricing import effective_unit_price, validate_and_set_product_discount
from core.services import (
    otp_service,
    product_service,
    support_notification_service,
    support_ticket_service,
    wallet_policy,
)
from core.services.user_presence import online_user_ids_for
from core.services.pos_order_service import create_pos_order, gen_pos_order_number as _gen_order_number
from core.services.site_settings_policy import (
    pos_checkout_allowed,
    pos_disabled_response,
    vendor_pos_checkout_allowed,
    vendor_pos_disabled_response,
)
from core.services.refund_service import breakdown_for_refund
from core.services.kyc_service import sync_user_kyc_status
from core.services.kyc_withdraw import kyc_withdraw_block_payload
from core.services.withdrawal_requests import create_pending_withdrawal, payout_required_block_payload
from core.services.reel_boost_patch import apply_reel_boost_from_data
from core.services.vendor_service import ensure_vendor_wallet
from core.views.admin.admin_write_utils import absolute_media_url, request_data_getlist, validation_error
from core.views.admin.resource_views import (
    _generate_unique_product_sku,
    _make_unique_slug,
    _product_sku_exists,
    _release_product_sku_for_reuse,
    _resolve_product_sku_for_create,
    _to_decimal,
)
from core.views.vendor.common import (
    get_or_create_pos_walkin_user,
    media_url,
    parse_reel_tags,
    vendor_or_error,
)
from core.views.vendor.vendor_views import VendorPagination

MAX_VENDOR_PRODUCT_GALLERY = 15


def _vendor_product_detail_payload(request, row: Product) -> dict:
    imgs = sorted(row.images.all(), key=lambda x: (x.sort_order, x.id))
    images_payload = [
        {
            "id": str(im.pk),
            "image_url": absolute_media_url(request, im.image) if im.image else "",
            "sort_order": im.sort_order,
        }
        for im in imgs
    ]
    return {
        "id": str(row.pk),
        "name": row.name,
        "slug": row.slug,
        "sku": row.sku,
        "description": row.description or "",
        "short_description": row.short_description or "",
        "price": float(row.price),
        "discount_type": row.discount_type or "",
        "discount": float(row.discount) if row.discount is not None else None,
        "tax_percent": float(row.tax_percent),
        "category_id": str(row.category_id),
        "category_name": row.category.name if row.category_id else "",
        "brand_id": str(row.brand_id) if row.brand_id else "",
        "brand_name": row.brand.name if row.brand_id else "",
        "unit_id": str(row.unit_id) if row.unit_id else "",
        "unit_name": row.unit.name if row.unit_id else "",
        "type": row.type,
        "stock": row.stock,
        "status": row.status,
        "is_featured": row.is_featured,
        "has_variations": row.has_variations,
        "enable_reels": row.enable_reels,
        "enable_pos": row.enable_pos,
        "seo_title": row.seo_title or "",
        "seo_description": row.seo_description or "",
        "seo_keywords": row.seo_keywords or "",
        "image_url": media_url(request, row.image),
        "images": images_payload,
        "attributes": row.attributes or {},
    }


def _paginate(request, queryset):
    paginator = VendorPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator, page


def _gen_ticket_number():
    return f"TKT-{uuid4().hex[:8].upper()}"


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
        from core import rbac_django as rbac

        u = request.user
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
                "groups": rbac.user_groups_payload(u),
                "permissions": rbac.user_permission_strings(u),
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


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_product_sku_preview(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    name = (request.query_params.get("name") or "").strip()
    sku = _generate_unique_product_sku(hint=name or None)
    return Response({"sku": sku})


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def vendor_product_detail(request, pk):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    row = (
        Product.objects.filter(pk=pk, seller=vendor)
        .select_related("category", "brand", "unit")
        .prefetch_related(Prefetch("images", queryset=ProductImage.objects.order_by("sort_order", "id")))
        .first()
    )
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(_vendor_product_detail_payload(request, row))
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
            _release_product_sku_for_reuse(row)
            row.save(
                update_fields=[
                    "status",
                    "stock",
                    "enable_pos",
                    "enable_reels",
                    "seller",
                    "sku",
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
    if "status" in request.data:
        st = (request.data.get("status") or "").strip()
        if st == Product.Status.ACTIVE:
            return validation_error(
                "Products cannot be set to active by vendors; publishing requires admin approval.",
                field="status",
            )
        if st in dict(Product.Status.choices):
            row.status = st
    for field in (
        "name",
        "description",
        "short_description",
        "seo_title",
        "seo_description",
        "seo_keywords",
    ):
        if field in request.data:
            setattr(row, field, request.data.get(field))
    if "sku" in request.data:
        new_sku = (request.data.get("sku") or "").strip()
        if not new_sku:
            return validation_error("SKU is required.", field="sku")
        if _product_sku_exists(new_sku, exclude_pk=row.pk):
            return validation_error("This SKU is already in use. Choose a different SKU.", field="sku")
        row.sku = new_sku
    if "slug" in request.data or "name" in request.data:
        row.slug = _make_unique_slug(
            Product, request.data.get("slug") or request.data.get("name") or row.name, instance_pk=row.pk
        )
    if "price" in request.data:
        row.price = _to_decimal(request.data.get("price"), "0")
    if "discount_type" in request.data or "discount" in request.data:
        try:
            validate_and_set_product_discount(
                row,
                discount_type_raw=request.data.get("discount_type"),
                discount_raw=request.data.get("discount"),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
    if "tax_percent" in request.data:
        row.tax_percent = _to_decimal(request.data.get("tax_percent"), "0")
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
    for bfield in ("has_variations", "enable_reels", "enable_pos"):
        if bfield in request.data:
            setattr(row, bfield, str(request.data.get(bfield)).lower() == "true")
    for raw_id in request_data_getlist(request.data, "delete_gallery_image_ids"):
        try:
            gid = int(raw_id)
        except (TypeError, ValueError):
            continue
        ProductImage.objects.filter(pk=gid, product_id=row.pk).delete()
    gallery_new = request.FILES.getlist("gallery_images")
    if gallery_new:
        current_count = ProductImage.objects.filter(product=row).count()
        remaining = max(0, MAX_VENDOR_PRODUCT_GALLERY - current_count)
        agg = ProductImage.objects.filter(product=row).aggregate(m=Max("sort_order"))
        start_order = (agg["m"] if agg["m"] is not None else -1) + 1
        for idx, f in enumerate(gallery_new[:remaining]):
            ProductImage.objects.create(product=row, image=f, sort_order=start_order + idx)
    image = request.FILES.get("image")
    if image:
        row.image = image
    row.save()
    product_service.sync_stock_status(row)
    return Response({"id": str(row.pk), "slug": row.slug})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def vendor_product_create(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    name = (request.data.get("name") or "").strip()
    category_id = request.data.get("category_id")
    image = request.FILES.get("image")
    if not name or not category_id or not image:
        return Response({"detail": "name, category_id and image are required"}, status=400)
    category = Category.objects.filter(pk=category_id).first()
    if not category:
        return Response({"detail": "invalid category_id"}, status=400)
    sku = _resolve_product_sku_for_create(request, name=name)
    if isinstance(sku, Response):
        return sku
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
        discount_type="",
        discount=None,
        tax_percent=_to_decimal(request.data.get("tax_percent"), "0"),
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
        is_featured=False,
        has_variations=str(request.data.get("has_variations", "")).lower() == "true",
        seo_title=request.data.get("seo_title") or "",
        seo_description=request.data.get("seo_description") or "",
        seo_keywords=request.data.get("seo_keywords") or "",
        enable_reels=str(request.data.get("enable_reels", "")).lower() == "true",
        enable_pos=str(request.data.get("enable_pos", "")).lower() == "true",
        attributes=attrs if isinstance(attrs, dict) else {},
    )
    if request.data.get("discount_type") or request.data.get("discount"):
        try:
            validate_and_set_product_discount(
                row,
                discount_type_raw=request.data.get("discount_type"),
                discount_raw=request.data.get("discount"),
            )
        except ValueError as e:
            row.delete()
            return Response({"detail": str(e)}, status=400)
        row.save(update_fields=["discount_type", "discount"])
    gallery_files = request.FILES.getlist("gallery_images")[:MAX_VENDOR_PRODUCT_GALLERY]
    for idx, f in enumerate(gallery_files):
        ProductImage.objects.create(product=row, image=f, sort_order=idx)
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
    rows = []
    for r in page:
        fee, net = breakdown_for_refund(r)
        rows.append(
            {
                "id": r.refund_number,
                "order": r.order.order_number,
                "amount": float(r.amount),
                "gross_amount": float(r.amount),
                "platform_fee": float(fee),
                "net_credit": float(net),
                "reason": r.reason,
                "status": r.status,
                "date": r.created_at.date().isoformat(),
                "customer": r.customer.name,
            }
        )
    return paginator.get_paginated_response(rows)


# --- POS ---


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_pos_checkout(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if not pos_checkout_allowed():
        return pos_disabled_response()
    if not vendor_pos_checkout_allowed(vendor):
        return vendor_pos_disabled_response()
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
    discount = _to_decimal(request.data.get("discount") or request.data.get("discount_amount"), "0")
    notes = (request.data.get("notes") or "")[:500]

    if payment_method == Order.PaymentMethod.CASH:
        purchase = _to_decimal(request.data.get("purchase_amount"), "0")
        collected = _to_decimal(request.data.get("collected_amount"), "0")
        change = _to_decimal(request.data.get("change_amount"), "0")
        if collected > 0 and purchase > 0 and collected < purchase:
            return validation_error(
                "collected amount is less than purchase amount",
                field="collected_amount",
            )
        if collected > 0 or change > 0:
            extra = f" | Collected: Rs.{collected:.2f} | Change: Rs.{change:.2f}"
            notes = (notes + extra)[:500]

    try:
        order = create_pos_order(
            acting_vendor=vendor,
            customer=customer,
            items=items,
            payment_method=payment_method,
            tax_percent=tax_percent,
            discount=discount,
            notes=notes,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)

    return Response({"order_number": order.order_number, "total": float(order.total)}, status=201)


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
    vu = vendor.user
    sync_user_kyc_status(vu)
    vu.refresh_from_db()
    block = kyc_withdraw_block_payload(vu)
    if block:
        return Response(block, status=403)
    pay_block = payout_required_block_payload(vu)
    if pay_block:
        return Response(pay_block, status=403)
    if not wallet_policy.vendor_wallet_operations_allowed():
        return Response(
            {
                "code": "vendor_wallet_disabled",
                "detail": "Vendor wallet withdrawals are disabled in site settings.",
            },
            status=403,
        )
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    if wallet_policy.withdrawal_requires_otp():
        code = (request.data.get("otp") or "").strip()
        if not code:
            return Response(
                {
                    "code": "otp_required",
                    "detail": "OTP is required for withdrawals.",
                },
                status=400,
            )
        phone = (vu.phone or "").strip()
        if not phone:
            return Response(
                {"detail": "No phone number on file for OTP verification."},
                status=400,
            )
        try:
            otp_service.consume(phone, OTPVerification.Purpose.WITHDRAW, code)
        except otp_service.OTPError as e:
            return Response({"detail": str(e)}, status=400)
    raw_pid = request.data.get("payout_account_id") or request.data.get("payout_account")
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return validation_error("payout_account_id required", field="payout_account_id")
    acct = PayoutAccount.objects.filter(pk=pid, user=vu).first()
    if not acct:
        return validation_error("Invalid payout account.", field="payout_account_id")
    try:
        wd = create_pending_withdrawal(
            wallet=wallet,
            payout_user=vu,
            payout_account=acct,
            amount=amount,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response({"id": wd.withdrawal_number}, status=201)


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

_PLACED_PORTAL_LABELS = dict(Order.PlacedPortal.choices)
_PAYMENT_METHOD_LABELS = dict(Order.PaymentMethod.choices)


def _vendor_reports_date_range(request):
    end_d = parse_date(request.query_params.get("to") or "") or timezone.localdate()
    start_d = parse_date(request.query_params.get("from") or "") or (end_d - timedelta(days=30))
    start_dt = timezone.make_aware(datetime.combine(start_d, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end_d, datetime.max.time()))
    return start_d, end_d, start_dt, end_dt


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_reports_summary(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    start_d, end_d, start_dt, end_dt = _vendor_reports_date_range(request)
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
    item_fq = OrderItem.objects.filter(
        order__seller=vendor,
        order__created_at__gte=start_dt,
        order__created_at__lte=end_dt,
    )
    cat_rows = item_fq.values("product__category__name").annotate(revenue=Sum("total_price"))
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

    order_count = oq.count()
    items_agg = item_fq.aggregate(t=Sum("quantity"))
    items_sold = int(items_agg["t"] or 0)
    avg_order_value = float(gross / order_count) if order_count else 0.0
    summary_counts = {
        "order_count": order_count,
        "avg_order_value": avg_order_value,
        "items_sold": items_sold,
    }

    by_placed_portal = []
    for r in oq.values("placed_portal").annotate(orders=Count("id"), revenue=Sum("total")):
        raw = r["placed_portal"]
        key = (raw or "").strip() or "legacy"
        label = _PLACED_PORTAL_LABELS.get(raw, "Legacy") if raw else "Legacy"
        by_placed_portal.append(
            {
                "key": key,
                "label": label,
                "orders": r["orders"],
                "revenue": float(r["revenue"] or 0),
            }
        )
    by_placed_portal.sort(key=lambda x: x["revenue"], reverse=True)

    by_channel = []
    for r in oq.values("is_pos_order").annotate(orders=Count("id"), revenue=Sum("total")):
        ch = "pos" if r["is_pos_order"] else "online"
        by_channel.append(
            {
                "channel": ch,
                "label": "POS" if r["is_pos_order"] else "Online store",
                "orders": r["orders"],
                "revenue": float(r["revenue"] or 0),
            }
        )
    by_channel.sort(key=lambda x: x["revenue"], reverse=True)

    by_payment_method = []
    for r in oq.values("payment_method").annotate(orders=Count("id"), revenue=Sum("total")):
        pm = r["payment_method"] or ""
        by_payment_method.append(
            {
                "method": pm,
                "label": _PAYMENT_METHOD_LABELS.get(pm, pm or "—"),
                "orders": r["orders"],
                "revenue": float(r["revenue"] or 0),
            }
        )
    by_payment_method.sort(key=lambda x: x["revenue"], reverse=True)

    top_products = [
        {
            "product_id": r["product_id"],
            "name": (r["product__name"] or "—")[:200],
            "quantity": int(r["qty"] or 0),
            "revenue": float(r["revenue"] or 0),
        }
        for r in item_fq.values("product_id", "product__name")
        .annotate(qty=Sum("quantity"), revenue=Sum("total_price"))
        .order_by("-revenue")[:10]
    ]

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
            "summary_counts": summary_counts,
            "by_placed_portal": by_placed_portal,
            "by_channel": by_channel,
            "by_payment_method": by_payment_method,
            "top_products": top_products,
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_reports_export_csv(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    start_d, end_d, start_dt, end_dt = _vendor_reports_date_range(request)
    oq = Order.objects.filter(seller=vendor, created_at__gte=start_dt, created_at__lte=end_dt).order_by(
        "-created_at"
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "order_number",
            "date",
            "customer",
            "status",
            "total",
            "payment_status",
            "placed_portal",
            "is_pos_order",
            "payment_method",
        ]
    )
    for o in oq.iterator(chunk_size=200):
        w.writerow(
            [
                o.order_number,
                o.created_at.isoformat(),
                o.customer.name,
                o.status,
                float(o.total),
                o.payment_status,
                (o.placed_portal or ""),
                "yes" if o.is_pos_order else "no",
                o.payment_method,
            ]
        )
    resp = HttpResponse(buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="vendor-orders-{start_d}-{end_d}.csv"'
    return resp


# --- Support tickets ---


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_support_super_admin_contact(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    u = support_ticket_service.primary_super_admin_user()
    if not u:
        return Response(
            {
                "name": "",
                "phone": "",
                "avatar_url": "",
                "is_online": False,
            }
        )
    sa_ids = support_ticket_service.super_admin_user_ids()
    online = online_user_ids_for(sa_ids)
    return Response(
        {
            "name": u.name or u.phone or "",
            "phone": u.phone or "",
            "avatar_url": absolute_media_url(request, u.avatar) or "",
            "is_online": u.pk in online,
        }
    )


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
        ticket_ids = [t.pk for t in page]
        states = {
            rs.ticket_id: rs
            for rs in SupportTicketReaderState.objects.filter(
                reader=request.user, ticket_id__in=ticket_ids
            )
        }
        rows = []
        for t in page:
            st = states.get(t.pk)
            rows.append(
                {
                    "id": t.ticket_number,
                    "subject": t.subject,
                    "status": t.status,
                    "priority": t.priority,
                    "category": t.category,
                    "source_panel": t.source_panel,
                    "created": t.created_at.date().isoformat(),
                    "last_activity": (t.last_activity_at or t.created_at).date().isoformat(),
                    "has_unread": support_ticket_service.ticket_has_unread_for_submitter(
                        t, state=st
                    ),
                }
            )
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
        support_ticket_service.mark_ticket_read(t, request.user)
        _av = lambda u: absolute_media_url(request, u.avatar)
        sa_ids = support_ticket_service.super_admin_user_ids()
        counterpart_online = bool(online_user_ids_for(sa_ids))
        msgs = support_ticket_service.serialize_ticket_messages(
            list(t.messages.all()),
            _vendor_support_attachment_url,
            ticket=t,
            sender_avatar_url_fn=_av,
            viewer_user_id=request.user.pk,
            viewer_is_staff=False,
            counterpart_online=counterpart_online,
        )
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
                "counterpart_online": counterpart_online,
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
        sa_ids = support_ticket_service.super_admin_user_ids()
        counterpart_online = bool(online_user_ids_for(sa_ids))
        read_at = support_ticket_service.get_counterpart_last_read_at(
            t, viewer_is_staff=False
        )
        results, has_more = support_ticket_service.messages_page_before(
            t,
            before_id,
            limit,
            _vendor_support_attachment_url,
            sender_avatar_url_fn=lambda u: absolute_media_url(request, u.avatar),
            viewer_user_id=request.user.pk,
            viewer_is_staff=False,
            counterpart_online=counterpart_online,
            counterpart_read_at=read_at,
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
    sa_ids = support_ticket_service.super_admin_user_ids()
    counterpart_online = bool(online_user_ids_for(sa_ids))
    read_at = support_ticket_service.get_counterpart_last_read_at(t, viewer_is_staff=False)
    tick = support_ticket_service.delivery_tick_for_message(
        msg,
        viewer_user_id=request.user.pk,
        viewer_is_staff=False,
        counterpart_online=counterpart_online,
        counterpart_last_read_at=read_at,
    )
    return Response(
        {
            "ok": True,
            "message": support_ticket_service.message_to_row(
                msg,
                _vendor_support_attachment_url,
                sender_avatar_url_fn=lambda u: absolute_media_url(request, u.avatar),
                delivery_ticks=tick,
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
        status=Reel.Status.PENDING,
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