"""Admin resource endpoints (list + write actions)."""

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
import re
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, FloatField, Max, Prefetch, Q, Sum
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import slugify
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import (
    Attribute,
    AttributeValue,
    AuditLog,
    Banner,
    BlogPost,
    Brand,
    Category,
    CMSPage,
    Coupon,
    DeliveryMan,
    EmployeeProfile,
    FamilyGroup,
    FamilyGroupPermission,
    FamilyMember,
    FlashDeal,
    FlashDealProduct,
    FlaggedActivity,
    KYCDocument,
    LoyaltyRule,
    LoyaltySettings,
    Notification,
    Order,
    OrderCommissionSettlement,
    OrderSettings,
    PaymentGatewaySettings,
    PaymentTransaction,
    Product,
    ProductImage,
    ProductApproval,
    ProductReview,
    PurchaseOrder,
    PurchaseOrderLine,
    Refund,
    Reel,
    Role,
    SecuritySettings,
    SiteSettings,
    ShippingMethod,
    ShippingSettings,
    ShippingZone,
    SupportTicket,
    SupportTicketMessage,
    SupportTicketReaderState,
    Unit,
    User,
    Vendor,
    VendorImpersonationLog,
    Wallet,
    WalletBonus,
    WalletSettings,
    WalletTransaction,
    WalletWithdrawal,
    WeightRule,
    PayoutAccount,
)
from core.serializers import ReelPublicSerializer
from core.services import (
    audit_service,
    po_service,
    product_service,
    refund_notification_service,
    refund_service,
    support_notification_service,
    support_ticket_service,
)
from core.services.product_pricing import effective_unit_price, validate_and_set_product_discount
from core.services.user_presence import online_user_ids_for
from core.services.shipping_quote import compute_shipping_fee
from core.services.base import new_wallet_txn_id
from core.services.reel_boost_patch import apply_reel_boost_from_data
from core.services.kyc_portal import supersede_non_approved_kyc, validate_kyc_upload_file
from core.services.kyc_service import sync_user_kyc_status
from core.services.pos_order_service import create_pos_order
from core.services.site_settings_policy import pos_checkout_allowed, pos_disabled_response
from core.services.vendor_service import ensure_vendor_wallet
from core.services.withdrawal_notifications import (
    notify_withdrawal_approved,
    notify_withdrawal_rejected,
)
from core.views.admin.admin_access import (
    enforce_admin_api_access,
    enforce_audit_log_access,
    user_can_manage_wallet_freeze,
)
from core.views.vendor.common import get_or_create_pos_walkin_user
from core.views.admin.admin_write_utils import (
    absolute_media_url,
    client_ip_from_request,
    parse_int_pk,
    product_primary_image_url,
    resolve_user_by_pk_or_phone,
    scalar_request_value,
    validation_error,
)


def _forbidden(request):
    return enforce_admin_api_access(request)


def _admin_support_attachment_url(att_id: int) -> str:
    return f"/admin/tickets/attachments/{att_id}/"


def _audit_datetime_param(raw: str, *, end_of_day: bool = False):
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    dt = parse_datetime(s)
    if dt is None:
        d = parse_date(s)
        if d is None:
            return None
        t = datetime.max.time().replace(microsecond=0) if end_of_day else datetime.min.time()
        dt = datetime.combine(d, t)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _audit_metadata_preview(meta, max_len: int = 200) -> str:
    if not meta:
        return ""
    try:
        s = json.dumps(meta, default=str)
    except TypeError:
        s = str(meta)
    return (s[:max_len] + "…") if len(s) > max_len else s


class AdminPagination(PageNumberPagination):
    page_size = 30
    page_size_query_param = "page_size"
    max_page_size = 500


def _paginate(request, queryset):
    paginator = AdminPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator, page


def _format_rs(amount) -> str:
    try:
        d = Decimal(str(amount))
    except Exception:
        d = Decimal("0")
    return f"Rs. {d:.2f}"


def _explain_shipping_breakdown(
    *,
    breakdown: list,
    fee: Decimal,
    zone: ShippingZone,
    weight: float,
    order_total: Decimal,
    method: ShippingMethod | None,
) -> list[dict]:
    """Human-readable lines for super-admin shipping calculator (parallel to machine `breakdown`)."""
    zn = zone.name or "this zone"
    explained: list[dict] = []
    for b in breakdown:
        step = b.get("step")
        if step == "global_free":
            explained.append(
                {
                    "step": step,
                    "title": "Global free shipping",
                    "lines": [
                        "Store-wide free shipping is enabled in shipping settings.",
                        f"Order total is {_format_rs(order_total)} (greater than zero), so shipping is {_format_rs(0)}.",
                    ],
                }
            )
        elif step == "method_free_threshold":
            thr = b.get("threshold")
            explained.append(
                {
                    "step": step,
                    "title": "Shipping method: free threshold met",
                    "lines": [
                        f"Order total {_format_rs(order_total)} is at or above the method’s free-shipping threshold of {_format_rs(thr)}.",
                        f"Shipping charge: {_format_rs(0)}.",
                    ],
                }
            )
        elif step == "method_free_not_met_flat":
            amt = b.get("amount", 0)
            explained.append(
                {
                    "step": step,
                    "title": "Shipping method: threshold not met",
                    "lines": [
                        f"Order total {_format_rs(order_total)} is below the method’s free-shipping threshold.",
                        f"A flat fallback charge applies: {_format_rs(amt)}.",
                    ],
                }
            )
        elif step == "flat":
            amt = b.get("amount", 0)
            explained.append(
                {
                    "step": step,
                    "title": "Shipping method: flat rate",
                    "lines": [
                        f"Selected method “{method.name if method else 'method'}” uses a flat rate of {_format_rs(amt)}.",
                    ],
                }
            )
        elif step == "pickup":
            explained.append(
                {
                    "step": step,
                    "title": "Local pickup",
                    "lines": [
                        f"Pickup is selected; delivery shipping is {_format_rs(0)}.",
                    ],
                }
            )
        elif step == "zone_flat":
            amt = b.get("amount", 0)
            explained.append(
                {
                    "step": step,
                    "title": f"Zone base charge ({zn})",
                    "lines": [
                        f"Base flat component for zone “{zn}”: {_format_rs(amt)}.",
                    ],
                }
            )
        elif step == "weight_band":
            amt = b.get("amount", 0)
            wkg = b.get("weight_kg", weight)
            rpk = b.get("rate_per_kg")
            mn = b.get("min_weight")
            mx = b.get("max_weight")
            lines = []
            if rpk is not None:
                lines.append(
                    f"Weight charge: {wkg} kg × {_format_rs(rpk)} per kg = {_format_rs(amt)}."
                )
            else:
                lines.append(f"Weight-based component: {_format_rs(amt)}.")
            if mn is not None and mx is not None:
                lines.append(f"This uses the weight band from {mn} kg up to {mx} kg (inclusive).")
            explained.append({"step": step, "title": "Weight-based charge", "lines": lines})
        elif step == "zone_flat_no_weight_rule":
            amt = b.get("amount", 0)
            explained.append(
                {
                    "step": step,
                    "title": f"No matching weight rule ({zn})",
                    "lines": [
                        f"No weight rule covers {weight} kg for zone “{zn}”.",
                        f"Only the zone flat rate applies: {_format_rs(amt)}.",
                    ],
                }
            )
        elif step == "zone_flat_only":
            amt = b.get("amount", 0)
            explained.append(
                {
                    "step": step,
                    "title": f"Zone flat only ({zn})",
                    "lines": [
                        f"No weight rule includes {weight} kg for zone “{zn}”.",
                        f"Shipping is the zone flat rate only: {_format_rs(amt)} (no per-kg add-on).",
                    ],
                }
            )
        elif step == "zone_free_above":
            fa = b.get("free_above")
            sub = b.get("subtotal_before_free")
            lines = [
                f"Zone “{zn}” offers free shipping when the order total is at or above {_format_rs(fa)}.",
                f"Your order total is {_format_rs(order_total)}, so this rule applies.",
            ]
            if sub is not None:
                lines.append(
                    f"Subtotal from the steps above was {_format_rs(sub)}; that amount is waived. Final shipping: {_format_rs(0)}."
                )
            else:
                lines.append(f"Final shipping charge: {_format_rs(0)} (previous components superseded).")
            explained.append(
                {
                    "step": step,
                    "title": "Zone free shipping (order value)",
                    "lines": lines,
                }
            )
        elif step == "seller_pays":
            explained.append(
                {
                    "step": step,
                    "title": "Seller pays shipping",
                    "lines": [
                        f"The fee shown ({_format_rs(fee)}) is what the customer would see if they paid shipping; "
                        "in settings, the seller bears this cost.",
                    ],
                }
            )
        else:
            explained.append(
                {
                    "step": str(step),
                    "title": str(step),
                    "lines": [json.dumps(b, default=str)],
                }
            )
    return explained


def _merge_admin_extras(current: dict | None, patch: dict | None) -> dict:
    base = dict(current) if isinstance(current, dict) else {}
    if not isinstance(patch, dict):
        return base
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_orders_list(request):
    if err := _forbidden(request):
        return err
    qs = (
        Order.objects.select_related("customer", "seller", "delivery_address")
        .annotate(item_count=Count("items"))
        .order_by("-created_at")
    )
    status = request.query_params.get("status")
    if status:
        qs = qs.filter(status=status)
    df = request.query_params.get("date_from")
    dt = request.query_params.get("date_to")
    if df:
        d0 = parse_date(str(df).strip())
        if d0 is not None:
            qs = qs.filter(created_at__date__gte=d0)
    if dt:
        d1 = parse_date(str(dt).strip())
        if d1 is not None:
            qs = qs.filter(created_at__date__lte=d1)
    raw_v = request.query_params.get("vendor_id") or request.query_params.get("seller_id")
    if raw_v is not None and str(raw_v).strip() != "":
        try:
            qs = qs.filter(seller_id=int(raw_v))
        except (TypeError, ValueError):
            pass
    raw_c = request.query_params.get("category_id")
    if raw_c is not None and str(raw_c).strip() != "":
        try:
            qs = qs.filter(items__product__category_id=int(raw_c)).distinct()
        except (TypeError, ValueError):
            pass
    paginator, page = _paginate(request, qs)
    rows = []
    for o in page:
        addr = getattr(o, "delivery_address", None)
        rows.append(
            {
                "id": o.order_number,
                "pk": o.pk,
                "customer": o.customer.name,
                "phone": addr.mobile if addr else o.customer.phone,
                "total": float(o.total),
                "items": o.item_count,
                "status": o.status,
                "date": o.created_at.isoformat(),
                "payment": o.get_payment_method_display(),
                "seller": o.seller.store_name if o.seller_id else "In-House",
                "address": addr.area_location if addr else "",
            }
        )
    return paginator.get_paginated_response(rows)


_ORDER_STATUS_FLOW = [
    Order.Status.PENDING,
    Order.Status.PROCESSING,
    Order.Status.SHIPPED,
    Order.Status.DELIVERED,
]


def _order_status_transition_allowed(current: str, new: str) -> bool:
    if new == current:
        return True
    if new == Order.Status.CANCELLED:
        return current not in (
            Order.Status.CANCELLED,
            Order.Status.REFUNDED,
            Order.Status.DELIVERED,
        )
    if current in (Order.Status.CANCELLED, Order.Status.REFUNDED):
        return False
    if new == Order.Status.REFUNDED:
        return False
    try:
        i_curr = _ORDER_STATUS_FLOW.index(current)
        i_new = _ORDER_STATUS_FLOW.index(new)
    except ValueError:
        return False
    return i_new == i_curr + 1


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_order_detail(request, pk):
    if err := _forbidden(request):
        return err
    try:
        o = (
            Order.objects.select_related(
                "customer",
                "seller",
                "delivery_address",
                "coupon",
                "commission_settlement",
            )
            .prefetch_related("items__product")
            .get(pk=pk)
        )
    except Order.DoesNotExist:
        return Response({"detail": "Not found."}, status=404)

    if request.method == "PATCH":
        new_status = request.data.get("status")
        if new_status is not None:
            if new_status not in dict(Order.Status.choices):
                return validation_error("invalid status", field="status")
            if not _order_status_transition_allowed(o.status, new_status):
                return validation_error(
                    f"cannot change status from {o.status} to {new_status}",
                    field="status",
                )
            o.status = new_status
        if "payment_status" in request.data:
            ps = request.data.get("payment_status")
            if ps not in dict(Order.PaymentStatus.choices):
                return validation_error("invalid payment_status", field="payment_status")
            if ps == Order.PaymentStatus.PAID:
                if o.payment_status != Order.PaymentStatus.PENDING:
                    return validation_error(
                        "can only mark paid from pending",
                        field="payment_status",
                    )
                if o.status in (Order.Status.CANCELLED, Order.Status.REFUNDED):
                    return validation_error(
                        "cannot mark paid for cancelled or refunded order",
                        field="payment_status",
                    )
            o.payment_status = ps
        if "tracking_number" in request.data:
            o.tracking_number = (request.data.get("tracking_number") or "")[:100]
        if "carrier" in request.data:
            o.carrier = (request.data.get("carrier") or "")[:100]
        o.save()
        o.refresh_from_db()

    addr = getattr(o, "delivery_address", None)
    items = [
        {
            "name": it.product.name,
            "sku": it.product.sku,
            "qty": it.quantity,
            "unit_price": float(it.unit_price),
            "total": float(it.total_price),
            "image_url": absolute_media_url(request, it.product.image),
        }
        for it in o.items.all()
    ]
    refunded_sum = (
        Refund.objects.filter(
            order=o,
            status=Refund.Status.APPROVED,
        ).aggregate(s=Sum("amount"))["s"]
        or Decimal("0")
    )
    remaining = max(Decimal("0"), Decimal(o.total) - refunded_sum)
    refund_preview = None
    if (
        remaining > 0
        and o.payment_method == Order.PaymentMethod.WALLET
        and o.payment_status == Order.PaymentStatus.PAID
        and o.status not in (Order.Status.CANCELLED, Order.Status.REFUNDED)
    ):
        try:
            rfin = refund_service.refund_financials(
                o, remaining, persist_settlement=False
            )
            refund_preview = {
                "gross": float(remaining),
                "platform_fee": float(rfin.fee_retained),
                "net_credit": float(rfin.customer_credit),
                "platform_retention_label": refund_service.commission_slice_retention_short_label(),
            }
        except ValueError:
            refund_preview = None

    cs = getattr(o, "commission_settlement", None)
    commission_settlement = None
    if cs:
        commission_settlement = {
            "total_amount": float(cs.total_amount),
            "vendor_amount": float(cs.vendor_amount),
            "commission_amount": float(cs.commission_amount),
            "commission_percent": float(cs.commission_percent),
        }

    return Response(
        {
            "id": o.order_number,
            "pk": o.pk,
            "customer": o.customer.name,
            "phone": addr.mobile if addr else o.customer.phone,
            "payment": o.get_payment_method_display(),
            "seller": o.seller.store_name if o.seller_id else "In-House",
            "address": addr.area_location if addr else "",
            "full_address": (
                ", ".join(
                    x for x in (addr.area_location, addr.landmark) if (x or "").strip()
                )
                if addr
                else ""
            ),
            "date": o.created_at.isoformat(),
            "items": len(items),
            "item_lines": items,
            "total": float(o.total),
            "subtotal": float(o.subtotal),
            "delivery_fee": float(o.delivery_fee),
            "discount_amount": float(o.discount_amount),
            "status": o.status,
            "payment_status": o.payment_status,
            "tracking_number": o.tracking_number or "",
            "carrier": o.carrier or "",
            "refunded_total": float(refunded_sum),
            "refund_preview": refund_preview,
            "commission_settlement": commission_settlement,
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_order_refund(request, pk):
    if err := _forbidden(request):
        return err
    o = Order.objects.filter(pk=pk).first()
    if not o:
        return Response({"detail": "Not found."}, status=404)
    if o.payment_method != Order.PaymentMethod.WALLET:
        return validation_error("refunds are only supported for wallet-paid orders", field="order")
    if o.payment_status != Order.PaymentStatus.PAID:
        return validation_error("order must be paid before requesting a refund", field="order")
    if o.status in (Order.Status.CANCELLED, Order.Status.REFUNDED):
        return validation_error("order cannot be refunded", field="status")
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return validation_error("reason is required", field="reason")

    settings = OrderSettings.load()
    max_age = timedelta(days=int(settings.refund_validity_days or 0))
    if max_age and timezone.now() - o.created_at > max_age:
        return validation_error("refund period has expired", field="order")

    already = (
        Refund.objects.filter(order=o, status=Refund.Status.APPROVED).aggregate(
            s=Sum("amount")
        )["s"]
        or Decimal("0")
    )
    remaining = Decimal(o.total) - already
    if remaining <= Decimal("0"):
        return validation_error("nothing left to refund for this order", field="order")

    if Refund.objects.filter(order=o, status=Refund.Status.PENDING).exists():
        return validation_error(
            "a refund request is already pending for this order",
            field="order",
        )

    fin = refund_service.refund_financials(o, remaining, persist_settlement=True)
    refund_no = f"RF-{timezone.now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
    rf = Refund.objects.create(
        refund_number=refund_no,
        order=o,
        customer=o.customer,
        amount=remaining,
        platform_fee_amount=fin.fee_retained,
        net_credit_amount=fin.customer_credit,
        reason=reason[:4000],
        status=Refund.Status.PENDING,
    )
    refund_notification_service.notify_admins_new_refund_request(rf)
    return Response(
        {
            "ok": True,
            "refund_number": refund_no,
            "gross_amount": float(remaining),
            "platform_fee": float(fin.fee_retained),
            "net_credit": float(fin.customer_credit),
            "platform_retention_label": refund_service.commission_slice_retention_short_label(),
            "status": Refund.Status.PENDING,
            "message": "Pending Super Admin approval.",
        },
        status=201,
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_products_list(request):
    if err := _forbidden(request):
        return err
    qs = Product.objects.select_related("category", "brand", "seller").order_by("-created_at")

    def _parse_bool_query(value):
        if value is None:
            return None
        v = str(value).strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
        return None

    status = request.query_params.get("status")
    if status:
        qs = qs.filter(status=status)
    search = request.query_params.get("search")
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(sku__icontains=search))
    seller_id = request.query_params.get("seller_id") or request.query_params.get("vendor_id")
    if seller_id:
        qs = qs.filter(seller_id=seller_id)
    enable_pos = _parse_bool_query(request.query_params.get("enable_pos"))
    if enable_pos is not None:
        qs = qs.filter(enable_pos=enable_pos)
    enable_reels = _parse_bool_query(request.query_params.get("enable_reels"))
    if enable_reels is not None:
        qs = qs.filter(enable_reels=enable_reels)
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(p.pk),
            "name": p.name,
            "sku": p.sku,
            "category": p.category.name,
            "price": float(effective_unit_price(p)),
            "stock": p.stock,
            "status": p.status,
            "seller": p.seller.store_name if p.seller_id else "In-House",
            "type": p.type,
            "brand": p.brand.name if p.brand_id else "",
            "featured": p.is_featured,
            "image_url": absolute_media_url(request, p.image),
        }
        for p in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendors_list(request):
    if err := _forbidden(request):
        return err
    qs = Vendor.objects.select_related("user", "wallet").annotate(
        product_count=Count("products"),
        order_count=Count("orders"),
        revenue_sum=Sum("orders__total"),
    ).order_by("-created_at")
    search = request.query_params.get("search")
    if search:
        qs = qs.filter(
            Q(store_name__icontains=search)
            | Q(user__name__icontains=search)
            | Q(user__phone__icontains=search)
        )
    paginator, page = _paginate(request, qs)
    rows = []
    for v in page:
        bal = Decimal("0")
        try:
            bal = v.wallet.balance
        except Vendor.wallet.RelatedObjectDoesNotExist:
            pass
        rows.append(
            {
                "id": str(v.pk),
                "name": v.store_name,
                "owner": v.user.name,
                "products": v.product_count,
                "orders": v.order_count,
                "revenue": float(v.revenue_sum or 0),
                "status": v.status,
                "commission": float(v.commission_rate),
                "walletBalance": float(bal),
                "canPost": v.can_post,
                "canSell": v.can_sell,
                "posEnabled": v.pos_enabled,
                "phone": (v.phone or v.user.phone or "")[:15],
                "contact_email": (v.contact_email or "").strip(),
                "address": (v.address or "").strip(),
                "description": (v.description or "").strip(),
                "logo_url": absolute_media_url(request, v.logo),
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def admin_vendor_create(request):
    if err := _forbidden(request):
        return err
    store_name = (request.data.get("store_name") or "").strip()
    owner_name = (request.data.get("name") or "").strip()
    phone = (request.data.get("phone") or "").strip()
    if not store_name or not owner_name or not phone:
        return validation_error("store_name, name, and phone are required")
    if User.objects.filter(phone=phone).exists():
        return validation_error("phone already registered")
    status = (request.data.get("status") or "").strip() or Vendor.Status.PENDING
    if status not in dict(Vendor.Status.choices):
        return validation_error("invalid status")

    def _bool_field(key: str, default: bool) -> bool:
        if key not in request.data:
            return default
        v = request.data.get(key)
        return v in (True, "true", "1", 1, "True")

    kyc_type = (request.data.get("kyc_document_type") or "").strip()
    approve_kyc = _bool_field("kyc_approve", False) or _bool_field("approve_kyc", False)
    kyc_img = request.FILES.get("kyc_document_image") or request.FILES.get("document_image")
    kyc_pdf = request.FILES.get("kyc_document_file") or request.FILES.get("document_file")
    kyc_back = request.FILES.get("kyc_document_back") or request.FILES.get("document_back")

    if kyc_type and kyc_type not in {c[0] for c in KYCDocument.DocumentType.choices}:
        return validation_error("invalid kyc_document_type", field="kyc_document_type")
    if kyc_img or kyc_pdf or kyc_back:
        if not kyc_type:
            return validation_error(
                "kyc_document_type is required when uploading KYC files",
                field="kyc_document_type",
            )
        for f, name in ((kyc_img, "document_image"), (kyc_back, "document_back"), (kyc_pdf, "document_file")):
            if f:
                err = validate_kyc_upload_file(f, name)
                if err:
                    return err
        if not kyc_img and not kyc_pdf:
            return validation_error(
                "Provide kyc_document_image or kyc_document_file",
                field="kyc_document_image",
            )
    elif kyc_type and not (kyc_img or kyc_pdf) and not approve_kyc:
        return validation_error(
            "Provide kyc_document_image or kyc_document_file when kyc_document_type is set",
            field="kyc_document_image",
        )

    base_slug = slugify(store_name)[:180] or "vendor"
    store_slug = base_slug
    suffix = 0
    while Vendor.objects.filter(store_slug=store_slug).exists():
        suffix += 1
        store_slug = f"{base_slug}-{suffix}"

    commission_rate = _to_decimal(request.data.get("commission_rate"), "10")
    can_post = _bool_field("can_post", True)
    can_sell = _bool_field("can_sell", True)
    pos_enabled = _bool_field("pos_enabled", True)

    with transaction.atomic():
        user = User.objects.create_user(
            username=phone,
            email=(request.data.get("email") or "").strip(),
            password=request.data.get("password") or None,
            name=owner_name,
            phone=phone,
            role=User.Role.NORMAL,
        )
        if not request.data.get("password"):
            user.set_unusable_password()
            user.save(update_fields=["password"])

        vendor = Vendor(
            user=user,
            store_name=store_name,
            store_slug=store_slug,
            description=(request.data.get("description") or "").strip(),
            contact_email=(request.data.get("contact_email") or "").strip(),
            phone=phone[:15],
            address=(request.data.get("address") or "").strip(),
            commission_rate=commission_rate,
            can_post=can_post,
            can_sell=can_sell,
            pos_enabled=pos_enabled,
            status=status,
        )
        if request.FILES.get("logo"):
            vendor.logo = request.FILES["logo"]
        if request.FILES.get("banner"):
            vendor.banner = request.FILES["banner"]
        vendor.save()
        if status == Vendor.Status.APPROVED:
            ensure_vendor_wallet(vendor)

        if approve_kyc and not kyc_img and not kyc_pdf:
            User.objects.filter(pk=user.pk).update(kyc_status=User.KYCStatus.VERIFIED)
        elif kyc_img or kyc_pdf:
            id_num = (request.data.get("kyc_document_id_number") or "").strip()[:100]
            supersede_non_approved_kyc(user, kyc_type)
            kyc_row = KYCDocument(
                user=user,
                document_type=kyc_type,
                status=KYCDocument.Status.APPROVED if approve_kyc else KYCDocument.Status.PENDING,
                document_id_number=id_num,
            )
            if kyc_img:
                kyc_row.document_image = kyc_img
            if kyc_pdf:
                kyc_row.document_file = kyc_pdf
            if kyc_back:
                kyc_row.document_back = kyc_back
            if approve_kyc:
                kyc_row.reviewer = request.user
                kyc_row.reviewed_at = timezone.now()
            kyc_row.save()
            sync_user_kyc_status(user)
            if approve_kyc:
                User.objects.filter(pk=user.pk).update(kyc_status=User.KYCStatus.VERIFIED)

    return Response({"id": str(vendor.pk)}, status=201)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_reels_list(request):
    if err := _forbidden(request):
        return err
    qs = Reel.objects.select_related("vendor", "product", "product__category").order_by("-created_at")
    status = request.query_params.get("status")
    if status:
        qs = qs.filter(status=status)
    vendor_id = request.query_params.get("vendor_id")
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    paginator, page = _paginate(request, qs)
    data = ReelPublicSerializer(page, many=True, context={"request": request}).data
    return paginator.get_paginated_response(data)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_categories_flat(request):
    if err := _forbidden(request):
        return err
    qs = Category.objects.select_related("parent").annotate(pc=Count("product")).order_by("sort_order", "name")
    search = request.query_params.get("search")
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(slug__icontains=search))
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(c.pk),
            "name": c.name,
            "slug": c.slug,
            "products": c.pc,
            "status": c.status,
            "parent": c.parent.name if c.parent_id else "-",
            "parentId": str(c.parent_id) if c.parent_id else None,
            "level": c.level,
            "sortOrder": c.sort_order,
            "image": c.image.url if c.image else "",
            "seoTitle": c.seo_title,
            "seoDesc": c.seo_description,
        }
        for c in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_brands_list(request):
    if err := _forbidden(request):
        return err
    qs = Brand.objects.annotate(pc=Count("product")).order_by("name")
    search = request.query_params.get("search")
    if search:
        qs = qs.filter(name__icontains=search)
    paginator, page = _paginate(request, qs)
    rows = []
    for b in page:
        logo_url = ""
        if b.logo:
            logo_url = b.logo.url
            if request and logo_url.startswith("/"):
                logo_url = request.build_absolute_uri(logo_url)
        rows.append(
            {
                "id": str(b.pk),
                "name": b.name,
                "logo": logo_url,
                "products": b.pc,
                "status": b.status,
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_attributes_list(request):
    if err := _forbidden(request):
        return err
    qs = Attribute.objects.annotate(vc=Count("values")).order_by("name")
    search = request.query_params.get("search")
    if search:
        qs = qs.filter(name__icontains=search)
    paginator, page = _paginate(request, qs)
    rows = [
        {"id": str(a.pk), "name": a.name, "type": a.type, "values": a.vc, "status": a.status}
        for a in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_attribute_values_list(request):
    if err := _forbidden(request):
        return err
    qs = AttributeValue.objects.select_related("attribute").order_by("attribute_id", "sort_order", "value")
    attr_id = request.query_params.get("attribute")
    if attr_id:
        qs = qs.filter(attribute_id=attr_id)
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(v.pk),
            "value": v.value,
            "sortOrder": v.sort_order,
            "status": v.status,
            "attribute_id": str(v.attribute_id),
        }
        for v in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_units_list(request):
    if err := _forbidden(request):
        return err
    qs = Unit.objects.order_by("name")
    search = request.query_params.get("search")
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(short_name__icontains=search))
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(u.pk),
            "name": u.name,
            "shortName": u.short_name,
            "type": u.type,
            "conversion": u.conversion or "-",
            "status": u.status,
        }
        for u in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_reviews_list(request):
    if err := _forbidden(request):
        return err
    qs = ProductReview.objects.select_related("product", "customer").order_by("-created_at")
    st = request.query_params.get("status")
    if st in {c[0] for c in ProductReview.Status.choices}:
        qs = qs.filter(status=st)
    search = (request.query_params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(product__name__icontains=search)
            | Q(customer__name__icontains=search)
            | Q(comment__icontains=search)
        )
    df = request.query_params.get("date_from")
    if df:
        b = _audit_datetime_param(df, end_of_day=False)
        if b:
            qs = qs.filter(created_at__gte=b)
    dto = request.query_params.get("date_to")
    if dto:
        b = _audit_datetime_param(dto, end_of_day=True)
        if b:
            qs = qs.filter(created_at__lte=b)
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(r.pk),
            "product": r.product.name,
            "product_id": str(r.product_id),
            "customer": r.customer.name,
            "customer_id": str(r.customer_id),
            "rating": r.rating,
            "comment": r.comment,
            "date": r.created_at.date().isoformat(),
            "replied": bool(r.reply_text),
            "status": r.status,
        }
        for r in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_review_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = ProductReview.objects.filter(pk=pk).select_related("product").first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        product = row.product
        row.delete()
        product_service.refresh_product_rating(product)
        audit_service.log(
            "Product review deleted",
            log_type=AuditLog.Type.PRODUCT,
            performed_by=request.user,
            object_type="ProductReview",
            object_id=str(pk),
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="reviews",
            metadata={"product_id": str(product.pk)},
        )
        return Response({"ok": True})
    old_status = row.status
    if "status" in request.data:
        new_st = request.data.get("status")
        if new_st not in {c[0] for c in ProductReview.Status.choices}:
            return validation_error("invalid status", field="status")
        row.status = new_st
    if "reply_text" in request.data:
        row.reply_text = (request.data.get("reply_text") or "").strip()[:5000]
        if row.reply_text:
            row.replied_at = timezone.now()
    row.save()
    if row.status != old_status:
        product_service.refresh_product_rating(row.product)
    audit_service.log(
        f"Product review updated (status {row.status})",
        log_type=AuditLog.Type.PRODUCT,
        performed_by=request.user,
        object_type="ProductReview",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="reviews",
        metadata={"product_id": str(row.product_id), "status": row.status},
    )
    return Response({"id": str(row.pk), "status": row.status})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_product_approvals_list(request):
    if err := _forbidden(request):
        return err
    qs = ProductApproval.objects.select_related(
        "product", "product__category", "vendor"
    ).order_by("-submitted_at")
    st = request.query_params.get("status")
    if st in {c[0] for c in ProductApproval.Status.choices}:
        qs = qs.filter(status=st)
    paginator, page = _paginate(request, qs)
    rows = []
    for a in page:
        cat = getattr(a.product, "category", None)
        prod = a.product
        rows.append(
            {
                "id": str(a.pk),
                "product": prod.name,
                "product_id": str(prod.pk),
                "sku": prod.sku,
                "image_url": absolute_media_url(request, prod.image) if prod.image else "",
                "vendor": a.vendor.store_name,
                "vendor_id": str(a.vendor_id),
                "type": a.type,
                "status": a.status,
                "submitted": a.submitted_at.isoformat(),
                "category": cat.name if cat else "—",
                "price": float(prod.price),
                "rejection_reason": (a.rejection_reason or "")[:500],
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_coupons_list(request):
    if err := _forbidden(request):
        return err
    qs = (
        Coupon.objects.select_related("vendor", "category")
        .prefetch_related("products")
        .order_by("-created_at")
    )
    paginator, page = _paginate(request, qs)
    rows = []
    for c in page:
        deal_prods = list(c.products.all())
        pids = [str(p.pk) for p in deal_prods]
        preview_cap = 5
        products_preview = []
        for p in deal_prods[:preview_cap]:
            products_preview.append(
                {
                    "id": str(p.pk),
                    "name": p.name,
                    "price": float(p.price),
                    "image_url": absolute_media_url(request, p.image) if p.image else "",
                }
            )
        rows.append(
            {
                "id": str(c.pk),
                "code": c.code,
                "type": c.type,
                "value": float(c.value),
                "minOrder": float(c.min_order),
                "used": c.used_count,
                "limit": c.usage_limit,
                "status": c.status,
                "expires": c.expires_at.isoformat() if c.expires_at else "",
                "vendor": c.vendor.store_name if c.vendor_id else "All",
                "vendor_id": str(c.vendor_id) if c.vendor_id else "",
                "category": c.category.name if c.category_id else "All",
                "category_id": str(c.category_id) if c.category_id else "",
                "products": len(deal_prods),
                "product_ids": pids,
                "products_preview": products_preview,
                "products_preview_more": max(0, len(deal_prods) - preview_cap),
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_flash_deals_list(request):
    if err := _forbidden(request):
        return err
    dp_qs = FlashDealProduct.objects.select_related("product").order_by("id")
    qs = (
        FlashDeal.objects.annotate(pc=Count("deal_products"))
        .prefetch_related(Prefetch("deal_products", queryset=dp_qs))
        .order_by("-priority", "-start_at")
    )
    paginator, page = _paginate(request, qs)
    rows = []
    for d in page:
        deal_prods = list(d.deal_products.all())
        pids = [str(dp.product_id) for dp in deal_prods]
        preview_cap = 5
        products_preview = []
        for dp in deal_prods[:preview_cap]:
            p = dp.product
            price = dp.override_price if dp.override_price is not None else effective_unit_price(p)
            products_preview.append(
                {
                    "id": str(p.pk),
                    "name": p.name,
                    "price": float(price),
                    "image_url": absolute_media_url(request, p.image) if p.image else "",
                }
            )
        rows.append(
            {
                "id": str(d.pk),
                "name": d.name,
                "products": d.pc,
                "product_ids": pids,
                "products_preview": products_preview,
                "products_preview_more": max(0, len(deal_prods) - preview_cap),
                "discount": float(d.discount_percent),
                "startDate": d.start_at.isoformat(),
                "endDate": d.end_at.isoformat(),
                "status": d.status,
                "priority": d.priority,
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_banners_list(request):
    if err := _forbidden(request):
        return err
    qs = Banner.objects.select_related("category").order_by("sort_order", "-created_at")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(b.pk),
            "title": b.title,
            "subtitle": b.subtitle,
            "placement": b.placement,
            "image": absolute_media_url(request, b.image) if b.image else "",
            "gradient": b.gradient,
            "clickUrl": b.click_url,
            "startDate": b.start_date.isoformat() if b.start_date else "",
            "endDate": b.end_date.isoformat() if b.end_date else "",
            "status": b.status,
            "clicks": b.click_count,
            "sortOrder": b.sort_order,
            "cardVariant": b.card_variant,
            "ctaText": b.cta_text,
            "badgeText": b.badge_text,
        }
        for b in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_cms_pages_list(request):
    if err := _forbidden(request):
        return err
    qs = CMSPage.objects.order_by("-last_updated")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(p.pk),
            "title": p.title,
            "slug": p.slug,
            "status": p.status,
            "lastUpdated": p.last_updated.date().isoformat(),
            "seoTitle": p.seo_title,
            "seoDesc": p.seo_description,
            "imageUrl": absolute_media_url(request, p.featured_image) if p.featured_image else "",
        }
        for p in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_notifications_list(request):
    if err := _forbidden(request):
        return err
    qs = Notification.objects.select_related("recipient").order_by("-created_at")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(n.pk),
            "title": n.title,
            "message": n.message,
            "type": n.type,
            "target": n.target,
            "read": n.is_read,
            "created": n.created_at.isoformat(),
        }
        for n in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_me_notifications_list(request):
    """In-app notifications addressed to the signed-in admin user."""
    if err := _forbidden(request):
        return err
    u = request.user
    qs = Notification.objects.filter(recipient=u).order_by("-created_at")[:50]
    rows = []
    for n in qs:
        body = (n.message or "")[:500]
        rows.append(
            {
                "id": str(n.pk),
                "type": n.type,
                "title": n.title,
                "message": body,
                "time": n.created_at.isoformat(),
                "created_at": n.created_at.isoformat(),
                "is_read": bool(n.is_read),
                "action_url": n.action_url or "",
                "urgent": n.type == Notification.Type.SECURITY,
                "preview": f"{n.title}: {body}"[:200],
            }
        )
    return Response(rows)


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_me_notifications_mark_read(request):
    if err := _forbidden(request):
        return err
    u = request.user
    data = request.data if isinstance(request.data, Mapping) else {}
    mark_all = data.get("all")
    if mark_all in (True, "true", "1", 1):
        updated = Notification.objects.filter(recipient=u, is_read=False).update(is_read=True)
        return Response({"ok": True, "updated": updated})
    raw_ids = data.get("ids")
    if raw_ids is None:
        return Response({"detail": "Provide all: true or ids: [...]"}, status=400)
    if not isinstance(raw_ids, list):
        return Response({"detail": "ids must be a list"}, status=400)
    pks = []
    for x in raw_ids:
        try:
            pks.append(int(x))
        except (TypeError, ValueError):
            continue
    if not pks:
        return Response({"ok": True, "updated": 0})
    updated = Notification.objects.filter(recipient=u, pk__in=pks, is_read=False).update(
        is_read=True
    )
    return Response({"ok": True, "updated": updated})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_refunds_list(request):
    if err := _forbidden(request):
        return err
    qs = Refund.objects.select_related(
        "order", "customer", "order__commission_settlement"
    ).order_by("-created_at")
    paginator, page = _paginate(request, qs)
    rows = []
    for r in page:
        fee, net = refund_service.breakdown_for_refund(r)
        has_settlement = bool(
            getattr(r.order, "commission_settlement", None)
            and r.order.seller_id
        )
        rows.append(
            {
                "id": r.refund_number,
                "order": r.order.order_number,
                "order_pk": r.order_id,
                "customer": r.customer.name,
                "customer_phone": getattr(r.customer, "phone", "") or "",
                "customer_avatar": (
                    absolute_media_url(request, r.customer.avatar)
                    if getattr(r.customer, "avatar", None)
                    else ""
                ),
                "placed_portal": r.order.placed_portal or "",
                "amount": float(r.amount),
                "gross_amount": float(r.amount),
                "platform_fee": float(fee),
                "net_credit": float(net),
                "platform_retention_label": refund_service.commission_slice_retention_short_label(),
                "deduction_summary": refund_service.commission_slice_refund_deduction_summary(
                    has_vendor_settlement=has_settlement
                ),
                "reason": r.reason,
                "status": r.status,
                "date": r.created_at.date().isoformat(),
                "created_at": r.created_at.isoformat(),
                "processed_at": r.processed_at.isoformat() if r.processed_at else None,
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_refund_detail_write(request, refund_number: str):
    if err := enforce_audit_log_access(request):
        return err
    rf = (
        Refund.objects.filter(refund_number=refund_number)
        .select_related("order", "customer")
        .first()
    )
    if not rf:
        return Response({"detail": "Not found."}, status=404)
    body = request.data if isinstance(request.data, Mapping) else {}
    new_status = (body.get("status") or "").strip().lower()
    if new_status not in ("approved", "rejected"):
        return validation_error('status must be "approved" or "rejected"', field="status")
    if rf.status != Refund.Status.PENDING:
        return validation_error("Only pending refunds can be updated.", field="status")

    admin_note = (body.get("admin_note") or "").strip()
    if new_status == "rejected" and not admin_note:
        return validation_error("admin_note is required when rejecting.", field="admin_note")

    reject_rid = rf.pk

    if new_status == "rejected":
        with transaction.atomic():
            locked = Refund.objects.select_for_update().filter(pk=reject_rid).first()
            if not locked or locked.status != Refund.Status.PENDING:
                return validation_error("Only pending refunds can be updated.", field="status")
            locked.status = Refund.Status.REJECTED
            locked.admin_note = admin_note[:4000]
            locked.processed_at = timezone.now()
            locked.save(update_fields=["status", "admin_note", "processed_at"])

            def _notify_reject():
                r = Refund.objects.get(pk=reject_rid)
                refund_notification_service.notify_customer_refund_status(r, approved=False)

            transaction.on_commit(_notify_reject)
        return Response({"ok": True, "status": Refund.Status.REJECTED})

    try:
        with transaction.atomic():
            locked = (
                Refund.objects.select_for_update()
                .filter(pk=reject_rid)
                .select_related("order", "customer")
                .first()
            )
            if not locked or locked.status != Refund.Status.PENDING:
                return validation_error("Only pending refunds can be updated.", field="status")
            locked.status = Refund.Status.APPROVED
            update_fields = ["status"]
            if admin_note:
                locked.admin_note = admin_note[:4000]
                update_fields.append("admin_note")
            locked.save(update_fields=update_fields)
            refund_service.execute_refund(locked)

            def _notify_approve():
                r = Refund.objects.get(pk=reject_rid)
                refund_notification_service.notify_customer_refund_status(r, approved=True)
                refund_notification_service.notify_vendor_refund_processed(r)

            transaction.on_commit(_notify_approve)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)

    r_final = Refund.objects.get(pk=reject_rid)
    return Response(
        {
            "ok": True,
            "status": r_final.status,
            "processed_at": r_final.processed_at.isoformat() if r_final.processed_at else None,
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_payments_list(request):
    if err := _forbidden(request):
        return err
    qs = PaymentTransaction.objects.select_related("order", "customer").order_by("-created_at")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": t.txn_ref,
            "order": t.order.order_number if t.order_id else "-",
            "customer": t.customer.name,
            "amount": float(t.amount),
            "method": t.get_method_display(),
            "status": t.status,
            "date": t.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for t in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_commission_settlements_list(request):
    if err := _forbidden(request):
        return err
    qs = OrderCommissionSettlement.objects.select_related(
        "order", "vendor", "platform_wallet_txn", "vendor_wallet_txn"
    ).order_by("-created_at")
    vendor_id = request.query_params.get("vendor_id")
    if vendor_id and str(vendor_id).strip().isdigit():
        qs = qs.filter(vendor_id=int(vendor_id))
    order_q = (request.query_params.get("order_number") or "").strip()
    if order_q:
        qs = qs.filter(order__order_number__icontains=order_q)
    df = request.query_params.get("date_from")
    if df:
        b = _audit_datetime_param(df, end_of_day=False)
        if b:
            qs = qs.filter(created_at__gte=b)
    dto = request.query_params.get("date_to")
    if dto:
        b = _audit_datetime_param(dto, end_of_day=True)
        if b:
            qs = qs.filter(created_at__lte=b)
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(s.pk),
            "order_id": s.order_id,
            "order_number": s.order.order_number,
            "vendor_id": s.vendor_id,
            "vendor_name": s.vendor.store_name,
            "total_amount": float(s.total_amount),
            "commission_percent": float(s.commission_percent),
            "commission_amount": float(s.commission_amount),
            "vendor_amount": float(s.vendor_amount),
            "payment_status": s.payment_status,
            "created_at": s.created_at.isoformat(),
        }
        for s in page
    ]
    return paginator.get_paginated_response(rows)


def _ledger_wallet_portal(wallet: Wallet) -> str:
    if wallet.type == Wallet.Type.PLATFORM:
        return "platform"
    if wallet.vendor_id:
        return "vendor"
    if wallet.family_group_id:
        return "family"
    if wallet.type == Wallet.Type.CHILD:
        return "child"
    if wallet.type == Wallet.Type.PARENT:
        return "parent"
    return "customer"


def _wallet_txn_signed_amount(t: WalletTransaction) -> float:
    """Sign for admin ledger display: outflows negative, inflows positive."""
    amt = float(t.amount)
    typ = t.type
    if typ in (
        WalletTransaction.Type.DEBIT,
        WalletTransaction.Type.WITHDRAWAL,
        WalletTransaction.Type.PURCHASE,
        WalletTransaction.Type.REFUND_VENDOR_DEBIT,
        WalletTransaction.Type.REFUND_PLATFORM_DEBIT,
    ):
        return -amt
    if typ == WalletTransaction.Type.REFUND_CREDIT:
        return amt
    if typ == WalletTransaction.Type.TRANSFER:
        wid = t.wallet_id
        if t.from_wallet_id and t.from_wallet_id == wid:
            return -amt
        if t.to_wallet_id and t.to_wallet_id == wid:
            return amt
        return amt
    return amt


def _ledger_payment_row(t: PaymentTransaction, request) -> dict:
    a = float(t.amount)
    return {
        "id": f"pay-{t.pk}",
        "source": "payment",
        "portal": "store",
        "type": "payment",
        "txn_type": t.get_method_display(),
        "user": t.customer.name,
        "user_id": str(t.customer_id),
        "amount": a,
        "signed_amount": a,
        "status": t.status,
        "reference": t.order.order_number if t.order_id else "-",
        "description": (t.txn_ref or "")[:120],
        "created_at": t.created_at.isoformat(),
        "_sort": t.created_at,
    }


def _ledger_commission_settlement_row(s: OrderCommissionSettlement, request) -> dict:
    """Synthetic row: platform commission taken (vendor perspective — negative signed_amount)."""
    amt = float(s.commission_amount)
    return {
        "id": f"cms-{s.pk}",
        "source": "commission_settlement",
        "portal": "vendor",
        "type": "commission_settlement",
        "txn_type": "Platform commission",
        "user": s.vendor.store_name,
        "user_id": str(s.vendor.user_id),
        "family": "—",
        "amount": amt,
        "signed_amount": -amt,
        "status": "completed",
        "reference": s.order.order_number,
        "description": (
            f"Platform commission ({s.commission_percent}%) — order {s.order.order_number}"
        )[:200],
        "created_at": s.created_at.isoformat(),
        "_sort": s.created_at,
    }


def _commission_settlements_for_ledger(
    request, search: str, wst: str | None, vendor_user_id: int | None = None
):
    """OrderCommissionSettlement queryset with same date bounds as ledger (vendor tab)."""
    qs = OrderCommissionSettlement.objects.select_related("order", "vendor")
    if vendor_user_id is not None:
        qs = qs.filter(vendor__user_id=vendor_user_id)
    df = request.query_params.get("date_from")
    if df:
        b = _audit_datetime_param(df, end_of_day=False)
        if b:
            qs = qs.filter(created_at__gte=b)
    dto = request.query_params.get("date_to")
    if dto:
        b = _audit_datetime_param(dto, end_of_day=True)
        if b:
            qs = qs.filter(created_at__lte=b)
    if search:
        qs = qs.filter(
            Q(order__order_number__icontains=search)
            | Q(vendor__store_name__icontains=search)
        )
    if wst:
        qs = qs.filter(vendor_wallet_txn__status=wst)
    return qs


def _ledger_wallet_row(t: WalletTransaction, request) -> dict:
    w = t.wallet
    label = "—"
    user_id = ""
    if w.type == Wallet.Type.PLATFORM:
        label = "Platform (commission)"
        user_id = ""
    elif w.vendor_id:
        label = w.vendor.store_name
        user_id = str(w.vendor.user_id) if w.vendor.user_id else ""
    elif w.owner_id:
        label = w.owner.name
        user_id = str(w.owner_id)
    fam = w.family_group.name if w.family_group_id else "—"
    amt = float(t.amount)
    return {
        "id": f"wal-{t.txn_id}",
        "source": "wallet",
        "portal": _ledger_wallet_portal(w),
        "type": "wallet",
        "txn_type": t.get_type_display(),
        "user": label,
        "user_id": user_id,
        "family": fam,
        "amount": amt,
        "signed_amount": _wallet_txn_signed_amount(t),
        "status": t.status,
        "reference": t.reference_id or "-",
        "description": (t.description or "")[:200],
        "created_at": t.created_at.isoformat(),
        "_sort": t.created_at,
    }


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_ledger_transactions_list(request):
    if err := _forbidden(request):
        return err
    source = (request.query_params.get("source") or "all").strip().lower()
    pay_qs = PaymentTransaction.objects.select_related("order", "customer")
    wal_qs = WalletTransaction.objects.select_related(
        "wallet",
        "wallet__owner",
        "wallet__vendor",
        "wallet__family_group",
        "wallet__vendor__user",
        "from_wallet",
        "to_wallet",
    )

    df = request.query_params.get("date_from")
    if df:
        b = _audit_datetime_param(df, end_of_day=False)
        if b:
            pay_qs = pay_qs.filter(created_at__gte=b)
            wal_qs = wal_qs.filter(created_at__gte=b)
    dto = request.query_params.get("date_to")
    if dto:
        b = _audit_datetime_param(dto, end_of_day=True)
        if b:
            pay_qs = pay_qs.filter(created_at__lte=b)
            wal_qs = wal_qs.filter(created_at__lte=b)

    pst = request.query_params.get("payment_status")
    if pst:
        pay_qs = pay_qs.filter(status=pst)
    wst = request.query_params.get("wallet_status")
    if wst:
        wal_qs = wal_qs.filter(status=wst)
    wtype = request.query_params.get("wallet_type")
    if wtype:
        wal_qs = wal_qs.filter(type=wtype)

    search = (request.query_params.get("search") or "").strip()
    if search:
        pay_qs = pay_qs.filter(
            Q(customer__name__icontains=search)
            | Q(customer__phone__icontains=search)
            | Q(txn_ref__icontains=search)
            | Q(order__order_number__icontains=search)
        )
        wal_qs = wal_qs.filter(
            Q(description__icontains=search)
            | Q(txn_id__icontains=search)
            | Q(wallet__owner__name__icontains=search)
            | Q(wallet__vendor__store_name__icontains=search)
            | Q(wallet__family_group__name__icontains=search)
        )

    portal = (request.query_params.get("portal") or "").strip().lower()
    if portal and portal != "all":
        if portal == "store":
            wal_qs = wal_qs.none()
        elif portal == "vendor":
            pay_qs = pay_qs.none()
            wal_qs = wal_qs.filter(wallet__vendor_id__isnull=False)
        elif portal == "platform":
            pay_qs = pay_qs.none()
            wal_qs = wal_qs.filter(wallet__type=Wallet.Type.PLATFORM)
        elif portal in ("family", "child", "parent"):
            pay_qs = pay_qs.none()
            if portal == "family":
                wal_qs = wal_qs.filter(wallet__family_group_id__isnull=False)
            elif portal == "child":
                wal_qs = wal_qs.filter(wallet__type=Wallet.Type.CHILD)
            else:
                wal_qs = wal_qs.filter(wallet__type=Wallet.Type.PARENT)
        elif portal in ("customer_portal", "customer"):
            # Main customer portal: keep payment rows; personal wallets only (no vendor/family).
            wal_qs = wal_qs.filter(
                wallet__vendor_id__isnull=True,
                wallet__family_group_id__isnull=True,
                wallet__owner_id__isnull=False,
            )

    uid = request.query_params.get("user_id")
    uid_int: int | None = int(uid) if uid and str(uid).strip().isdigit() else None
    if uid_int is not None:
        pay_qs = pay_qs.filter(customer_id=uid_int)
        wal_qs = wal_qs.filter(
            Q(wallet__owner_id=uid_int) | Q(wallet__vendor__user_id=uid_int)
        )

    def _ledger_merge_paginate_in_memory(row_dicts: list) -> Response:
        row_dicts.sort(key=lambda r: r["_sort"], reverse=True)
        for r in row_dicts:
            r.pop("_sort", None)
        paginator = AdminPagination()
        page_size = min(
            paginator.max_page_size,
            int(request.query_params.get("page_size") or paginator.page_size),
        )
        page_num = max(1, int(request.query_params.get("page") or 1))
        p = Paginator(row_dicts, page_size)
        page_obj = p.get_page(page_num)
        return Response(
            {
                "count": p.count,
                "next": page_obj.next_page_number() if page_obj.has_next() else None,
                "previous": page_obj.previous_page_number() if page_obj.has_previous() else None,
                "results": list(page_obj.object_list),
            }
        )

    portal_is_vendor = portal == "vendor"

    if source == "payment":
        pay_qs = pay_qs.order_by("-created_at")
        paginator, page = _paginate(request, pay_qs)
        rows = [_ledger_payment_row(t, request) for t in page]
        for r in rows:
            r.pop("_sort", None)
        return paginator.get_paginated_response(rows)
    if source == "wallet":
        if portal_is_vendor:
            wal_list = list(wal_qs.order_by("-created_at")[:800])
            cms_qs = _commission_settlements_for_ledger(
                request, search, wst, vendor_user_id=uid_int
            ).order_by("-created_at")[:800]
            cms_list = list(cms_qs)
            combined = [_ledger_wallet_row(t, request) for t in wal_list] + [
                _ledger_commission_settlement_row(s, request) for s in cms_list
            ]
            return _ledger_merge_paginate_in_memory(combined)
        wal_qs = wal_qs.order_by("-created_at")
        paginator, page = _paginate(request, wal_qs)
        rows = [_ledger_wallet_row(t, request) for t in page]
        for r in rows:
            r.pop("_sort", None)
        return paginator.get_paginated_response(rows)

    pay_list = list(pay_qs.order_by("-created_at")[:800])
    wal_list = list(wal_qs.order_by("-created_at")[:800])
    combined_rows = [_ledger_payment_row(t, request) for t in pay_list] + [
        _ledger_wallet_row(t, request) for t in wal_list
    ]
    if portal_is_vendor:
        cms_qs = _commission_settlements_for_ledger(
            request, search, wst, vendor_user_id=uid_int
        ).order_by("-created_at")[:800]
        combined_rows.extend(
            _ledger_commission_settlement_row(s, request) for s in cms_qs
        )
    combined_rows.sort(key=lambda r: r["_sort"], reverse=True)
    for r in combined_rows:
        r.pop("_sort", None)

    paginator = AdminPagination()
    page_size = min(
        paginator.max_page_size,
        int(request.query_params.get("page_size") or paginator.page_size),
    )
    page_num = max(1, int(request.query_params.get("page") or 1))
    p = Paginator(combined_rows, page_size)
    page_obj = p.get_page(page_num)
    return Response(
        {
            "count": p.count,
            "next": page_obj.next_page_number() if page_obj.has_next() else None,
            "previous": page_obj.previous_page_number() if page_obj.has_previous() else None,
            "results": list(page_obj.object_list),
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_withdrawals_list(request):
    if err := _forbidden(request):
        return err
    qs = (
        WalletWithdrawal.objects.select_related(
            "wallet",
            "wallet__vendor",
            "wallet__owner",
            "payout_account",
        )
        .order_by("-created_at")
    )
    paginator, page = _paginate(request, qs)
    rows = []
    for w in page:
        seller = "-"
        vendor_id = ""
        owner_id = ""
        if w.wallet.vendor_id:
            seller = w.wallet.vendor.store_name
            vendor_id = str(w.wallet.vendor_id)
        elif w.wallet.owner_id:
            seller = w.wallet.owner.name
            owner_id = str(w.wallet.owner_id)
        payout_summary = ""
        if w.payout_account_id:
            pa = w.payout_account
            payout_summary = f"{pa.get_type_display()}"
            if pa.phone:
                payout_summary += f" · {pa.phone}"
            elif pa.bank_account_no:
                payout_summary += f" · …{pa.bank_account_no[-4:]}"
        rows.append(
            {
                "id": str(w.pk),
                "withdrawal_number": w.withdrawal_number,
                "seller": seller,
                "vendor_id": vendor_id,
                "owner_id": owner_id,
                "wallet_id": str(w.wallet_id),
                "wallet_type": w.wallet.type,
                "amount": float(w.amount),
                "method": w.get_method_display(),
                "method_code": w.method,
                "method_account": w.method_account,
                "bank_name": w.bank_name,
                "account_holder": w.account_holder,
                "admin_note": w.admin_note,
                "reject_reason": (w.reject_reason or "").strip(),
                "payout_account_id": str(w.payout_account_id) if w.payout_account_id else "",
                "payout_summary": payout_summary,
                "status": w.status,
                "date": w.created_at.date().isoformat(),
                "created_at": w.created_at.isoformat(),
                "updated_at": w.updated_at.isoformat() if getattr(w, "updated_at", None) else "",
                "processed_at": w.processed_at.isoformat() if w.processed_at else "",
                "balance": float(w.wallet.balance),
                "proof_image_url": (
                    absolute_media_url(request, w.proof_image) if w.proof_image else ""
                ),
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_withdrawal_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = WalletWithdrawal.objects.filter(pk=pk).select_related("wallet", "wallet__vendor").first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if row.status != WalletWithdrawal.Status.PENDING:
        return validation_error("only pending withdrawals can be approved or rejected")
    new_status = request.data.get("status")
    if new_status not in (WalletWithdrawal.Status.APPROVED, WalletWithdrawal.Status.REJECTED):
        return validation_error("status must be approved or rejected", field="status")
    if "admin_note" in request.data:
        row.admin_note = (request.data.get("admin_note") or "")[:2000]
    if new_status == WalletWithdrawal.Status.REJECTED and "reject_reason" in request.data:
        row.reject_reason = (request.data.get("reject_reason") or "")[:2000]
    row.status = new_status
    row.save()
    if new_status == WalletWithdrawal.Status.APPROVED:
        notify_withdrawal_approved(row)
    else:
        notify_withdrawal_rejected(row)
    audit_service.log(
        f"Withdrawal {row.withdrawal_number} {new_status}",
        log_type=AuditLog.Type.WALLET,
        performed_by=request.user,
        object_type="WalletWithdrawal",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="withdrawals",
        metadata={"withdrawal_number": row.withdrawal_number, "status": new_status},
    )
    return Response(
        {
            "id": str(row.pk),
            "withdrawal_number": row.withdrawal_number,
            "status": row.status,
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_withdrawals_summary(request):
    if err := _forbidden(request):
        return err
    pending = WalletWithdrawal.objects.filter(
        status=WalletWithdrawal.Status.PENDING
    ).count()
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    approved_today = WalletWithdrawal.objects.filter(
        status=WalletWithdrawal.Status.APPROVED,
        processed_at__gte=start,
    ).count()
    total_payout_accounts = PayoutAccount.objects.count()
    users_verified_kyc = User.objects.filter(
        kyc_status=User.KYCStatus.VERIFIED
    ).count()
    return Response(
        {
            "pending_withdrawals": pending,
            "approved_today": approved_today,
            "total_payout_accounts": total_payout_accounts,
            "users_kyc_verified": users_verified_kyc,
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_payout_accounts_list(request):
    if err := _forbidden(request):
        return err
    qs = PayoutAccount.objects.select_related("user").order_by("-updated_at", "-id")
    search = (request.query_params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(user__name__icontains=search)
            | Q(user__phone__icontains=search)
            | Q(phone__icontains=search)
            | Q(bank_account_no__icontains=search)
        )
    paginator, page = _paginate(request, qs)
    rows = []
    for pa in page:
        rows.append(
            {
                "id": str(pa.pk),
                "user_id": str(pa.user_id),
                "user_name": pa.user.name,
                "user_phone": pa.user.phone or "",
                "type": pa.type,
                "phone": pa.phone or "",
                "bank_name": pa.bank_name or "",
                "bank_account_no": pa.bank_account_no or "",
                "bank_account_holder": pa.bank_account_holder or "",
                "qr_image_url": absolute_media_url(request, pa.qr_image) if pa.qr_image else "",
                "created_at": pa.created_at.isoformat(),
                "updated_at": pa.updated_at.isoformat(),
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallets_list(request):
    if err := _forbidden(request):
        return err
    qs = (
        Wallet.objects.select_related("owner", "vendor", "family_group")
        .order_by("-updated_at")
    )
    search = request.query_params.get("search")
    if search:
        wq = (
            Q(owner__name__icontains=search)
            | Q(owner__phone__icontains=search)
            | Q(vendor__store_name__icontains=search)
            | Q(family_group__name__icontains=search)
        )
        if search.isdigit():
            try:
                wq |= Q(pk=int(search))
            except (ValueError, OverflowError):
                pass
        qs = qs.filter(wq)
    family_only = (request.query_params.get("family_only") or "").strip().lower()
    if family_only in ("true", "1", "yes"):
        qs = qs.filter(family_group_id__isnull=False)
    paginator, page = _paginate(request, qs)
    rows = []
    for w in page:
        label = "—"
        fam = "—"
        if w.vendor_id:
            label = w.vendor.store_name
        elif w.owner_id:
            label = w.owner.name
        if w.family_group_id:
            fam = w.family_group.name
        rows.append(
            {
                "id": str(w.pk),
                "owner": label,
                "type": w.type,
                "balance": float(w.balance),
                "status": w.status,
                "family": fam,
                "lastActivity": w.updated_at.isoformat(),
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_transactions_list(request):
    if err := _forbidden(request):
        return err
    qs = WalletTransaction.objects.select_related("wallet", "wallet__owner", "wallet__vendor").order_by(
        "-created_at"
    )
    raw_wid = (request.query_params.get("wallet_id") or "").strip()
    if raw_wid.isdigit():
        qs = qs.filter(wallet_id=int(raw_wid))
    df = request.query_params.get("date_from")
    dt = request.query_params.get("date_to")
    if df:
        d0 = parse_date(str(df).strip())
        if d0 is not None:
            qs = qs.filter(created_at__date__gte=d0)
    if dt:
        d1 = parse_date(str(dt).strip())
        if d1 is not None:
            qs = qs.filter(created_at__date__lte=d1)
    raw_status = (request.query_params.get("status") or "").strip()
    if raw_status:
        allowed = {c.value for c in WalletTransaction.Status}
        parts = [s.strip().lower() for s in raw_status.split(",") if s.strip()]
        statuses = [s for s in parts if s in allowed]
        if statuses:
            qs = qs.filter(status__in=statuses)
        else:
            qs = qs.none()
    paginator, page = _paginate(request, qs)
    rows = []
    for t in page:
        label = "—"
        if t.wallet.vendor_id:
            label = t.wallet.vendor.store_name
        elif t.wallet.owner_id:
            label = t.wallet.owner.name
        rows.append(
            {
                "id": t.txn_id,
                "user": label,
                "type": t.type,
                "item": t.description,
                "amount": float(t.amount),
                "time": t.created_at.strftime("%H:%M"),
                "status": t.status,
                "family": "—",
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_bonuses_list(request):
    if err := _forbidden(request):
        return err
    qs = WalletBonus.objects.order_by("-id")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(b.pk),
            "name": b.title,
            "title": b.title,
            "type": b.type,
            "amount": float(b.amount),
            "status": b.status,
            "minTopup": float(b.min_topup),
            "used": b.used_count,
            "expires": b.expires_at.isoformat() if b.expires_at else "",
            "is_percentage": b.is_percentage,
        }
        for b in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_loyalty_rules_list(request):
    if err := _forbidden(request):
        return err
    qs = LoyaltyRule.objects.order_by("-id")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(r.pk),
            "name": r.name,
            "event": r.event,
            "multiplier": r.multiplier,
            "status": r.status,
            "rule": r.rule_description,
            "rule_description": r.rule_description,
        }
        for r in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_families_list(request):
    if err := _forbidden(request):
        return err
    qs = FamilyGroup.objects.select_related("leader").annotate(mc=Count("members"))
    qs = qs.annotate(tb=Sum("wallets__balance")).order_by("-created_at")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(g.pk),
            "name": g.name,
            "leader": g.leader.name,
            "members": g.mc,
            "totalBalance": float(g.tb or 0),
            "status": g.status,
            "created": g.created_at.date().isoformat(),
            "type": g.type,
        }
        for g in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_purchase_orders_list(request):
    if err := _forbidden(request):
        return err
    merged: list[tuple] = []
    for po in (
        PurchaseOrder.objects.select_related("customer", "seller")
        .annotate(item_count=Count("lines"))
        .order_by("-created_at")
    ):
        merged.append(
            (
                po.created_at,
                {
                    "record_type": "purchase_order",
                    "detail_key": f"po-{po.pk}",
                    "id": po.po_number,
                    "pk": po.pk,
                    "customer": po.customer.name if po.customer_id else "Walk-in",
                    "items": po.item_count,
                    "subtotal": float(po.subtotal),
                    "tax": float(po.tax),
                    "discount": float(po.discount),
                    "delivery_fee": float(po.delivery_fee),
                    "total": float(po.total),
                    "status": po.status,
                    "date": po.created_at.date().isoformat(),
                    "seller": po.seller.store_name if po.seller_id else "Admin",
                    "vendor_id": str(po.seller_id) if po.seller_id else "",
                },
            )
        )
    for o in (
        Order.objects.filter(is_pos_order=True)
        .select_related("customer", "seller")
        .annotate(item_count=Count("items"))
        .order_by("-created_at")
    ):
        tax_est = o.total - o.subtotal + o.discount_amount - o.delivery_fee
        if tax_est < 0:
            tax_est = Decimal("0")
        merged.append(
            (
                o.created_at,
                {
                    "record_type": "pos_order",
                    "detail_key": f"ord-{o.pk}",
                    "id": o.order_number,
                    "pk": o.pk,
                    "customer": o.customer.name if o.customer_id else "Walk-in",
                    "items": o.item_count,
                    "subtotal": float(o.subtotal),
                    "tax": float(tax_est.quantize(Decimal("0.01"))),
                    "discount": float(o.discount_amount),
                    "delivery_fee": float(o.delivery_fee),
                    "total": float(o.total),
                    "status": o.status,
                    "date": o.created_at.date().isoformat(),
                    "seller": o.seller.store_name if o.seller_id else "Admin",
                    "vendor_id": str(o.seller_id) if o.seller_id else "",
                },
            )
        )
    merged.sort(key=lambda x: x[0], reverse=True)
    flat = [x[1] for x in merged]
    paginator = AdminPagination()
    page = paginator.paginate_queryset(flat, request)
    if page is not None:
        return paginator.get_paginated_response(page)
    return Response(flat)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_delivery_men_list(request):
    if err := _forbidden(request):
        return err
    qs = DeliveryMan.objects.select_related("user", "zone").order_by("-id")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(d.pk),
            "user_id": str(d.user_id),
            "name": d.user.name,
            "phone": d.user.phone,
            "status": d.status,
            "zone_id": str(d.zone_id) if d.zone_id else "",
            "zone": d.zone.name if d.zone_id else "",
            "deliveries": d.deliveries_count,
            "rating": float(d.rating),
            "earning": float(d.total_earnings),
            "pending": float(d.pending_earnings),
            "id_document_front": absolute_media_url(request, d.id_document_front)
            if getattr(d, "id_document_front", None)
            else "",
            "id_document_back": absolute_media_url(request, d.id_document_back)
            if getattr(d, "id_document_back", None)
            else "",
            "selfie": absolute_media_url(request, d.selfie) if getattr(d, "selfie", None) else "",
            "emergency_contact": d.emergency_contact,
            "license_number": d.license_number,
        }
        for d in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_audit_log_filter_options(request):
    if err := enforce_audit_log_access(request):
        return err
    modules_qs = (
        AuditLog.objects.exclude(module="")
        .order_by("module")
        .values_list("module", flat=True)
        .distinct()
    )
    modules = sorted({m for m in modules_qs if m})[:200]
    actor_ids = list(
        AuditLog.objects.exclude(performed_by_id__isnull=True)
        .values_list("performed_by_id", flat=True)
        .distinct()[:500]
    )
    actors: list[dict] = []
    if actor_ids:
        rows = User.objects.filter(pk__in=actor_ids).values("id", "name", "phone")[:500]
        for u in rows:
            actors.append(
                {
                    "id": str(u["id"]),
                    "name": (u["name"] or "").strip(),
                    "phone": (u["phone"] or "").strip(),
                }
            )
        actors.sort(key=lambda x: (x["name"].lower(), x["id"]))
    return Response({"modules": modules, "actors": actors})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_audit_log_detail(request, pk):
    if err := enforce_audit_log_access(request):
        return err
    log = AuditLog.objects.select_related("performed_by").filter(pk=pk).first()
    if not log:
        return Response({"detail": "Not found."}, status=404)
    return Response(
        {
            "id": str(log.pk),
            "description": log.action,
            "action": log.action,
            "action_kind": log.action_kind,
            "module": log.module or "",
            "type": log.type,
            "user": log.performed_by.name if log.performed_by_id else "System",
            "user_id": log.performed_by_id,
            "time": log.created_at.isoformat(),
            "created_at": log.created_at.isoformat(),
            "ip_address": str(log.ip_address) if log.ip_address else None,
            "object_type": log.object_type or "",
            "object_id": log.object_id or "",
            "metadata": log.metadata if isinstance(log.metadata, dict) else {},
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_audit_logs_list(request):
    if err := enforce_audit_log_access(request):
        return err
    qs = AuditLog.objects.select_related("performed_by")

    uid_raw = request.query_params.get("user_id") or request.query_params.get("performed_by")
    if uid_raw is not None and str(uid_raw).strip() != "":
        try:
            qs = qs.filter(performed_by_id=int(uid_raw))
        except (TypeError, ValueError):
            pass

    action_kind = request.query_params.get("action_kind")
    if action_kind:
        qs = qs.filter(action_kind=action_kind)

    module = request.query_params.get("module")
    if module:
        qs = qs.filter(module=str(module).strip())

    typ = request.query_params.get("type")
    if typ:
        qs = qs.filter(type=typ)

    df = request.query_params.get("date_from")
    if df:
        b = _audit_datetime_param(df, end_of_day=False)
        if b:
            qs = qs.filter(created_at__gte=b)
    dto = request.query_params.get("date_to")
    if dto:
        b = _audit_datetime_param(dto, end_of_day=True)
        if b:
            qs = qs.filter(created_at__lte=b)

    search = (request.query_params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(action__icontains=search)
            | Q(module__icontains=search)
            | Q(object_type__icontains=search)
        )

    ot = request.query_params.get("object_type")
    oid = request.query_params.get("object_id")
    if ot:
        qs = qs.filter(object_type=ot)
    if oid:
        qs = qs.filter(object_id=str(oid))

    ordering = (request.query_params.get("ordering") or "-created_at").strip()
    if ordering not in ("created_at", "-created_at"):
        ordering = "-created_at"
    qs = qs.order_by(ordering)

    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(log.pk),
            "action": log.action,
            "description": log.action,
            "user": log.performed_by.name if log.performed_by_id else "System",
            "user_id": log.performed_by_id,
            "action_kind": log.action_kind,
            "module": log.module or "",
            "type": log.type,
            "time": log.created_at.isoformat(),
            "created_at": log.created_at.isoformat(),
            "ip_address": str(log.ip_address) if log.ip_address else None,
            "object_type": log.object_type or "",
            "object_id": log.object_id or "",
            "metadata_preview": _audit_metadata_preview(
                log.metadata if isinstance(log.metadata, dict) else {}
            ),
        }
        for log in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_tickets_list(request):
    if err := _forbidden(request):
        return err
    qs = SupportTicket.objects.select_related("submitter").order_by("-last_activity_at", "-created_at")
    sp = (request.query_params.get("source_panel") or "").strip()
    if sp in dict(SupportTicket.SourcePanel.choices):
        qs = qs.filter(source_panel=sp)
    st = (request.query_params.get("status") or "").strip()
    if st in dict(SupportTicket.Status.choices):
        qs = qs.filter(status=st)
    q = (request.query_params.get("search") or "").strip()
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(ticket_number__icontains=q))
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
                "submitter_name": t.submitter.name if t.submitter_id else "",
                "submitter_phone": t.submitter.phone if t.submitter_id else "",
                "submitter_avatar_url": (
                    absolute_media_url(request, t.submitter.avatar) if t.submitter_id else ""
                ),
                "created": t.created_at.date().isoformat(),
                "last_activity": (t.last_activity_at or t.created_at).date().isoformat(),
                "has_unread": support_ticket_service.ticket_has_unread_for_staff_reader(
                    t, state=st
                ),
                "last_message_preview": support_ticket_service.last_message_preview_text(t),
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_ticket_detail(request, ticket_number):
    if err := _forbidden(request):
        return err
    t = (
        SupportTicket.objects.filter(ticket_number=ticket_number)
        .select_related("submitter", "assigned_to")
        .prefetch_related("messages__sender", "messages__attachments")
        .first()
    )
    if not t:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        support_ticket_service.ensure_initial_message(t)
        support_ticket_service.mark_ticket_read(t, request.user)
        sub = t.submitter
        assign = t.assigned_to
        _av = lambda u: absolute_media_url(request, u.avatar)
        counterpart_online = sub.pk in online_user_ids_for([sub.pk])
        msgs = support_ticket_service.serialize_ticket_messages(
            list(t.messages.all()),
            _admin_support_attachment_url,
            ticket=t,
            sender_avatar_url_fn=_av,
            viewer_user_id=request.user.pk,
            viewer_is_staff=True,
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
                "submitter": {
                    "id": sub.pk,
                    "name": sub.name,
                    "phone": sub.phone,
                    "role": sub.role,
                    "avatar_url": absolute_media_url(request, sub.avatar),
                },
                "assigned_to": (
                    {"id": assign.pk, "name": assign.name, "phone": assign.phone}
                    if assign
                    else None
                ),
                "messages": msgs,
            }
        )
    if "status" in request.data:
        st = request.data.get("status")
        if st not in dict(SupportTicket.Status.choices):
            return validation_error("invalid status")
        t.status = st
    if "priority" in request.data:
        pr = request.data.get("priority")
        if pr in dict(SupportTicket.Priority.choices):
            t.priority = pr
    if "assigned_to" in request.data:
        raw = request.data.get("assigned_to")
        if raw in (None, "", "null"):
            t.assigned_to = None
        else:
            try:
                uid = int(raw)
            except (TypeError, ValueError):
                return validation_error("assigned_to must be a user id or null")
            assign_u = User.objects.filter(pk=uid).first()
            if not assign_u:
                return Response({"detail": "assigned_to user not found."}, status=400)
            t.assigned_to = assign_u
    if "category" in request.data:
        cat = (request.data.get("category") or "").strip()
        if cat in dict(SupportTicket.Category.choices):
            t.category = cat
    t.save()
    return Response(
        {
            "id": t.ticket_number,
            "status": t.status,
            "priority": t.priority,
            "category": t.category,
            "assigned_to": t.assigned_to_id,
        }
    )


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def admin_ticket_messages(request, ticket_number):
    if err := _forbidden(request):
        return err
    t = SupportTicket.objects.filter(ticket_number=ticket_number).first()
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
        sub = t.submitter
        counterpart_online = sub.pk in online_user_ids_for([sub.pk])
        read_at = support_ticket_service.get_counterpart_last_read_at(
            t, viewer_is_staff=True
        )
        results, has_more = support_ticket_service.messages_page_before(
            t,
            before_id,
            limit,
            _admin_support_attachment_url,
            sender_avatar_url_fn=lambda u: absolute_media_url(request, u.avatar),
            viewer_user_id=request.user.pk,
            viewer_is_staff=True,
            counterpart_online=counterpart_online,
            counterpart_read_at=read_at,
        )
        return Response({"results": results, "has_more": has_more})

    body, files = support_ticket_service.extract_message_body_and_files_from_request(request)
    try:
        msg = support_ticket_service.append_message(t, request.user, body, files)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    support_notification_service.notify_submitter_staff_replied(t)
    msg = (
        SupportTicketMessage.objects.filter(pk=msg.pk)
        .select_related("sender")
        .prefetch_related("attachments")
        .first()
    )
    sub = t.submitter
    counterpart_online = sub.pk in online_user_ids_for([sub.pk])
    read_at = support_ticket_service.get_counterpart_last_read_at(t, viewer_is_staff=True)
    tick = support_ticket_service.delivery_tick_for_message(
        msg,
        viewer_user_id=request.user.pk,
        viewer_is_staff=True,
        counterpart_online=counterpart_online,
        counterpart_last_read_at=read_at,
    )
    return Response(
        {
            "ok": True,
            "message": support_ticket_service.message_to_row(
                msg,
                _admin_support_attachment_url,
                sender_avatar_url_fn=lambda u: absolute_media_url(request, u.avatar),
                delivery_ticks=tick,
            ),
        },
        status=201,
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_support_ticket_attachment(request, attachment_id: int):
    if err := _forbidden(request):
        return err
    att = support_ticket_service.get_attachment_or_none(attachment_id)
    if not att:
        return Response({"detail": "Not found."}, status=404)
    return support_ticket_service.attachment_file_response(att)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_flagged_activities_list(request):
    if err := _forbidden(request):
        return err
    qs = FlaggedActivity.objects.select_related("user").order_by("-created_at")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(f.pk),
            "user": f.user.name if f.user_id else "Unknown",
            "type": f.activity_type,
            "severity": f.severity,
            "status": f.status,
            "detail": f.detail or "",
            "time": f.created_at.isoformat(),
        }
        for f in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_flagged_activity_detail_write(request, pk):
    from core.services import security_service

    if err := _forbidden(request):
        return err
    row = FlaggedActivity.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    resolution_note = (request.data.get("resolution_note") or "").strip()
    if "status" in request.data:
        st = request.data.get("status")
        if st in {c[0] for c in FlaggedActivity.Status.choices}:
            if st in (
                FlaggedActivity.Status.REVIEWED,
                FlaggedActivity.Status.RESOLVED,
            ):
                note_err = security_service.validate_flag_resolution_note(
                    row.severity, resolution_note
                )
                if note_err:
                    return Response({"detail": note_err}, status=400)
                if resolution_note:
                    security_service.append_resolution_note(row, resolution_note)
            row.status = st
    if row.status in (
        FlaggedActivity.Status.REVIEWED,
        FlaggedActivity.Status.RESOLVED,
    ):
        row.reviewed_by = request.user
    row.save()
    return Response({"id": str(row.pk), "status": row.status})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_shipping_methods_list(request):
    if err := _forbidden(request):
        return err
    qs = ShippingMethod.objects.order_by("name")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(m.pk),
            "name": m.name,
            "type": m.type,
            "cost": float(m.cost),
            "threshold": float(m.free_threshold),
            "status": m.status,
        }
        for m in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_shipping_zones_list(request):
    if err := _forbidden(request):
        return err
    qs = ShippingZone.objects.order_by("name")
    search = request.query_params.get("search")
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(areas__icontains=search))
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(z.pk),
            "name": z.name,
            "areas": z.areas,
            "flatRate": float(z.flat_rate),
            "freeAbove": float(z.free_above) if z.free_above is not None else 0,
            "status": z.status,
        }
        for z in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_weight_rules_list(request):
    if err := _forbidden(request):
        return err
    # Use Cast + .values() so SQLite reads these as REAL; some legacy rows trip DecimalField quantize.
    qs = (
        WeightRule.objects.select_related("zone")
        .annotate(
            min_w=Cast("min_weight", FloatField()),
            max_w=Cast("max_weight", FloatField()),
            rpk=Cast("rate_per_kg", FloatField()),
        )
        .values(
            "id",
            "zone_id",
            "zone__name",
            "min_w",
            "max_w",
            "rpk",
        )
        .order_by("zone_id", "min_w")
    )
    zone_id = request.query_params.get("zone")
    if zone_id:
        qs = qs.filter(zone_id=zone_id)
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(w["id"]),
            "zone_id": str(w["zone_id"]),
            "zone": w["zone__name"] or "",
            "minWeight": float(w["min_w"] or 0),
            "maxWeight": float(w["max_w"] or 0),
            "ratePerKg": float(w["rpk"] or 0),
        }
        for w in page
    ]
    return paginator.get_paginated_response(rows)


PORTAL_SURFACES = (
    {"id": "vendor", "label": "Vendor"},
    {"id": "portal", "label": "Portal"},
    {"id": "family_portal", "label": "Family Portal"},
    {"id": "child_portal", "label": "Child Portal"},
)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_account_portal_catalog(request):
    if err := _forbidden(request):
        return err
    return Response(
        {
            "portal_surfaces": [dict(x) for x in PORTAL_SURFACES],
            "user_account_roles": [
                {"value": c[0], "label": str(c[1])} for c in User.Role.choices
            ],
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_roles_list(request):
    if err := _forbidden(request):
        return err
    qs = Role.objects.annotate(assigned_count=Count("employeeprofile")).order_by("name")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(r.pk),
            "name": r.name,
            "status": r.status,
            "is_system": r.is_system,
            "permissions": r.permissions if isinstance(r.permissions, dict) else {},
            "assigned_count": int(getattr(r, "assigned_count", 0) or 0),
        }
        for r in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_employees_list(request):
    if err := _forbidden(request):
        return err
    qs = EmployeeProfile.objects.select_related("user", "role").order_by("-id")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(e.pk),
            "user_id": str(e.user_id),
            "name": e.user.name,
            "email": e.user.email,
            "phone": e.user.phone,
            "role_id": str(e.role_id),
            "role": e.role.name,
            "status": e.status,
            "modules_access": e.modules_access if isinstance(e.modules_access, list) else [],
        }
        for e in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_staff_users_list(request):
    if err := _forbidden(request):
        return err
    qs = User.objects.filter(Q(is_staff=True) | Q(role=User.Role.SUPER_ADMIN)).order_by("-created_at")
    paginator, page = _paginate(request, qs)
    from core import rbac_django as rbac

    rows = [
        {
            "id": str(u.pk),
            "name": u.name,
            "email": u.email or "",
            "phone": u.phone or "",
            "role": u.role,
            "lastLogin": u.last_login.isoformat() if u.last_login else "",
            "status": "active" if u.is_active else "inactive",
            "groups": rbac.user_groups_payload(u),
            "group_ids": list(u.groups.values_list("pk", flat=True)),
        }
        for u in page
    ]
    return paginator.get_paginated_response(rows)


def _to_decimal(v, default="0"):
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


def _parse_coupon_usage_limit(raw) -> int | None:
    """Positive limits only; 0 or invalid means unlimited (None)."""
    if raw in (None, ""):
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _make_unique_slug(model_cls, raw_value, instance_pk=None):
    base = slugify((raw_value or "").strip()) or uuid4().hex[:8]
    base = base[:280]
    candidate = base
    n = 1
    qs = model_cls.objects.all()
    if instance_pk is not None:
        qs = qs.exclude(pk=instance_pk)
    while qs.filter(slug=candidate).exists():
        suffix = f"-{n}"
        candidate = f"{base[: max(1, 300 - len(suffix))]}{suffix}"
        n += 1
    return candidate


# Soft-deleted vendor products (seller cleared) should not block SKU reuse.
_VENDOR_SOFT_DELETED_PRODUCT_SKU_FILTER = {
    "seller__isnull": True,
    "status": Product.Status.DRAFT,
    "stock": 0,
    "enable_pos": False,
    "enable_reels": False,
}


def _product_sku_exists(sku: str, *, exclude_pk=None) -> bool:
    sku = (sku or "").strip()
    if not sku:
        return False
    qs = Product.objects.filter(sku=sku).exclude(**_VENDOR_SOFT_DELETED_PRODUCT_SKU_FILTER)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _release_product_sku_for_reuse(product: Product) -> None:
    """Rename SKU when a product is soft-deleted so the original code can be reused."""
    sku = (product.sku or "").strip()
    if not sku:
        return
    marker = f"-archived-{product.pk}"
    if sku.endswith(marker):
        return
    max_base = max(1, 100 - len(marker))
    product.sku = f"{sku[:max_base]}{marker}"


def _validate_hex_color(value):
    if not value:
        return ""
    v = str(value).strip()
    if re.match(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", v):
        return v.lower()
    return None


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_category_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"detail": "name is required"}, status=400)
    slug = _make_unique_slug(Category, request.data.get("slug") or name)
    parent_id = request.data.get("parent_id")
    parent = Category.objects.filter(pk=parent_id).first() if parent_id else None
    row = Category.objects.create(
        name=name,
        slug=slug,
        parent=parent,
        level=(parent.level + 1) if parent else 0,
        seo_title=request.data.get("seo_title", ""),
        seo_description=request.data.get("seo_description", ""),
        sort_order=int(request.data.get("sort_order") or 0),
        status=request.data.get("status") or Category.Status.ACTIVE,
    )
    image = request.FILES.get("image")
    if image:
        row.image = image
        row.save(update_fields=["image"])
    return Response({"id": str(row.pk), "name": row.name, "slug": row.slug}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_category_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Category.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    for field in ("name", "seo_title", "seo_description", "status"):
        if field in request.data:
            setattr(row, field, request.data.get(field))
    if "slug" in request.data and request.data.get("slug"):
        row.slug = _make_unique_slug(Category, request.data.get("slug"), instance_pk=row.pk)
    elif "name" in request.data and request.data.get("name"):
        row.slug = _make_unique_slug(Category, request.data.get("name"), instance_pk=row.pk)
    if "sort_order" in request.data:
        row.sort_order = int(request.data.get("sort_order") or 0)
    if "parent_id" in request.data:
        parent = Category.objects.filter(pk=request.data.get("parent_id")).first()
        row.parent = parent
        row.level = (parent.level + 1) if parent else 0
    image = request.FILES.get("image")
    if image:
        row.image = image
    row.save()
    return Response({"id": str(row.pk), "name": row.name, "slug": row.slug})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_brand_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"detail": "name is required"}, status=400)
    row = Brand.objects.create(name=name, status=request.data.get("status") or Brand.Status.ACTIVE)
    logo = request.FILES.get("logo")
    if logo:
        row.logo = logo
        row.save(update_fields=["logo"])
    return Response({"id": str(row.pk), "name": row.name}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_brand_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Brand.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "name" in request.data:
        row.name = request.data.get("name")
    if "status" in request.data:
        row.status = request.data.get("status")
    logo = request.FILES.get("logo")
    if logo:
        row.logo = logo
    row.save()
    return Response({"id": str(row.pk), "name": row.name, "status": row.status})


MAX_ADMIN_PRODUCT_GALLERY = 15


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def admin_product_create(request):
    if err := _forbidden(request):
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
    if _product_sku_exists(sku):
        return validation_error("This SKU is already in use. Choose a different SKU.", field="sku")
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
        seller=Vendor.objects.filter(pk=request.data.get("seller_id")).first()
        if request.data.get("seller_id")
        else None,
        status=request.data.get("status") or Product.Status.DRAFT,
        is_featured=str(request.data.get("is_featured", "")).lower() == "true",
        has_variations=str(request.data.get("has_variations", "")).lower() == "true",
        seo_title=request.data.get("seo_title") or "",
        seo_description=request.data.get("seo_description") or "",
        seo_keywords=request.data.get("seo_keywords") or "",
        enable_reels=str(request.data.get("enable_reels", "")).lower() == "true",
        enable_pos=str(request.data.get("enable_pos", "")).lower() == "true",
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
    gallery_files = request.FILES.getlist("gallery_images")[:MAX_ADMIN_PRODUCT_GALLERY]
    for idx, f in enumerate(gallery_files):
        ProductImage.objects.create(product=row, image=f, sort_order=idx)
    return Response({"id": str(row.pk), "name": row.name, "slug": row.slug}, status=201)


def _admin_product_detail_payload(request, row: Product) -> dict:
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
        "stock": row.stock,
        "tax_percent": float(row.tax_percent),
        "type": row.type,
        "status": row.status,
        "category_id": str(row.category_id),
        "category_name": row.category.name,
        "brand_id": str(row.brand_id) if row.brand_id else "",
        "brand_name": row.brand.name if row.brand_id else "",
        "unit_id": str(row.unit_id) if row.unit_id else "",
        "unit_name": row.unit.name if row.unit_id else "",
        "seller_id": str(row.seller_id) if row.seller_id else "",
        "seller_name": row.seller.store_name if row.seller_id else "",
        "is_featured": row.is_featured,
        "has_variations": row.has_variations,
        "enable_reels": row.enable_reels,
        "enable_pos": row.enable_pos,
        "seo_title": row.seo_title or "",
        "seo_description": row.seo_description or "",
        "seo_keywords": row.seo_keywords or "",
        "image_url": product_primary_image_url(request, row),
        "images": images_payload,
    }


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def admin_product_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = (
        Product.objects.select_related("category", "brand", "unit", "seller")
        .prefetch_related(
            Prefetch("images", queryset=ProductImage.objects.order_by("sort_order", "id"))
        )
        .filter(pk=pk)
        .first()
    )
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(_admin_product_detail_payload(request, row))
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    for field in ("name", "description", "short_description", "sku", "status", "seo_title", "seo_description", "seo_keywords"):
        if field in request.data:
            setattr(row, field, request.data.get(field))
    if "sku" in request.data:
        new_sku = (request.data.get("sku") or "").strip()
        if new_sku and _product_sku_exists(new_sku, exclude_pk=row.pk):
            return validation_error("This SKU is already in use. Choose a different SKU.", field="sku")
    if "slug" in request.data or "name" in request.data:
        row.slug = _make_unique_slug(Product, request.data.get("slug") or request.data.get("name") or row.name, instance_pk=row.pk)
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
        raw_b = scalar_request_value(request.data.get("brand_id"))
        row.brand = Brand.objects.filter(pk=raw_b).first() if raw_b else None
    if "unit_id" in request.data:
        raw_u = scalar_request_value(request.data.get("unit_id"))
        row.unit = Unit.objects.filter(pk=raw_u).first() if raw_u else None
    if "seller_id" in request.data:
        raw_s = scalar_request_value(request.data.get("seller_id"))
        if raw_s is None or raw_s in ("", "null", "none"):
            row.seller = None
        else:
            row.seller = Vendor.objects.filter(pk=raw_s).first()
    if "type" in request.data:
        row.type = request.data.get("type") or row.type
    for bfield in ("is_featured", "has_variations", "enable_reels", "enable_pos"):
        if bfield in request.data:
            setattr(row, bfield, str(request.data.get(bfield)).lower() == "true")
    for raw_id in request.data.getlist("delete_gallery_image_ids"):
        try:
            gid = int(raw_id)
        except (TypeError, ValueError):
            continue
        ProductImage.objects.filter(pk=gid, product_id=row.pk).delete()
    gallery_new = request.FILES.getlist("gallery_images")
    if gallery_new:
        current_count = ProductImage.objects.filter(product=row).count()
        remaining = max(0, MAX_ADMIN_PRODUCT_GALLERY - current_count)
        agg = ProductImage.objects.filter(product=row).aggregate(m=Max("sort_order"))
        start_order = (agg["m"] if agg["m"] is not None else -1) + 1
        for idx, f in enumerate(gallery_new[:remaining]):
            ProductImage.objects.create(product=row, image=f, sort_order=start_order + idx)
    image = request.FILES.get("image")
    if image:
        row.image = image
    row.save()
    return Response({"id": str(row.pk), "name": row.name, "slug": row.slug})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_banner_create(request):
    if err := _forbidden(request):
        return err
    title = (request.data.get("title") or "").strip()
    image = request.FILES.get("image")
    placement = (request.data.get("placement") or Banner.Placement.HOMEPAGE).strip()
    if not title:
        return Response({"detail": "title is required"}, status=400)
    is_promo_strip = placement == Banner.Placement.PROMO_STRIP
    if not is_promo_strip and not image:
        return Response({"detail": "title and image are required"}, status=400)
    promo_variants = {c.value for c in Banner.CardVariant}
    card_variant = (request.data.get("card_variant") or "").strip()
    if is_promo_strip:
        if card_variant not in promo_variants:
            return Response(
                {"detail": "card_variant must be one of: " + ", ".join(sorted(promo_variants))},
                status=400,
            )
    else:
        card_variant = ""
    try:
        sort_order = int(request.data.get("sort_order", 0))
    except (TypeError, ValueError):
        sort_order = 0
    cta_text = (request.data.get("cta_text") or "")[:40]
    badge_text = (request.data.get("badge_text") or "")[:80]
    gradient = _validate_hex_color(request.data.get("gradient"))
    if request.data.get("gradient") and gradient is None:
        return Response({"detail": "gradient must be a valid hex color (#fff or #ffffff)"}, status=400)
    row = Banner.objects.create(
        title=title,
        subtitle=(request.data.get("subtitle") or "")[:255],
        placement=placement,
        image=image if image else None,
        click_url=request.data.get("click_url") or "",
        category=Category.objects.filter(pk=request.data.get("category_id")).first() if request.data.get("category_id") else None,
        gradient=gradient or "",
        start_date=request.data.get("start_date") or None,
        end_date=request.data.get("end_date") or None,
        status=request.data.get("status") or Banner.Status.ACTIVE,
        sort_order=sort_order,
        card_variant=card_variant,
        cta_text=cta_text,
        badge_text=badge_text,
    )
    return Response({"id": str(row.pk), "title": row.title}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_banner_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Banner.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    for field in ("title", "subtitle", "placement", "click_url", "status", "card_variant", "cta_text", "badge_text"):
        if field in request.data:
            val = request.data.get(field) or ""
            if field == "subtitle":
                val = str(val)[:255]
            elif field == "cta_text":
                val = str(val)[:40]
            elif field == "badge_text":
                val = str(val)[:80]
            setattr(row, field, val)
    if "sort_order" in request.data:
        try:
            row.sort_order = int(request.data.get("sort_order", 0))
        except (TypeError, ValueError):
            row.sort_order = 0
    if "gradient" in request.data:
        gradient = _validate_hex_color(request.data.get("gradient"))
        if request.data.get("gradient") and gradient is None:
            return Response({"detail": "gradient must be a valid hex color (#fff or #ffffff)"}, status=400)
        row.gradient = gradient or ""
    if "start_date" in request.data:
        row.start_date = request.data.get("start_date") or None
    if "end_date" in request.data:
        row.end_date = request.data.get("end_date") or None
    if "category_id" in request.data:
        row.category = Category.objects.filter(pk=request.data.get("category_id")).first()
    image = request.FILES.get("image")
    if image:
        row.image = image
    promo_variants = {c.value for c in Banner.CardVariant}
    if row.placement == Banner.Placement.PROMO_STRIP:
        if row.card_variant not in promo_variants:
            return Response(
                {"detail": "card_variant must be set for promo_strip banners"},
                status=400,
            )
        if not row.image:
            pass  # optional
    elif row.placement in (
        Banner.Placement.HOMEPAGE,
        Banner.Placement.CATEGORY,
        Banner.Placement.SIDEBAR,
        Banner.Placement.SMALL_STRIP,
        Banner.Placement.FOOTER_PROMO,
    ) and not row.image:
        return Response({"detail": "image is required for this placement"}, status=400)
    row.save()
    return Response({"id": str(row.pk), "title": row.title})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def admin_cms_page_create(request):
    if err := _forbidden(request):
        return err
    title = (request.data.get("title") or "").strip()
    content = request.data.get("content") or ""
    if not title:
        return Response({"detail": "title is required"}, status=400)
    image = request.FILES.get("image")
    row = CMSPage.objects.create(
        title=title,
        slug=_make_unique_slug(CMSPage, request.data.get("slug") or title),
        content=content,
        featured_image=image if image else None,
        status=request.data.get("status") or CMSPage.Status.DRAFT,
        seo_title=request.data.get("seo_title") or "",
        seo_description=request.data.get("seo_description") or "",
    )
    audit_service.log(
        f"Created CMS page {title!r} (slug={row.slug})",
        log_type=AuditLog.Type.MARKETING,
        performed_by=request.user,
        object_type="CMSPage",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.CREATE,
        module="cms",
        metadata={"slug": row.slug},
    )
    return Response({"id": str(row.pk), "title": row.title, "slug": row.slug}, status=201)


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def admin_cms_page_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = CMSPage.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(
            {
                "id": str(row.pk),
                "title": row.title,
                "slug": row.slug,
                "content": row.content or "",
                "status": row.status,
                "seoTitle": row.seo_title,
                "seoDesc": row.seo_description,
                "lastUpdated": row.last_updated.date().isoformat() if row.last_updated else "",
                "imageUrl": absolute_media_url(request, row.featured_image) if row.featured_image else "",
            }
        )
    if request.method == "DELETE":
        rid, slug = str(row.pk), row.slug
        row.delete()
        audit_service.log(
            f"Deleted CMS page (id={rid}, slug={slug})",
            log_type=AuditLog.Type.MARKETING,
            performed_by=request.user,
            object_type="CMSPage",
            object_id=rid,
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="cms",
        )
        return Response({"ok": True})
    for field in ("title", "content", "status", "seo_title", "seo_description"):
        if field in request.data:
            setattr(row, field, request.data.get(field))
    if "slug" in request.data or "title" in request.data:
        row.slug = _make_unique_slug(CMSPage, request.data.get("slug") or request.data.get("title") or row.title, instance_pk=row.pk)
    image = request.FILES.get("image")
    if image:
        row.featured_image = image
    elif request.data.get("clear_featured_image") in (True, "true", "1", 1):
        if row.featured_image:
            row.featured_image.delete(save=False)
        row.featured_image = None
    row.save()
    audit_service.log(
        f"Updated CMS page {row.title!r} (id={row.pk})",
        log_type=AuditLog.Type.MARKETING,
        performed_by=request.user,
        object_type="CMSPage",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="cms",
        metadata={"slug": row.slug, "status": row.status},
    )
    return Response({"id": str(row.pk), "title": row.title, "slug": row.slug})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_blog_posts_list(request):
    if err := _forbidden(request):
        return err
    qs = BlogPost.objects.select_related("author").order_by("-created_at")
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(p.pk),
            "title": p.title,
            "slug": p.slug,
            "status": p.status,
            "excerpt": (p.excerpt or "")[:120],
            "seoTitle": p.seo_title,
            "seoDesc": p.seo_description,
            "publishedAt": p.published_at.date().isoformat() if p.published_at else "",
            "authorName": getattr(p.author, "name", "") if p.author_id else "",
            "coverUrl": absolute_media_url(request, p.cover_image) if p.cover_image else "",
        }
        for p in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def admin_blog_post_create(request):
    if err := _forbidden(request):
        return err
    title = (request.data.get("title") or "").strip()
    if not title:
        return Response({"detail": "title is required"}, status=400)
    status_val = request.data.get("status") or BlogPost.Status.DRAFT
    published_at = None
    if status_val == BlogPost.Status.PUBLISHED:
        published_at = timezone.now()
    cover = request.FILES.get("cover_image") or request.FILES.get("image")
    row = BlogPost.objects.create(
        title=title,
        slug=_make_unique_slug(BlogPost, request.data.get("slug") or title),
        excerpt=(request.data.get("excerpt") or "")[:500],
        content=request.data.get("content") or "",
        cover_image=cover if cover else None,
        author=request.user,
        status=status_val,
        seo_title=request.data.get("seo_title") or "",
        seo_description=request.data.get("seo_description") or "",
        published_at=published_at,
    )
    audit_service.log(
        f"Created blog post {title!r} (slug={row.slug})",
        log_type=AuditLog.Type.MARKETING,
        performed_by=request.user,
        object_type="BlogPost",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.CREATE,
        module="blog",
        metadata={"slug": row.slug},
    )
    return Response({"id": str(row.pk), "title": row.title, "slug": row.slug}, status=201)


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def admin_blog_post_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = BlogPost.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(
            {
                "id": str(row.pk),
                "title": row.title,
                "slug": row.slug,
                "excerpt": row.excerpt or "",
                "content": row.content or "",
                "status": row.status,
                "seoTitle": row.seo_title,
                "seoDesc": row.seo_description,
                "publishedAt": row.published_at.isoformat() if row.published_at else "",
                "coverUrl": absolute_media_url(request, row.cover_image) if row.cover_image else "",
            }
        )
    if request.method == "DELETE":
        rid, slug = str(row.pk), row.slug
        row.delete()
        audit_service.log(
            f"Deleted blog post (id={rid}, slug={slug})",
            log_type=AuditLog.Type.MARKETING,
            performed_by=request.user,
            object_type="BlogPost",
            object_id=rid,
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="blog",
        )
        return Response({"ok": True})
    for field in ("title", "excerpt", "content", "status", "seo_title", "seo_description"):
        if field in request.data:
            val = request.data.get(field)
            if field == "excerpt":
                val = (val or "")[:500]
            setattr(row, field, val)
    if "slug" in request.data or "title" in request.data:
        row.slug = _make_unique_slug(
            BlogPost,
            request.data.get("slug") or request.data.get("title") or row.title,
            instance_pk=row.pk,
        )
    if "status" in request.data:
        if row.status == BlogPost.Status.PUBLISHED and not row.published_at:
            row.published_at = timezone.now()
    cover = request.FILES.get("cover_image") or request.FILES.get("image")
    if cover:
        row.cover_image = cover
    elif request.data.get("clear_cover_image") in (True, "true", "1", 1):
        if row.cover_image:
            row.cover_image.delete(save=False)
        row.cover_image = None
    row.save()
    audit_service.log(
        f"Updated blog post {row.title!r} (id={row.pk})",
        log_type=AuditLog.Type.MARKETING,
        performed_by=request.user,
        object_type="BlogPost",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="blog",
        metadata={"slug": row.slug, "status": row.status},
    )
    return Response({"id": str(row.pk), "title": row.title, "slug": row.slug})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_attribute_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return Response({"detail": "name is required"}, status=400)
    row = Attribute.objects.create(
        name=name,
        type=request.data.get("type") or Attribute.Type.DROPDOWN,
        status=request.data.get("status") or Attribute.Status.ACTIVE,
    )
    return Response({"id": str(row.pk), "name": row.name}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_attribute_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Attribute.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "name" in request.data:
        row.name = request.data.get("name")
    if "type" in request.data:
        row.type = request.data.get("type")
    if "status" in request.data:
        row.status = request.data.get("status")
    row.save()
    return Response({"id": str(row.pk), "name": row.name})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_attribute_value_create(request):
    if err := _forbidden(request):
        return err
    attribute_id = request.data.get("attribute_id")
    value = (request.data.get("value") or "").strip()
    if not attribute_id or not value:
        return Response({"detail": "attribute_id and value are required"}, status=400)
    attr = Attribute.objects.filter(pk=attribute_id).first()
    if not attr:
        return Response({"detail": "invalid attribute_id"}, status=404)
    if attr.type == Attribute.Type.COLOR:
        color = _validate_hex_color(value)
        if color is None:
            return Response({"detail": "value must be a valid hex color (#RGB or #RRGGBB)"}, status=400)
        value = color
    row = AttributeValue.objects.create(
        attribute=attr,
        value=value,
        sort_order=int(request.data.get("sort_order") or 0),
        status=request.data.get("status") or AttributeValue.Status.ACTIVE,
    )
    return Response(
        {
            "id": str(row.pk),
            "value": row.value,
            "sortOrder": row.sort_order,
            "status": row.status,
            "attribute_id": str(row.attribute_id),
        },
        status=201,
    )


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_attribute_value_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = AttributeValue.objects.select_related("attribute").filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "value" in request.data and request.data.get("value") is not None:
        val = (request.data.get("value") or "").strip()
        if row.attribute.type == Attribute.Type.COLOR:
            color = _validate_hex_color(val)
            if color is None:
                return Response({"detail": "value must be a valid hex color (#RGB or #RRGGBB)"}, status=400)
            val = color
        row.value = val
    if "sort_order" in request.data:
        row.sort_order = int(request.data.get("sort_order") or 0)
    if "status" in request.data:
        row.status = request.data.get("status")
    row.save()
    return Response(
        {
            "id": str(row.pk),
            "value": row.value,
            "sortOrder": row.sort_order,
            "status": row.status,
            "attribute_id": str(row.attribute_id),
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_unit_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    short_name = (request.data.get("short_name") or "").strip()
    if not name or not short_name:
        return Response({"detail": "name and short_name are required"}, status=400)
    row = Unit.objects.create(
        name=name,
        short_name=short_name,
        type=request.data.get("type") or Unit.Type.QUANTITY,
        conversion=request.data.get("conversion") or "",
        status=request.data.get("status") or Unit.Status.ACTIVE,
    )
    return Response({"id": str(row.pk), "name": row.name}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_unit_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Unit.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    for field in ("name", "short_name", "type", "conversion", "status"):
        if field in request.data:
            setattr(row, field, request.data.get(field))
    row.save()
    return Response({"id": str(row.pk), "name": row.name})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_purchase_order_detail(request, pk):
    if err := _forbidden(request):
        return err
    po = (
        PurchaseOrder.objects.select_related("customer", "seller")
        .prefetch_related("lines__product")
        .filter(pk=pk)
        .first()
    )
    if not po:
        return Response({"detail": "Not found."}, status=404)
    lines = [
        {
            "product_id": str(ln.product_id),
            "name": ln.product.name,
            "sku": ln.product.sku,
            "quantity": ln.quantity,
            "unit_price": float(ln.unit_price),
            "line_total": float(ln.line_total),
            "image_url": absolute_media_url(request, ln.product.image),
        }
        for ln in po.lines.all()
    ]
    return Response(
        {
            "id": po.po_number,
            "pk": po.pk,
            "customer": po.customer.name if po.customer_id else "Walk-in",
            "customer_id": str(po.customer_id) if po.customer_id else "",
            "seller": po.seller.store_name if po.seller_id else "In-House",
            "subtotal": float(po.subtotal),
            "tax": float(po.tax),
            "discount": float(po.discount),
            "delivery_fee": float(po.delivery_fee),
            "total": float(po.total),
            "payment_method": po.payment_method,
            "status": po.status,
            "date": po.created_at.date().isoformat(),
            "lines": lines,
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_pos_order_billing_detail(request, pk):
    """Invoice-shaped detail for a POS Order (same JSON shape as admin_purchase_order_detail)."""
    if err := _forbidden(request):
        return err
    order = (
        Order.objects.select_related("customer", "seller")
        .prefetch_related("items__product")
        .filter(pk=pk, is_pos_order=True)
        .first()
    )
    if not order:
        return Response({"detail": "Not found."}, status=404)
    tax = order.total - order.subtotal + order.discount_amount - order.delivery_fee
    if tax < 0:
        tax = Decimal("0")
    lines = [
        {
            "product_id": str(ln.product_id),
            "name": ln.product.name,
            "sku": ln.product.sku,
            "quantity": ln.quantity,
            "unit_price": float(ln.unit_price),
            "line_total": float(ln.total_price),
            "image_url": absolute_media_url(request, ln.product.image),
        }
        for ln in order.items.all()
    ]
    return Response(
        {
            "id": order.order_number,
            "pk": order.pk,
            "customer": order.customer.name if order.customer_id else "Walk-in",
            "customer_id": str(order.customer_id) if order.customer_id else "",
            "seller": order.seller.store_name if order.seller_id else "Admin",
            "subtotal": float(order.subtotal),
            "tax": float(tax.quantize(Decimal("0.01"))),
            "discount": float(order.discount_amount),
            "delivery_fee": float(order.delivery_fee),
            "total": float(order.total),
            "payment_method": order.payment_method,
            "status": order.status,
            "date": order.created_at.date().isoformat(),
            "lines": lines,
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_purchase_order_create(request):
    if err := _forbidden(request):
        return err
    po_number = f"PO-{timezone.now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
    customer = None
    raw_customer = request.data.get("customer_id")
    if raw_customer not in (None, ""):
        try:
            customer_pk = int(str(raw_customer).strip())
        except (TypeError, ValueError):
            return validation_error("customer_id must be a valid integer", field="customer_id")
        customer = User.objects.filter(pk=customer_pk).first()
        if not customer:
            return validation_error("customer not found", field="customer_id")
    seller = None
    raw_seller = request.data.get("seller_id")
    if raw_seller not in (None, ""):
        try:
            seller_pk = int(str(raw_seller).strip())
        except (TypeError, ValueError):
            return validation_error("seller_id must be a valid integer", field="seller_id")
        seller = Vendor.objects.filter(pk=seller_pk).first()
        if not seller:
            return validation_error("seller not found", field="seller_id")

    items_raw = request.data.get("items")
    if not isinstance(items_raw, list) or len(items_raw) == 0:
        return validation_error("items must be a non-empty list", field="items")

    lines_payload: list[tuple[Product, int, Decimal, Decimal]] = []
    subtotal = Decimal("0")
    for raw in items_raw:
        if not isinstance(raw, dict):
            return validation_error("each item must be an object", field="items")
        pid = raw.get("product_id")
        try:
            qty = int(raw.get("quantity") or 0)
        except (TypeError, ValueError):
            return validation_error("invalid quantity", field="items")
        if pid is None or qty < 1:
            return validation_error("each item needs product_id and quantity >= 1", field="items")
        prod = Product.objects.filter(pk=pid).first()
        if not prod:
            return validation_error("product not found", field="items")
        up_raw = raw.get("unit_price")
        if up_raw is None or up_raw == "":
            unit_price = effective_unit_price(prod)
        else:
            unit_price = _to_decimal(up_raw, "0")
        line_total = unit_price * qty
        subtotal += line_total
        lines_payload.append((prod, qty, unit_price, line_total))

    delivery_fee = _to_decimal(request.data.get("delivery_fee"), "0")
    discount = _to_decimal(request.data.get("discount"), "0")
    if discount > subtotal:
        discount = subtotal
    tax = Decimal("0")
    total = subtotal - discount + delivery_fee

    try:
        with transaction.atomic():
            row = PurchaseOrder.objects.create(
                po_number=po_number,
                customer=customer,
                seller=seller,
                subtotal=subtotal,
                tax=tax,
                discount=discount,
                delivery_fee=delivery_fee,
                total=total,
                payment_method=request.data.get("payment_method") or PurchaseOrder.PaymentMethod.CASH,
                status=PurchaseOrder.Status.COMPLETED,
            )
            for prod, qty, unit_price, line_total in lines_payload:
                PurchaseOrderLine.objects.create(
                    purchase_order=row,
                    product=prod,
                    quantity=qty,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            po_service.complete_purchase_order(row)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response({"id": row.po_number, "pk": row.pk}, status=201)


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_pos_checkout(request):
    if err := _forbidden(request):
        return err
    if not pos_checkout_allowed():
        return pos_disabled_response()
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
        try:
            customer_pk = int(str(raw_cid).strip())
        except (TypeError, ValueError):
            return validation_error("customer_id must be a valid integer", field="customer_id")
        customer = User.objects.filter(pk=customer_pk).first()
        if not customer:
            return validation_error("customer not found", field="customer_id")

    tax_percent = _to_decimal(request.data.get("tax_percent"), "0")
    discount = _to_decimal(request.data.get("discount"), "0")
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
            acting_vendor=None,
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


def _parse_admin_datetime(val):
    if not val:
        return None
    s = str(val).strip().replace("Z", "+00:00")
    dt = parse_datetime(s)
    if dt is None:
        return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _flash_deal_refresh_status(row: FlashDeal) -> None:
    now = timezone.now()
    if row.end_at <= now:
        row.status = FlashDeal.Status.EXPIRED
    elif row.start_at > now:
        row.status = FlashDeal.Status.SCHEDULED
    else:
        row.status = FlashDeal.Status.ACTIVE


def _flash_deal_set_products(deal: FlashDeal, product_ids) -> None:
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
        if Product.objects.filter(pk=pid).exists():
            FlashDealProduct.objects.create(flash_deal=deal, product_id=pid)


def _coupon_set_products(coupon: Coupon, product_ids) -> None:
    if product_ids is None:
        return
    if not isinstance(product_ids, list):
        return
    pids: list[int] = []
    for raw in product_ids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if Product.objects.filter(pk=pid).exists():
            pids.append(pid)
    coupon.products.set(pids)


def _notification_recipient_qs(target: str):
    if target == Notification.Target.VENDORS:
        return User.objects.filter(vendor_profile__isnull=False).distinct()
    if target == Notification.Target.CUSTOMERS:
        return User.objects.filter(
            role__in=[User.Role.NORMAL, User.Role.PARENT, User.Role.CHILD]
        )
    if target == Notification.Target.ADMINS:
        return User.objects.filter(Q(is_staff=True) | Q(role=User.Role.SUPER_ADMIN))
    return User.objects.all()


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_flash_deal_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required")
    start_at = _parse_admin_datetime(request.data.get("start_at") or request.data.get("startDate"))
    end_at = _parse_admin_datetime(request.data.get("end_at") or request.data.get("endDate"))
    if not start_at or not end_at:
        return validation_error("start_at and end_at are required (ISO datetime)")
    row = FlashDeal(
        name=name,
        discount_percent=_to_decimal(request.data.get("discount_percent") or request.data.get("discount"), "0"),
        start_at=start_at,
        end_at=end_at,
        priority=int(request.data.get("priority") or 0),
        status=FlashDeal.Status.SCHEDULED,
    )
    _flash_deal_refresh_status(row)
    row.save()
    _flash_deal_set_products(row, request.data.get("product_ids"))
    audit_service.log(
        f"Created flash deal {name!r} (id={row.pk})",
        log_type=AuditLog.Type.MARKETING,
        performed_by=request.user,
        object_type="FlashDeal",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.CREATE,
        module="flash_deals",
        metadata={"name": name},
    )
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_flash_deal_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = FlashDeal.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        rid, rname = str(row.pk), row.name
        row.delete()
        audit_service.log(
            f"Deleted flash deal {rname!r} (id={rid})",
            log_type=AuditLog.Type.MARKETING,
            performed_by=request.user,
            object_type="FlashDeal",
            object_id=rid,
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="flash_deals",
        )
        return Response({"ok": True})
    if "name" in request.data:
        row.name = (request.data.get("name") or "").strip() or row.name
    if "discount_percent" in request.data or "discount" in request.data:
        row.discount_percent = _to_decimal(
            request.data.get("discount_percent") or request.data.get("discount"), "0"
        )
    if "start_at" in request.data or "startDate" in request.data:
        v = _parse_admin_datetime(request.data.get("start_at") or request.data.get("startDate"))
        if v:
            row.start_at = v
    if "end_at" in request.data or "endDate" in request.data:
        v = _parse_admin_datetime(request.data.get("end_at") or request.data.get("endDate"))
        if v:
            row.end_at = v
    if "priority" in request.data:
        row.priority = int(request.data.get("priority") or 0)
    _flash_deal_refresh_status(row)
    row.save()
    _flash_deal_set_products(row, request.data.get("product_ids"))
    audit_service.log(
        f"Updated flash deal {row.name!r} (id={row.pk})",
        log_type=AuditLog.Type.MARKETING,
        performed_by=request.user,
        object_type="FlashDeal",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="flash_deals",
    )
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_coupon_create(request):
    if err := _forbidden(request):
        return err
    code = (request.data.get("code") or "").strip().upper()
    if not code:
        return validation_error("code is required")
    if Coupon.objects.filter(code__iexact=code).exists():
        return validation_error("code already exists")
    vendor = None
    vid = request.data.get("vendor_id")
    if vid:
        vendor = Vendor.objects.filter(pk=vid).first()
    cat = None
    cid = request.data.get("category_id")
    if cid:
        cat = Category.objects.filter(pk=cid).first()
    exp_raw = request.data.get("expires_at") or request.data.get("expires")
    expires_at = _parse_admin_datetime(exp_raw) if exp_raw else None
    row = Coupon.objects.create(
        code=code,
        type=request.data.get("type") or Coupon.Type.PERCENTAGE,
        value=_to_decimal(request.data.get("value"), "0"),
        min_order=_to_decimal(request.data.get("min_order") or request.data.get("minOrder"), "0"),
        usage_limit=_parse_coupon_usage_limit(request.data.get("usage_limit")),
        status=request.data.get("status") or Coupon.Status.ACTIVE,
        expires_at=expires_at,
        vendor=vendor,
        category=cat,
    )
    _coupon_set_products(row, request.data.get("product_ids"))
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_coupon_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Coupon.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "code" in request.data:
        c = (request.data.get("code") or "").strip().upper()
        if c and not Coupon.objects.filter(code__iexact=c).exclude(pk=row.pk).exists():
            row.code = c
    for f, alt in (
        ("type", None),
        ("value", None),
        ("min_order", "minOrder"),
        ("status", None),
    ):
        keys = [f] + ([alt] if alt else [])
        for k in keys:
            if k and k in request.data:
                if f == "value" or f == "min_order":
                    setattr(row, f, _to_decimal(request.data.get(k), "0"))
                else:
                    setattr(row, f, request.data.get(k))
                break
    if "usage_limit" in request.data:
        row.usage_limit = _parse_coupon_usage_limit(request.data.get("usage_limit"))
    if "expires_at" in request.data or "expires" in request.data:
        raw = request.data.get("expires_at") or request.data.get("expires")
        row.expires_at = _parse_admin_datetime(raw) if raw else None
    if "vendor_id" in request.data:
        vid = request.data.get("vendor_id")
        row.vendor = Vendor.objects.filter(pk=vid).first() if vid else None
    if "category_id" in request.data:
        cid = request.data.get("category_id")
        row.category = Category.objects.filter(pk=cid).first() if cid else None
    row.save()
    if "product_ids" in request.data:
        _coupon_set_products(row, request.data.get("product_ids"))
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_notification_broadcast(request):
    if err := _forbidden(request):
        return err
    title = (request.data.get("title") or "").strip()
    message = (request.data.get("message") or "").strip()
    if not title or not message:
        return validation_error("title and message are required")
    target = request.data.get("target") or Notification.Target.ALL
    if target not in dict(Notification.Target.choices):
        return validation_error("invalid target")
    ntype = request.data.get("type") or Notification.Type.MARKETING
    if ntype not in dict(Notification.Type.choices):
        ntype = Notification.Type.MARKETING
    from core.services.fcm_device_service import fcm_tokens_for_user

    qs = _notification_recipient_qs(target)
    MAX_ROWS = 2500
    batch = []
    count = 0
    fcm_tokens: list[str] = []
    for u in qs.iterator(chunk_size=500):
        batch.append(
            Notification(
                title=title,
                message=message,
                type=ntype,
                target=target,
                recipient=u,
                is_read=False,
            )
        )
        fcm_tokens.extend(fcm_tokens_for_user(u))
        count += 1
        if len(batch) >= 400:
            Notification.objects.bulk_create(batch)
            batch = []
        if count >= MAX_ROWS:
            break
    if batch:
        Notification.objects.bulk_create(batch)
    from core.services.fcm_push_service import send_fcm_to_tokens

    push = send_fcm_to_tokens(fcm_tokens, title, message)
    return Response(
        {
            "created": min(count, MAX_ROWS),
            "capped": count > MAX_ROWS,
            "push": {
                "firebase_configured": push.firebase_configured,
                "device_tokens": push.unique_tokens,
                "delivered": push.success_count,
                "failed": push.failure_count,
                "skip_reason": push.skip_reason,
                "first_error": push.first_error,
            },
        },
        status=201,
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_notification_push(request, pk):
    """Resend FCM push for an existing notification (recipient or target audience)."""
    if err := _forbidden(request):
        return err
    row = Notification.objects.select_related("recipient").filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    from core.services.fcm_device_service import fcm_tokens_for_user
    from core.services.fcm_push_service import send_fcm_to_tokens

    fcm_tokens: list[str] = []
    if row.recipient_id:
        fcm_tokens.extend(fcm_tokens_for_user(row.recipient))
    else:
        for u in _notification_recipient_qs(row.target).iterator(chunk_size=500):
            fcm_tokens.extend(fcm_tokens_for_user(u))

    push = send_fcm_to_tokens(
        fcm_tokens,
        row.title,
        row.message,
        image_url=(row.image_url or "").strip(),
    )
    return Response(
        {
            "push": {
                "firebase_configured": push.firebase_configured,
                "device_tokens": push.unique_tokens,
                "delivered": push.success_count,
                "failed": push.failure_count,
                "skip_reason": push.skip_reason,
                "first_error": push.first_error,
            },
        }
    )


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_notification_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Notification.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "title" in request.data:
        row.title = (request.data.get("title") or "").strip() or row.title
    if "message" in request.data:
        row.message = (request.data.get("message") or "").strip() or row.message
    if "is_read" in request.data:
        row.is_read = bool(request.data.get("is_read"))
    row.save()
    return Response({"id": str(row.pk)})


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_product_approval_write(request, pk):
    if err := _forbidden(request):
        return err
    row = (
        ProductApproval.objects.select_related("product", "vendor", "vendor__user")
        .filter(pk=pk)
        .first()
    )
    if not row:
        return Response({"detail": "Not found."}, status=404)
    status = request.data.get("status")
    if status not in (
        ProductApproval.Status.APPROVED,
        ProductApproval.Status.DENIED,
        ProductApproval.Status.PENDING,
    ):
        return validation_error("invalid status")
    row.status = status
    row.reviewed_at = timezone.now()
    row.reviewer = request.user
    if "rejection_reason" in request.data:
        row.rejection_reason = (request.data.get("rejection_reason") or "")[:2000]
    row.save()
    if status == ProductApproval.Status.DENIED and row.vendor.user_id:
        reason = (row.rejection_reason or "").strip() or "No reason provided."
        Notification.objects.create(
            title="Product submission not approved",
            message=f'Your product "{row.product.name}" was not approved. Reason: {reason[:1500]}',
            type=Notification.Type.SYSTEM,
            target=Notification.Target.VENDORS,
            recipient_id=row.vendor.user_id,
            action_url="/vendor/all-products",
        )
    ip = client_ip_from_request(request)
    audit_service.log(
        f"Product approval {status}: {row.product.name} (vendor {row.vendor.store_name})",
        log_type=AuditLog.Type.PRODUCT,
        performed_by=request.user,
        object_type="ProductApproval",
        object_id=str(row.pk),
        ip_address=ip,
        action_kind=AuditLog.ActionKind.UPDATE,
        module="product_approvals",
        metadata={
            "product_id": str(row.product_id),
            "vendor_id": str(row.vendor_id),
            "status": status,
        },
    )
    return Response({"id": str(row.pk), "status": row.status})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_impersonate(request, pk):
    """
    Issue the vendor owner's API token so an authenticated admin can open the vendor SPA as that seller.
    Logged in VendorImpersonationLog and audit trail.
    """
    if err := _forbidden(request):
        return err
    vendor = Vendor.objects.filter(pk=pk).select_related("user").first()
    if not vendor:
        return Response({"detail": "Not found."}, status=404)
    if not vendor.user.is_active:
        return Response({"detail": "Vendor account is inactive."}, status=400)
    token, _ = Token.objects.get_or_create(user=vendor.user)
    session_key = str(uuid4())
    VendorImpersonationLog.objects.create(
        admin=request.user,
        vendor=vendor,
        session_token=session_key[:100],
        expires_at=timezone.now() + timedelta(hours=12),
    )
    ip = client_ip_from_request(request)
    audit_service.log(
        "Admin vendor impersonation",
        log_type=AuditLog.Type.SECURITY,
        performed_by=request.user,
        action_kind=AuditLog.ActionKind.LOGIN,
        module="vendors",
        ip_address=ip,
        metadata={
            "vendor_id": str(vendor.pk),
            "impersonation_session": session_key[:36],
            "vendor_user_id": str(vendor.user_id),
        },
    )
    return Response(
        {
            "token": token.key,
            "user": {
                "id": vendor.user.id,
                "name": vendor.user.name,
                "store_name": vendor.store_name,
                "store_slug": vendor.store_slug,
                "status": vendor.status,
            },
        }
    )


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_vendor_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Vendor.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    for field in (
        "store_name",
        "description",
        "contact_email",
        "phone",
        "address",
        "status",
        "commission_rate",
        "rejection_reason",
        "is_verified",
        "can_post",
        "can_sell",
        "pos_enabled",
    ):
        if field in request.data:
            val = request.data.get(field)
            if field in ("is_verified", "can_post", "can_sell", "pos_enabled"):
                setattr(row, field, val in (True, "true", "1", 1))
            elif field == "commission_rate":
                setattr(row, field, _to_decimal(val, str(row.commission_rate)))
            else:
                setattr(row, field, val)
    owner_name = (request.data.get("owner_name") or "").strip()
    if owner_name:
        row.user.name = owner_name[:150]
        row.user.save(update_fields=["name"])
    if "owner_phone" in request.data:
        new_phone = (request.data.get("owner_phone") or "").strip()
        if new_phone and new_phone != row.user.phone:
            if User.objects.filter(phone=new_phone).exclude(pk=row.user_id).exists():
                return validation_error("owner phone already in use", field="owner_phone")
            row.user.phone = new_phone[:15]
            row.user.username = new_phone[:15]
            row.user.save(update_fields=["phone", "username"])
        if new_phone:
            row.phone = new_phone[:15]
    if "logo" in request.FILES:
        row.logo = request.FILES["logo"]
    if "banner" in request.FILES:
        row.banner = request.FILES["banner"]
    row.save()
    if row.status == Vendor.Status.APPROVED:
        ensure_vendor_wallet(row)
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_role_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required")
    row = Role.objects.create(
        name=name,
        permissions=request.data.get("permissions") if isinstance(request.data.get("permissions"), dict) else {},
        status=request.data.get("status") or Role.Status.ACTIVE,
        is_system=False,
    )
    audit_service.log(
        f"Created role {name!r} (id={row.pk})",
        log_type=AuditLog.Type.SETTINGS,
        performed_by=request.user,
        object_type="Role",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.CREATE,
        module="roles",
        metadata={"role_id": str(row.pk)},
    )
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_role_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Role.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        if row.is_system:
            return validation_error("cannot delete system role", status=400)
        if EmployeeProfile.objects.filter(role=row).exists():
            return validation_error("role is in use", status=400)
        rid, rname = str(row.pk), row.name
        row.delete()
        audit_service.log(
            f"Deleted role {rname!r} (id={rid})",
            log_type=AuditLog.Type.SETTINGS,
            performed_by=request.user,
            object_type="Role",
            object_id=rid,
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="roles",
            metadata={"role_id": rid},
        )
        return Response({"ok": True})
    if row.is_system and "permissions" not in request.data:
        pass
    elif "permissions" in request.data and isinstance(request.data.get("permissions"), dict):
        row.permissions = request.data.get("permissions")
    if "name" in request.data and not row.is_system:
        row.name = (request.data.get("name") or "").strip() or row.name
    if "status" in request.data:
        row.status = request.data.get("status")
    row.save()
    audit_service.log(
        f"Updated role {row.name!r} (id={row.pk})",
        log_type=AuditLog.Type.SETTINGS,
        performed_by=request.user,
        object_type="Role",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="roles",
        metadata={"role_id": str(row.pk)},
    )
    return Response(
        {
            "id": str(row.pk),
            "name": row.name,
            "status": row.status,
            "permissions": row.permissions,
            "is_system": row.is_system,
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_employee_create(request):
    if err := _forbidden(request):
        return err
    role_id = request.data.get("role_id")
    role = Role.objects.filter(pk=role_id).first()
    if not role:
        return validation_error("role_id is required")
    user = None
    if request.data.get("user_id"):
        user = User.objects.filter(pk=request.data.get("user_id")).first()
    if not user:
        phone = (request.data.get("phone") or "").strip()
        name = (request.data.get("name") or "").strip()
        if not phone or not name:
            return validation_error("user_id or (phone + name) required")
        if User.objects.filter(phone=phone).exists():
            return validation_error("phone already registered")
        user = User.objects.create_user(
            username=phone,
            email=(request.data.get("email") or "").strip(),
            password=request.data.get("password") or None,
            name=name,
            phone=phone,
            role=User.Role.STAFF,
            is_staff=True,
        )
        if not request.data.get("password"):
            user.set_unusable_password()
            user.save(update_fields=["password"])
    if EmployeeProfile.objects.filter(user=user).exists():
        return validation_error("user already has employee profile")
    ep = EmployeeProfile.objects.create(
        user=user,
        role=role,
        status=request.data.get("status") or EmployeeProfile.Status.ACTIVE,
        modules_access=request.data.get("modules_access")
        if isinstance(request.data.get("modules_access"), list)
        else [],
    )
    audit_service.log(
        f"Created employee profile for {user.name} (id={ep.pk})",
        log_type=AuditLog.Type.USER,
        performed_by=request.user,
        object_type="EmployeeProfile",
        object_id=str(ep.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.CREATE,
        module="employees",
        metadata={"employee_profile_id": str(ep.pk), "user_id": str(user.pk)},
    )
    return Response({"id": str(ep.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_employee_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = EmployeeProfile.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        eid, uname = str(row.pk), row.user.name
        row.delete()
        audit_service.log(
            f"Deleted employee profile id={eid} ({uname})",
            log_type=AuditLog.Type.USER,
            performed_by=request.user,
            object_type="EmployeeProfile",
            object_id=eid,
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="employees",
            metadata={"employee_profile_id": eid},
        )
        return Response({"ok": True})
    if "role_id" in request.data:
        r = Role.objects.filter(pk=request.data.get("role_id")).first()
        if r:
            row.role = r
    if "status" in request.data:
        row.status = request.data.get("status")
    if "modules_access" in request.data and isinstance(request.data.get("modules_access"), list):
        row.modules_access = request.data.get("modules_access")
    row.save()

    u = row.user
    user_fields: list[str] = []
    if "name" in request.data:
        nm = (request.data.get("name") or "").strip()
        if nm:
            u.name = nm[:150]
            user_fields.append("name")
    if "email" in request.data:
        u.email = (request.data.get("email") or "").strip()[:254]
        user_fields.append("email")
    if "phone" in request.data:
        new_phone = (request.data.get("phone") or "").strip()
        if new_phone and new_phone != u.phone:
            if User.objects.filter(phone=new_phone).exclude(pk=u.pk).exists():
                return validation_error("phone already in use", field="phone")
            u.phone = new_phone[:15]
            u.username = new_phone[:15]
            user_fields.extend(["phone", "username"])
    pwd = request.data.get("password")
    if pwd is not None and str(pwd).strip():
        u.set_password(str(pwd).strip())
        user_fields.append("password")
    if user_fields:
        u.save(update_fields=list(dict.fromkeys(user_fields)))

    parts = ["Updated employee profile"]
    if user_fields:
        parts.append("user fields: " + ", ".join(user_fields))
    if "role_id" in request.data or "status" in request.data or "modules_access" in request.data:
        parts.append("profile/role/modules updated")
    audit_service.log(
        f"{'; '.join(parts)} (id={row.pk}, {row.user.name})",
        log_type=AuditLog.Type.USER,
        performed_by=request.user,
        object_type="EmployeeProfile",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="employees",
        metadata={"employee_profile_id": str(row.pk), "user_id": str(row.user_id)},
    )
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_delivery_man_create(request):
    if err := _forbidden(request):
        return err
    user, uerr = resolve_user_by_pk_or_phone(request.data.get("user_id"), "user_id")
    if uerr:
        return uerr
    if DeliveryMan.objects.filter(user=user).exists():
        return validation_error("delivery profile already exists for user")
    zone = None
    zid, zid_err = parse_int_pk(request.data.get("zone_id"), "zone_id")
    if zid_err:
        return zid_err
    if zid is not None:
        zone = ShippingZone.objects.filter(pk=zid).first()
        if not zone:
            return validation_error("zone not found", field="zone_id")
    row = DeliveryMan.objects.create(
        user=user,
        zone=zone,
        status=request.data.get("status") or DeliveryMan.Status.ACTIVE,
        emergency_contact=(request.data.get("emergency_contact") or "")[:15],
        license_number=(request.data.get("license_number") or "")[:50],
    )
    if request.FILES.get("id_document_front"):
        row.id_document_front = request.FILES["id_document_front"]
    if request.FILES.get("id_document_back"):
        row.id_document_back = request.FILES["id_document_back"]
    if request.FILES.get("selfie"):
        row.selfie = request.FILES["selfie"]
    row.save()
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_delivery_man_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = DeliveryMan.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "zone_id" in request.data:
        zid, zid_err = parse_int_pk(request.data.get("zone_id"), "zone_id")
        if zid_err:
            return zid_err
        if zid is None:
            row.zone = None
        else:
            z = ShippingZone.objects.filter(pk=zid).first()
            if not z:
                return validation_error("zone not found", field="zone_id")
            row.zone = z
    if "status" in request.data:
        row.status = request.data.get("status")
    if "emergency_contact" in request.data:
        row.emergency_contact = (request.data.get("emergency_contact") or "")[:15]
    if "license_number" in request.data:
        row.license_number = (request.data.get("license_number") or "")[:50]
    if request.FILES.get("id_document_front"):
        row.id_document_front = request.FILES["id_document_front"]
    if request.FILES.get("id_document_back"):
        row.id_document_back = request.FILES["id_document_back"]
    if request.FILES.get("selfie"):
        row.selfie = request.FILES["selfie"]
    row.save()
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_shipping_method_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required")
    row = ShippingMethod.objects.create(
        name=name,
        type=request.data.get("type") or ShippingMethod.Type.FLAT,
        cost=_to_decimal(request.data.get("cost"), "0"),
        free_threshold=_to_decimal(request.data.get("free_threshold") or request.data.get("threshold"), "0"),
        status=request.data.get("status") or ShippingMethod.Status.ACTIVE,
    )
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_shipping_method_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = ShippingMethod.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "name" in request.data:
        row.name = (request.data.get("name") or "").strip() or row.name
    if "type" in request.data:
        row.type = request.data.get("type")
    if "cost" in request.data:
        row.cost = _to_decimal(request.data.get("cost"), "0")
    if "free_threshold" in request.data or "threshold" in request.data:
        row.free_threshold = _to_decimal(
            request.data.get("free_threshold") or request.data.get("threshold"), "0"
        )
    if "status" in request.data:
        row.status = request.data.get("status")
    row.save()
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_shipping_zone_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required")
    row = ShippingZone.objects.create(
        name=name,
        areas=request.data.get("areas") or "",
        flat_rate=_to_decimal(request.data.get("flat_rate") or request.data.get("flatRate"), "0"),
        free_above=_to_decimal(request.data.get("free_above") or request.data.get("freeAbove"), "0")
        if request.data.get("free_above") not in ("", None)
        or request.data.get("freeAbove") not in ("", None)
        else None,
        status=request.data.get("status") or ShippingZone.Status.ACTIVE,
    )
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_shipping_zone_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = ShippingZone.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "name" in request.data:
        row.name = (request.data.get("name") or "").strip() or row.name
    if "areas" in request.data:
        row.areas = request.data.get("areas") or ""
    if "flat_rate" in request.data or "flatRate" in request.data:
        row.flat_rate = _to_decimal(
            request.data.get("flat_rate") or request.data.get("flatRate"), "0"
        )
    if "free_above" in request.data or "freeAbove" in request.data:
        fa = request.data.get("free_above")
        if fa is None:
            fa = request.data.get("freeAbove")
        row.free_above = _to_decimal(fa, "0") if fa not in ("", None) else None
    if "status" in request.data:
        row.status = request.data.get("status")
    row.save()
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_weight_rule_create(request):
    if err := _forbidden(request):
        return err
    zid = request.data.get("zone_id")
    zone = ShippingZone.objects.filter(pk=zid).first()
    if not zone:
        return validation_error("zone_id is required")
    row = WeightRule.objects.create(
        zone=zone,
        min_weight=_to_decimal(request.data.get("min_weight") or request.data.get("minWeight"), "0"),
        max_weight=_to_decimal(request.data.get("max_weight") or request.data.get("maxWeight"), "0"),
        rate_per_kg=_to_decimal(request.data.get("rate_per_kg") or request.data.get("ratePerKg"), "0"),
    )
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_weight_rule_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = WeightRule.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "zone_id" in request.data:
        z = ShippingZone.objects.filter(pk=request.data.get("zone_id")).first()
        if z:
            row.zone = z
    if "min_weight" in request.data or "minWeight" in request.data:
        row.min_weight = _to_decimal(
            request.data.get("min_weight") or request.data.get("minWeight"), "0"
        )
    if "max_weight" in request.data or "maxWeight" in request.data:
        row.max_weight = _to_decimal(
            request.data.get("max_weight") or request.data.get("maxWeight"), "0"
        )
    if "rate_per_kg" in request.data or "ratePerKg" in request.data:
        row.rate_per_kg = _to_decimal(
            request.data.get("rate_per_kg") or request.data.get("ratePerKg"), "0"
        )
    row.save()
    return Response({"id": str(row.pk)})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallets_summary(request):
    if err := _forbidden(request):
        return err
    agg = Wallet.objects.aggregate(total=Sum("balance"))
    total_bal = float(agg["total"] or 0)
    done = WalletTransaction.Status.COMPLETED
    tx_done = WalletTransaction.objects.filter(status=done)
    credit_types = (
        WalletTransaction.Type.CREDIT,
        WalletTransaction.Type.TOPUP,
        WalletTransaction.Type.BONUS,
        WalletTransaction.Type.REFUND_CREDIT,
        WalletTransaction.Type.REFUND_PLATFORM_FEE,
    )
    debit_types = (
        WalletTransaction.Type.DEBIT,
        WalletTransaction.Type.PURCHASE,
        WalletTransaction.Type.WITHDRAWAL,
        WalletTransaction.Type.REFUND_VENDOR_DEBIT,
        WalletTransaction.Type.REFUND_PLATFORM_DEBIT,
    )
    total_credit = float(
        tx_done.filter(type__in=credit_types).aggregate(t=Sum("amount"))["t"] or 0
    )
    total_debit = float(
        tx_done.filter(type__in=debit_types).aggregate(t=Sum("amount"))["t"] or 0
    )
    bonus_txn_sum = float(
        tx_done.filter(type=WalletTransaction.Type.BONUS).aggregate(t=Sum("amount"))["t"] or 0
    )
    bonus_rules_active = WalletBonus.objects.filter(status=WalletBonus.Status.ACTIVE).count()
    bonus_used_total = int(
        WalletBonus.objects.aggregate(t=Sum("used_count"))["t"] or 0
    )
    referral_count = User.objects.filter(referred_by__isnull=False).count()
    return Response(
        {
            "total_wallets": Wallet.objects.count(),
            "total_balance": total_bal,
            "frozen_wallets": Wallet.objects.filter(status=Wallet.Status.FROZEN).count(),
            "flagged_transactions": WalletTransaction.objects.filter(
                status=WalletTransaction.Status.FLAGGED
            ).count(),
            "active_loyalty_rules": LoyaltyRule.objects.filter(status=LoyaltyRule.Status.ACTIVE).count(),
            "points_issued_sum": bonus_txn_sum,
            "total_credit": total_credit,
            "total_debit": total_debit,
            "total_bonus_transactions": bonus_txn_sum,
            "wallet_bonuses_active_count": bonus_rules_active,
            "wallet_bonuses_used_total": bonus_used_total,
            "referral_count": referral_count,
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_adjust(request):
    if err := _forbidden(request):
        return err
    # Intentionally does not use wallet_service: allows balance correction while wallet is frozen.
    wid = request.data.get("wallet_id")
    w = Wallet.objects.filter(pk=wid).first()
    if not w:
        return validation_error("wallet not found")
    amount = abs(_to_decimal(request.data.get("amount"), "0"))
    if amount <= 0:
        return validation_error("amount must be positive")
    direction = (request.data.get("direction") or request.data.get("type") or "credit").lower()
    reason = (request.data.get("reason") or "Manual adjustment")[:255]
    with transaction.atomic():
        w = Wallet.objects.select_for_update().filter(pk=w.pk).first()
        if direction == "debit":
            w.balance -= amount
            if w.balance < 0:
                return validation_error("insufficient balance")
            txn_type = WalletTransaction.Type.DEBIT
        else:
            w.balance += amount
            txn_type = WalletTransaction.Type.CREDIT
        w.save(update_fields=["balance", "updated_at"])
        WalletTransaction.objects.create(
            wallet=w,
            txn_id=new_wallet_txn_id(),
            type=txn_type,
            amount=amount,
            description=reason,
            status=WalletTransaction.Status.COMPLETED,
            performed_by=request.user,
        )
    audit_service.log(
        f"Wallet {w.pk} manual {direction}: {amount} — {reason[:80]}",
        log_type=AuditLog.Type.WALLET,
        performed_by=request.user,
        object_type="Wallet",
        object_id=str(w.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="wallet",
        metadata={
            "wallet_id": str(w.pk),
            "direction": direction,
            "amount": str(amount),
        },
    )
    return Response({"ok": True, "balance": float(w.balance)})


def _admin_wallet_detail_dict(row: Wallet) -> dict:
    label = "—"
    fam = "—"
    family_group_id: int | None = None
    if row.vendor_id:
        label = row.vendor.store_name
    elif row.owner_id:
        label = row.owner.name
    if row.family_group_id:
        fam = row.family_group.name
        family_group_id = row.family_group.pk
    return {
        "id": str(row.pk),
        "owner": label,
        "type": row.type,
        "label": row.label or "",
        "balance": float(row.balance),
        "currency": row.currency,
        "status": row.status,
        "family": fam,
        "family_group_id": family_group_id,
        "lastActivity": row.updated_at.isoformat(),
    }


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = (
        Wallet.objects.filter(pk=pk)
        .select_related("owner", "vendor", "family_group")
        .first()
    )
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(_admin_wallet_detail_dict(row))

    data = request.data
    update_fields: list[str] = []
    if "status" in data:
        if not user_can_manage_wallet_freeze(request.user):
            return Response(
                {
                    "detail": "Freezing or unfreezing wallets requires super admin privileges.",
                },
                status=403,
            )
        new_status = data.get("status")
        if new_status not in (Wallet.Status.ACTIVE, Wallet.Status.FROZEN):
            return validation_error("status must be active or frozen", field="status")
        row.status = new_status
        update_fields.append("status")
    if "label" in data:
        row.label = (data.get("label") or "")[:100]
        update_fields.append("label")
    if not update_fields:
        return Response({"id": str(row.pk)})
    row.updated_at = timezone.now()
    update_fields.append("updated_at")
    row.save(update_fields=update_fields)
    audit_service.log(
        f"Updated wallet id={row.pk} ({', '.join(update_fields)})",
        log_type=AuditLog.Type.WALLET,
        performed_by=request.user,
        object_type="Wallet",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="wallet",
        metadata={"wallet_id": str(row.pk)},
    )
    return Response({"id": str(row.pk)})


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_settings_singleton(request):
    if err := _forbidden(request):
        return err
    ws = WalletSettings.load()
    if request.method == "GET":
        return Response(
            {
                "max_balance_per_user": float(ws.max_balance_per_user),
                "daily_transfer_limit": float(ws.daily_transfer_limit),
                "min_withdrawal": float(ws.min_withdrawal),
                "max_withdrawal_per_day": float(ws.max_withdrawal_per_day),
                "transaction_fee_type": ws.transaction_fee_type,
                "transaction_fee_value": float(ws.transaction_fee_value),
                "vendor_settlement_days": ws.vendor_settlement_days,
                "otp_for_withdrawals": ws.otp_for_withdrawals,
                "otp_for_transfers_above": float(ws.otp_for_transfers_above),
                "auto_flag_suspicious": ws.auto_flag_suspicious,
                "shared_wallet_enabled": ws.shared_wallet_enabled,
                "individual_wallet_enabled": ws.individual_wallet_enabled,
                "flat_wallet_enabled": ws.flat_wallet_enabled,
                "vendor_wallet_enabled": ws.vendor_wallet_enabled,
                "family_wallet_enabled": ws.family_wallet_enabled,
                "child_wallet_enabled": ws.child_wallet_enabled,
            }
        )
    numeric = (
        "max_balance_per_user",
        "daily_transfer_limit",
        "min_withdrawal",
        "max_withdrawal_per_day",
        "transaction_fee_value",
        "otp_for_transfers_above",
    )
    for f in numeric:
        if f in request.data:
            v = _to_decimal(request.data.get(f), str(getattr(ws, f)))
            if v < 0:
                v = Decimal("0")
            setattr(ws, f, v)
    if "transaction_fee_type" in request.data:
        tft = str(request.data.get("transaction_fee_type") or "").strip()
        if tft not in (
            WalletSettings.FeeType.FLAT,
            WalletSettings.FeeType.PERCENTAGE,
        ):
            return validation_error("transaction_fee_type must be flat or percentage")
        ws.transaction_fee_type = tft
    if "vendor_settlement_days" in request.data:
        ws.vendor_settlement_days = max(0, int(request.data.get("vendor_settlement_days") or 0))
    bools = (
        "otp_for_withdrawals",
        "auto_flag_suspicious",
        "shared_wallet_enabled",
        "individual_wallet_enabled",
        "flat_wallet_enabled",
        "vendor_wallet_enabled",
        "family_wallet_enabled",
        "child_wallet_enabled",
    )
    for f in bools:
        if f in request.data:
            setattr(ws, f, request.data.get(f) in (True, "true", "1", 1))
    try:
        ws.full_clean()
    except ValidationError as e:
        if getattr(e, "error_dict", None):
            return Response(e.error_dict, status=400)
        return Response({"detail": " ".join(e.messages)}, status=400)
    ws.save()
    return Response({"ok": True})


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_order_settings_singleton(request):
    if err := _forbidden(request):
        return err
    os_row = OrderSettings.load()
    if request.method == "GET":
        return Response(
            {
                "refund_validity_days": os_row.refund_validity_days,
                "auto_cancel_hours": os_row.auto_cancel_hours,
                "guest_checkout": os_row.guest_checkout,
                "order_verification_required": os_row.order_verification_required,
                "auto_assign_delivery": os_row.auto_assign_delivery,
            }
        )
    if "refund_validity_days" in request.data:
        os_row.refund_validity_days = max(
            0, int(request.data.get("refund_validity_days") or 0)
        )
    if "auto_cancel_hours" in request.data:
        os_row.auto_cancel_hours = max(
            0, int(request.data.get("auto_cancel_hours") or 0)
        )
    for f in (
        "guest_checkout",
        "order_verification_required",
        "auto_assign_delivery",
    ):
        if f in request.data:
            setattr(os_row, f, request.data.get(f) in (True, "true", "1", 1))
    os_row.save()
    return Response({"ok": True})


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_site_settings_singleton(request):
    if err := _forbidden(request):
        return err
    site = SiteSettings.load()
    if request.method == "GET":
        return Response(
            {
                "site_name": site.site_name,
                "site_logo_url": absolute_media_url(request, site.site_logo),
                "site_favicon_url": absolute_media_url(request, site.site_favicon)
                if site.site_favicon
                else "",
                "cover_image_url": absolute_media_url(request, site.cover_image)
                if site.cover_image
                else "",
                "site_email": site.site_email or "",
                "phone": site.phone or "",
                "address": site.address or "",
                "currency": site.currency or "NPR",
                "timezone": site.timezone or "Asia/Kathmandu",
                "site_description": site.site_description or "",
                "meta_keywords": site.meta_keywords or "",
                "footer_text": site.footer_text or "",
                "maintenance_mode": site.maintenance_mode,
                "temporary_shop_close": site.temporary_shop_close,
                "new_registrations": site.new_registrations,
                "kyc_required": site.kyc_required,
                "pos_enabled": site.pos_enabled,
                "search_placeholders": site.search_placeholders or [],
                "admin_extras": site.admin_extras or {},
                "smtp_host": site.smtp_host or "",
                "smtp_port": site.smtp_port if site.smtp_port is not None else None,
                "smtp_username": site.smtp_username or "",
                "smtp_from_name": site.smtp_from_name or "",
                "smtp_from_email": site.smtp_from_email or "",
                "smtp_password_set": bool((site.smtp_password or "").strip()),
            }
        )
    if "site_name" in request.data:
        site.site_name = (request.data.get("site_name") or "")[:150] or site.site_name
    if "site_email" in request.data:
        site.site_email = (request.data.get("site_email") or "").strip()[:254]
    if "phone" in request.data:
        site.phone = (request.data.get("phone") or "")[:20]
    if "address" in request.data:
        site.address = request.data.get("address") or ""
    if "currency" in request.data:
        site.currency = (request.data.get("currency") or "NPR")[:10]
    if "timezone" in request.data:
        site.timezone = (request.data.get("timezone") or "Asia/Kathmandu")[:50]
    if "site_description" in request.data:
        site.site_description = request.data.get("site_description") or ""
    if "meta_keywords" in request.data:
        site.meta_keywords = (request.data.get("meta_keywords") or "")[:500]
    if "footer_text" in request.data:
        site.footer_text = (request.data.get("footer_text") or "")[:255]
    for f in (
        "maintenance_mode",
        "temporary_shop_close",
        "new_registrations",
        "kyc_required",
        "pos_enabled",
    ):
        if f in request.data:
            setattr(site, f, request.data.get(f) in (True, "true", "1", 1))
    if "search_placeholders" in request.data:
        sp = request.data.get("search_placeholders")
        if isinstance(sp, list):
            site.search_placeholders = [str(x)[:120] for x in sp][:50]
    if "smtp_host" in request.data:
        site.smtp_host = (request.data.get("smtp_host") or "").strip()[:255]
    if "smtp_port" in request.data:
        raw_port = request.data.get("smtp_port")
        if raw_port in (None, ""):
            site.smtp_port = None
        else:
            try:
                p = int(raw_port)
                site.smtp_port = max(1, min(p, 65535))
            except (TypeError, ValueError):
                pass
    if "smtp_username" in request.data:
        site.smtp_username = (request.data.get("smtp_username") or "").strip()[:255]
    if "smtp_password" in request.data:
        pw = request.data.get("smtp_password")
        if pw is not None and str(pw).strip() != "":
            site.smtp_password = str(pw)[:255]
    if "smtp_from_name" in request.data:
        site.smtp_from_name = (request.data.get("smtp_from_name") or "").strip()[:150]
    if "smtp_from_email" in request.data:
        site.smtp_from_email = (request.data.get("smtp_from_email") or "").strip()[:254]
    if "admin_extras" in request.data:
        ex = request.data.get("admin_extras")
        if isinstance(ex, dict):
            site.admin_extras = _merge_admin_extras(site.admin_extras, ex)
    logo = request.FILES.get("site_logo") or request.FILES.get("logo")
    if logo:
        site.site_logo = logo
    favicon = request.FILES.get("site_favicon") or request.FILES.get("favicon")
    if favicon:
        site.site_favicon = favicon
    cover = request.FILES.get("cover_image")
    if cover:
        site.cover_image = cover
    elif request.data.get("clear_cover_image") in (True, "true", "1", 1):
        if site.cover_image:
            site.cover_image.delete(save=False)
        site.cover_image = None
    site.save()
    return Response({"ok": True})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_payment_gateways_list(request):
    if err := _forbidden(request):
        return err
    wanted = (
        PaymentGatewaySettings.Gateway.ESEWA,
        PaymentGatewaySettings.Gateway.KHALTI,
        PaymentGatewaySettings.Gateway.NCHL_QR,
    )
    by_gw = {r.gateway: r for r in PaymentGatewaySettings.objects.filter(gateway__in=wanted)}
    rows = []
    for gw_key in wanted:
        gw = by_gw.get(gw_key)
        if not gw:
            gw = PaymentGatewaySettings(
                gateway=gw_key,
                is_enabled=False,
                environment=PaymentGatewaySettings.Environment.TEST,
            )
        extras = gw.gateway_extras if isinstance(gw.gateway_extras, dict) else {}
        if gw_key == PaymentGatewaySettings.Gateway.ESEWA:
            rows.append(
                {
                    "gateway": gw.gateway,
                    "label": gw.get_gateway_display(),
                    "is_enabled": gw.is_enabled,
                    "environment": gw.environment,
                    "product_code": gw.merchant_id or "",
                    "secret_key_test": gw.secret_key_test or "",
                    "secret_key_live": gw.secret_key_live or "",
                    "form_url": str(extras.get("form_url") or ""),
                    "status_url_base": str(extras.get("status_url_base") or ""),
                }
            )
        elif gw_key == PaymentGatewaySettings.Gateway.NCHL_QR:
            from core.services import nchl_qr_service

            rows.append(
                {
                    "gateway": gw.gateway,
                    "label": gw.get_gateway_display(),
                    "is_enabled": gw.is_enabled,
                    "is_configured": nchl_qr_service.nchl_qr_is_configured(gw),
                    "environment": gw.environment,
                    "merchant_id": gw.merchant_id or "",
                    "merchant_name": gw.merchant_name or "",
                    "api_key_test": gw.api_key_test or "",
                    "api_key_live": gw.api_key_live or "",
                    "secret_key_test": gw.secret_key_test or "",
                    "secret_key_live": gw.secret_key_live or "",
                    "callback_url": gw.callback_url or "",
                    "qr_expiry_seconds": gw.qr_expiry_seconds,
                    "api_base_url_test": str(extras.get("api_base_url_test") or ""),
                    "api_base_url_live": str(extras.get("api_base_url_live") or ""),
                    "dynamic_qr_path": str(extras.get("dynamic_qr_path") or ""),
                    "status_inquiry_path": str(extras.get("status_inquiry_path") or ""),
                    "merchant_vpa": str(extras.get("merchant_vpa") or ""),
                    "terminal_id": str(extras.get("terminal_id") or ""),
                    "demo_mode": bool(extras.get("demo_mode")),
                }
            )
        else:
            rows.append(
                {
                    "gateway": gw.gateway,
                    "label": gw.get_gateway_display(),
                    "is_enabled": gw.is_enabled,
                    "environment": gw.environment,
                    "secret_key_test": gw.secret_key_test or "",
                    "secret_key_live": gw.secret_key_live or "",
                    "api_base_url": str(extras.get("api_base_url") or ""),
                }
            )
    return Response({"results": rows})


@api_view(["PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_payment_gateway_write(request, gateway: str):
    if err := _forbidden(request):
        return err
    if gateway not in {c[0] for c in PaymentGatewaySettings.Gateway.choices}:
        return Response({"detail": "Unknown gateway."}, status=404)
    row, _ = PaymentGatewaySettings.objects.get_or_create(
        gateway=gateway,
        defaults={"is_enabled": False},
    )
    if "is_enabled" in request.data:
        row.is_enabled = request.data.get("is_enabled") in (True, "true", "1", 1)
    if "environment" in request.data:
        env = request.data.get("environment")
        if env in {c[0] for c in PaymentGatewaySettings.Environment.choices}:
            row.environment = env
    extras = dict(row.gateway_extras) if isinstance(row.gateway_extras, dict) else {}
    if gateway == PaymentGatewaySettings.Gateway.ESEWA:
        if "product_code" in request.data:
            row.merchant_id = (request.data.get("product_code") or "")[:100]
        if "form_url" in request.data:
            extras["form_url"] = (request.data.get("form_url") or "").strip()[:500]
        if "status_url_base" in request.data:
            extras["status_url_base"] = (request.data.get("status_url_base") or "").strip()[:500]
    elif gateway == PaymentGatewaySettings.Gateway.KHALTI:
        if "api_base_url" in request.data:
            extras["api_base_url"] = (request.data.get("api_base_url") or "").strip()[:500]
    elif gateway == PaymentGatewaySettings.Gateway.NCHL_QR:
        if "merchant_id" in request.data:
            row.merchant_id = (request.data.get("merchant_id") or "")[:100]
        if "merchant_name" in request.data:
            row.merchant_name = (request.data.get("merchant_name") or "")[:150]
        for f in ("api_key_live", "api_key_test"):
            if f in request.data:
                setattr(row, f, (request.data.get(f) or "")[:255])
        if "callback_url" in request.data:
            row.callback_url = (request.data.get("callback_url") or "").strip()[:200]
        if "qr_expiry_seconds" in request.data:
            row.qr_expiry_seconds = max(30, int(request.data.get("qr_expiry_seconds") or 300))
        for key in (
            "api_base_url_test",
            "api_base_url_live",
            "dynamic_qr_path",
            "status_inquiry_path",
            "merchant_vpa",
            "terminal_id",
            "acquirer_id",
            "institution_code",
            "source_id",
            "currency",
            "signed_field_names",
            "demo_mode",
        ):
            if key in request.data:
                extras[key] = request.data.get(key)
    else:
        if "merchant_id" in request.data:
            row.merchant_id = (request.data.get("merchant_id") or "")[:100]
        if "merchant_name" in request.data:
            row.merchant_name = (request.data.get("merchant_name") or "")[:150]
        for f in ("api_key_live", "api_key_test"):
            if f in request.data:
                setattr(row, f, (request.data.get(f) or "")[:255])
        if "callback_url" in request.data:
            row.callback_url = (request.data.get("callback_url") or "").strip()[:200]
        if "qr_expiry_seconds" in request.data:
            row.qr_expiry_seconds = max(30, int(request.data.get("qr_expiry_seconds") or 300))
    for f in ("secret_key_live", "secret_key_test"):
        if f in request.data:
            setattr(row, f, (request.data.get(f) or "")[:255])
    row.gateway_extras = extras
    row.save()
    return Response({"gateway": row.gateway, "ok": True})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_db_table_stats(request):
    if err := _forbidden(request):
        return err
    if not getattr(request.user, "is_superuser", False):
        return Response({"detail": "Superuser only."}, status=403)
    from django.apps import apps

    tables = []
    for model in apps.get_models():
        try:
            tables.append(
                {
                    "name": model._meta.label,
                    "table": model._meta.db_table,
                    "count": model.objects.count(),
                }
            )
        except Exception:
            tables.append(
                {
                    "name": model._meta.label,
                    "table": model._meta.db_table,
                    "count": None,
                }
            )
    tables.sort(key=lambda x: x["name"])
    return Response({"tables": tables})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_bonus_create(request):
    if err := _forbidden(request):
        return err
    from django.utils.dateparse import parse_date

    title = (request.data.get("title") or request.data.get("name") or "").strip()
    if not title:
        return validation_error("title is required")
    btype = (request.data.get("type") or WalletBonus.Type.TOPUP).strip()
    if btype not in {c[0] for c in WalletBonus.Type.choices}:
        return validation_error("invalid bonus type", field="type")
    amt = _to_decimal(request.data.get("amount"), "0")
    if amt < 0:
        return validation_error("amount must be non-negative", field="amount")
    exp_raw = request.data.get("expires_at") or request.data.get("expires")
    expires_d = None
    if exp_raw not in (None, ""):
        s = str(exp_raw).strip()
        if len(s) >= 10 and s[4] == "-":
            expires_d = parse_date(s[:10])
        else:
            expires_d = parse_date(s)
        if expires_d is None:
            return validation_error("invalid expires date", field="expires_at")
    is_pct = bool(request.data.get("is_percentage"))
    min_topup = _to_decimal(request.data.get("min_topup") or request.data.get("minTopup"), "0")
    if (
        btype in (WalletBonus.Type.SIGNUP, WalletBonus.Type.REFERRAL)
        and is_pct
        and min_topup <= 0
    ):
        return validation_error(
            "min_topup must be positive for percentage signup or referral bonuses",
            field="min_topup",
        )
    row = WalletBonus.objects.create(
        title=title,
        type=btype,
        amount=amt,
        is_percentage=is_pct,
        min_topup=min_topup,
        status=request.data.get("status") or WalletBonus.Status.ACTIVE,
        expires_at=expires_d,
    )
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_bonus_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = WalletBonus.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "title" in request.data or "name" in request.data:
        row.title = (request.data.get("title") or request.data.get("name") or row.title).strip()
    if "type" in request.data:
        btype = str(request.data.get("type") or "").strip()
        if btype and btype not in {c[0] for c in WalletBonus.Type.choices}:
            return validation_error("invalid bonus type", field="type")
        if btype:
            row.type = btype
    if "amount" in request.data:
        row.amount = _to_decimal(request.data.get("amount"), "0")
        if row.amount < 0:
            return validation_error("amount must be non-negative", field="amount")
    if "is_percentage" in request.data:
        row.is_percentage = bool(request.data.get("is_percentage"))
    if "min_topup" in request.data or "minTopup" in request.data:
        row.min_topup = _to_decimal(
            request.data.get("min_topup") or request.data.get("minTopup"), "0"
        )
    if "status" in request.data:
        row.status = request.data.get("status")
    if "expires_at" in request.data or "expires" in request.data:
        raw = request.data.get("expires_at") or request.data.get("expires")
        if raw:
            from django.utils.dateparse import parse_date

            row.expires_at = parse_date(str(raw))
        else:
            row.expires_at = None
    if (
        row.type in (WalletBonus.Type.SIGNUP, WalletBonus.Type.REFERRAL)
        and row.is_percentage
        and row.min_topup <= 0
    ):
        return validation_error(
            "min_topup must be positive for percentage signup or referral bonuses",
            field="min_topup",
        )
    row.save()
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_loyalty_rule_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required")
    row = LoyaltyRule.objects.create(
        name=name,
        rule_description=request.data.get("rule_description") or request.data.get("rule") or "",
        event=request.data.get("event") or LoyaltyRule.Event.PURCHASE,
        multiplier=int(request.data.get("multiplier") or 1),
        status=request.data.get("status") or LoyaltyRule.Status.ACTIVE,
    )
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_loyalty_rule_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = LoyaltyRule.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "name" in request.data:
        row.name = (request.data.get("name") or "").strip() or row.name
    if "rule_description" in request.data or "rule" in request.data:
        row.rule_description = request.data.get("rule_description") or request.data.get("rule") or ""
    if "event" in request.data:
        row.event = request.data.get("event")
    if "multiplier" in request.data:
        row.multiplier = int(request.data.get("multiplier") or 1)
    if "status" in request.data:
        row.status = request.data.get("status")
    row.save()
    return Response({"id": str(row.pk)})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_family_create(request):
    if err := _forbidden(request):
        return err
    name = (request.data.get("name") or "").strip()
    leader, lerr = resolve_user_by_pk_or_phone(request.data.get("leader_id"), "leader_id")
    if lerr:
        return lerr
    if not name:
        return validation_error("name is required", field="name")
    gtype = request.data.get("type") or FamilyGroup.Type.FAMILY
    row = FamilyGroup.objects.create(
        name=name,
        leader=leader,
        type=gtype,
        status=request.data.get("status") or FamilyGroup.Status.ACTIVE,
    )
    FamilyGroupPermission.objects.get_or_create(group=row)
    audit_service.log(
        f"Created family group {name!r} (id={row.pk})",
        log_type=AuditLog.Type.FAMILY,
        performed_by=request.user,
        object_type="FamilyGroup",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.CREATE,
        module="families",
        metadata={"group_id": str(row.pk)},
    )
    return Response({"id": str(row.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_family_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = FamilyGroup.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        gid, gname = str(row.pk), row.name
        row.delete()
        audit_service.log(
            f"Deleted family group {gname!r} (id={gid})",
            log_type=AuditLog.Type.FAMILY,
            performed_by=request.user,
            object_type="FamilyGroup",
            object_id=gid,
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="families",
            metadata={"group_id": gid},
        )
        return Response({"ok": True})
    if "name" in request.data:
        row.name = (request.data.get("name") or "").strip() or row.name
    if "type" in request.data:
        row.type = request.data.get("type")
    if "status" in request.data:
        row.status = request.data.get("status")
    if "leader_id" in request.data:
        raw = request.data.get("leader_id")
        if scalar_request_value(raw) is not None:
            u, lerr = resolve_user_by_pk_or_phone(raw, "leader_id")
            if lerr:
                return lerr
            row.leader = u
    row.save()
    audit_service.log(
        f"Updated family group {row.name!r} (id={row.pk})",
        log_type=AuditLog.Type.FAMILY,
        performed_by=request.user,
        object_type="FamilyGroup",
        object_id=str(row.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="families",
        metadata={"group_id": str(row.pk)},
    )
    return Response({"id": str(row.pk)})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_family_members_list(request, pk):
    if err := _forbidden(request):
        return err
    group = FamilyGroup.objects.filter(pk=pk).first()
    if not group:
        return Response({"detail": "Not found."}, status=404)
    rows = []
    for m in group.members.select_related("user").all():
        w = (
            Wallet.objects.filter(family_group=group, owner=m.user)
            .order_by("-updated_at")
            .first()
        )
        if not w:
            w = Wallet.objects.filter(owner=m.user).order_by("-updated_at").first()
        row = {
            "id": str(m.pk),
            "user_id": str(m.user_id),
            "name": m.user.name,
            "phone": m.user.phone,
            "role": m.role,
            "status": m.status,
            "joinedDate": m.joined_at.date().isoformat(),
            "spending_limit_daily": float(m.spending_limit_daily),
            "spending_limit_weekly": float(m.spending_limit_weekly),
            "spending_limit_monthly": float(m.spending_limit_monthly),
            "initial_balance": float(m.initial_balance),
            "wallet_id": "",
            "wallet_balance": None,
            "wallet_type": "",
            "wallet_label": "",
        }
        if w:
            row["wallet_id"] = str(w.pk)
            row["wallet_balance"] = float(w.balance)
            row["wallet_type"] = w.type
            row["wallet_label"] = w.label or ""
        rows.append(row)

    group_wallets = []
    for w in group.wallets.select_related("owner", "vendor").order_by("-updated_at"):
        owner_name = ""
        if w.owner_id:
            owner_name = w.owner.name
        elif w.vendor_id:
            owner_name = w.vendor.store_name or ""
        group_wallets.append(
            {
                "id": str(w.pk),
                "balance": float(w.balance),
                "type": w.type,
                "label": w.label or "",
                "status": w.status,
                "owner_name": owner_name,
            }
        )
    return Response({"members": rows, "group_wallets": group_wallets})


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_family_permissions_write(request, pk):
    if err := _forbidden(request):
        return err
    group = FamilyGroup.objects.filter(pk=pk).first()
    if not group:
        return Response({"detail": "Not found."}, status=404)
    perm, _ = FamilyGroupPermission.objects.get_or_create(group=group)
    if request.method == "GET":
        return Response(
            {
                "allow_online_purchases": perm.allow_online_purchases,
                "allow_cash_withdrawal": perm.allow_cash_withdrawal,
            }
        )
    if "allow_online_purchases" in request.data:
        perm.allow_online_purchases = request.data.get("allow_online_purchases") in (
            True,
            "true",
            "1",
            1,
        )
    if "allow_cash_withdrawal" in request.data:
        perm.allow_cash_withdrawal = request.data.get("allow_cash_withdrawal") in (
            True,
            "true",
            "1",
            1,
        )
    perm.save()
    audit_service.log(
        "Updated family permissions: "
        f"allow_online_purchases={perm.allow_online_purchases}, "
        f"allow_cash_withdrawal={perm.allow_cash_withdrawal}",
        log_type=AuditLog.Type.FAMILY,
        performed_by=request.user,
        object_type="FamilyGroup",
        object_id=str(group.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="families",
        metadata={
            "group_id": str(group.pk),
            "allow_online_purchases": perm.allow_online_purchases,
            "allow_cash_withdrawal": perm.allow_cash_withdrawal,
        },
    )
    return Response({"ok": True})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_family_member_create(request, pk):
    if err := _forbidden(request):
        return err
    group = FamilyGroup.objects.filter(pk=pk).first()
    if not group:
        return Response({"detail": "Not found."}, status=404)
    user, uerr = resolve_user_by_pk_or_phone(request.data.get("user_id"), "user_id")
    if uerr:
        return uerr
    if FamilyMember.objects.filter(group=group, user=user).exists():
        return validation_error("user already in group")
    m = FamilyMember.objects.create(
        group=group,
        user=user,
        role=request.data.get("role") or FamilyMember.Role.GUEST,
        status=request.data.get("status") or FamilyMember.Status.ACTIVE,
        spending_limit_daily=_to_decimal(request.data.get("spending_limit_daily"), "0"),
        spending_limit_weekly=_to_decimal(request.data.get("spending_limit_weekly"), "0"),
        spending_limit_monthly=_to_decimal(request.data.get("spending_limit_monthly"), "0"),
        initial_balance=_to_decimal(request.data.get("initial_balance"), "0"),
    )
    audit_service.log(
        f"Added member {user.name} to family group id={group.pk} (member id={m.pk})",
        log_type=AuditLog.Type.FAMILY,
        performed_by=request.user,
        object_type="FamilyGroup",
        object_id=str(group.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.CREATE,
        module="families",
        metadata={"group_id": str(group.pk), "member_id": str(m.pk), "user_id": str(user.pk)},
    )
    return Response({"id": str(m.pk)}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_family_member_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = FamilyMember.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        gid, mid = str(row.group_id), str(row.pk)
        row.delete()
        audit_service.log(
            f"Removed family member id={mid} from group id={gid}",
            log_type=AuditLog.Type.FAMILY,
            performed_by=request.user,
            object_type="FamilyGroup",
            object_id=gid,
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="families",
            metadata={"group_id": gid, "member_id": mid},
        )
        return Response({"ok": True})
    if "role" in request.data:
        row.role = request.data.get("role")
    if "status" in request.data:
        row.status = request.data.get("status")
    for f in (
        "spending_limit_daily",
        "spending_limit_weekly",
        "spending_limit_monthly",
        "initial_balance",
    ):
        if f in request.data:
            setattr(row, f, _to_decimal(request.data.get(f), "0"))
    row.save()
    audit_service.log(
        f"Updated family member id={row.pk} in group id={row.group_id}",
        log_type=AuditLog.Type.FAMILY,
        performed_by=request.user,
        object_type="FamilyGroup",
        object_id=str(row.group_id),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="families",
        metadata={"group_id": str(row.group_id), "member_id": str(row.pk)},
    )
    return Response({"id": str(row.pk)})


# --- Loyalty / Security / Shipping admin singletons & helpers ---


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_loyalty_settings_singleton(request):
    if err := _forbidden(request):
        return err
    ls = LoyaltySettings.load()
    if request.method == "GET":
        return Response(
            {
                "points_per_currency_unit": float(ls.points_per_currency_unit),
                "redeem_points_per_currency": float(ls.redeem_points_per_currency),
                "min_redeem_points": ls.min_redeem_points,
                "max_redeem_per_order": ls.max_redeem_per_order,
                "referral_bonus_points": ls.referral_bonus_points,
                "loyalty_program_enabled": ls.loyalty_program_enabled,
            }
        )
    if "points_per_currency_unit" in request.data:
        ls.points_per_currency_unit = _to_decimal(
            request.data.get("points_per_currency_unit"), str(ls.points_per_currency_unit)
        )
    if "redeem_points_per_currency" in request.data:
        ls.redeem_points_per_currency = _to_decimal(
            request.data.get("redeem_points_per_currency"), str(ls.redeem_points_per_currency)
        )
    if "min_redeem_points" in request.data:
        ls.min_redeem_points = int(request.data.get("min_redeem_points") or 0)
    if "max_redeem_per_order" in request.data:
        ls.max_redeem_per_order = int(request.data.get("max_redeem_per_order") or 0)
    if "referral_bonus_points" in request.data:
        ls.referral_bonus_points = int(request.data.get("referral_bonus_points") or 0)
    if "loyalty_program_enabled" in request.data:
        ls.loyalty_program_enabled = request.data.get("loyalty_program_enabled") in (
            True,
            "true",
            "1",
            1,
        )
    ls.save()
    return Response({"ok": True})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_loyalty_summary(request):
    if err := _forbidden(request):
        return err
    referral_count = User.objects.filter(referred_by__isnull=False).count()
    rules = LoyaltyRule.objects.all()
    by_event = {}
    for ev, _ in LoyaltyRule.Event.choices:
        by_event[ev] = rules.filter(event=ev, status=LoyaltyRule.Status.ACTIVE).count()
    return Response(
        {
            "referral_count": referral_count,
            "active_loyalty_rules": LoyaltyRule.objects.filter(
                status=LoyaltyRule.Status.ACTIVE
            ).count(),
            "rules_by_event": by_event,
            "total_loyalty_rules": rules.count(),
        }
    )


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_security_settings_singleton(request):
    if err := _forbidden(request):
        return err
    ss = SecuritySettings.load()
    if request.method == "GET":
        return Response(
            {
                "otp_sensitive_crud": ss.otp_sensitive_crud,
                "rbac_enforced": ss.rbac_enforced,
                "duplicate_prevention": ss.duplicate_prevention,
                "auto_lock_failed_logins": ss.auto_lock_failed_logins,
                "ip_rate_limiting": ss.ip_rate_limiting,
            }
        )
    bool_fields = (
        "otp_sensitive_crud",
        "rbac_enforced",
        "duplicate_prevention",
        "auto_lock_failed_logins",
        "ip_rate_limiting",
    )
    for f in bool_fields:
        if f in request.data:
            setattr(ss, f, request.data.get(f) in (True, "true", "1", 1))
    ss.save()
    return Response({"ok": True})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_security_summary(request):
    if err := _forbidden(request):
        return err
    since = timezone.now() - timedelta(hours=24)
    return Response(
        {
            "flagged_open": FlaggedActivity.objects.filter(
                status=FlaggedActivity.Status.OPEN
            ).count(),
            "flagged_reviewed": FlaggedActivity.objects.filter(
                status=FlaggedActivity.Status.REVIEWED
            ).count(),
            "flagged_resolved": FlaggedActivity.objects.filter(
                status=FlaggedActivity.Status.RESOLVED
            ).count(),
            "blocked_users": User.objects.filter(is_active=False).count(),
            "flagged_wallet_txns": WalletTransaction.objects.filter(
                status__in=(WalletTransaction.Status.FLAGGED, WalletTransaction.Status.BLOCKED)
            ).count(),
            "security_audit_logs_24h": AuditLog.objects.filter(
                type=AuditLog.Type.SECURITY, created_at__gte=since
            ).count(),
        }
    )


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_shipping_settings_singleton(request):
    if err := _forbidden(request):
        return err
    sh = ShippingSettings.load()
    if request.method == "GET":
        return Response(
            {
                "seller_pays_shipping": sh.seller_pays_shipping,
                "free_shipping_global": sh.free_shipping_global,
                "default_zone_id": str(sh.default_zone_id) if sh.default_zone_id else "",
                "default_checkout_weight_kg": float(sh.default_checkout_weight_kg),
            }
        )
    if "seller_pays_shipping" in request.data:
        sh.seller_pays_shipping = request.data.get("seller_pays_shipping") in (
            True,
            "true",
            "1",
            1,
        )
    if "free_shipping_global" in request.data:
        sh.free_shipping_global = request.data.get("free_shipping_global") in (
            True,
            "true",
            "1",
            1,
        )
    if "default_zone_id" in request.data:
        raw = request.data.get("default_zone_id")
        if raw in ("", None):
            sh.default_zone = None
        else:
            z = ShippingZone.objects.filter(pk=raw).first()
            sh.default_zone = z
    if "default_checkout_weight_kg" in request.data:
        raw = request.data.get("default_checkout_weight_kg")
        try:
            w = Decimal(str(raw))
            if w < 0:
                w = Decimal("0")
            if w > Decimal("500"):
                w = Decimal("500")
            sh.default_checkout_weight_kg = w.quantize(Decimal("0.001"))
        except Exception:
            pass
    sh.save()
    return Response({"ok": True})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_shipping_calculate(request):
    if err := _forbidden(request):
        return err
    sh = ShippingSettings.load()
    zone_id = request.data.get("zone_id") or request.data.get("zone")
    if not zone_id and sh.default_zone_id:
        zone_id = sh.default_zone_id
    zone = ShippingZone.objects.filter(pk=zone_id).first() if zone_id else None
    if not zone:
        return validation_error("zone_id is required (or set default zone in shipping settings)")
    weight = float(request.data.get("weight_kg") or request.data.get("weight") or 0)
    order_total_dec = _to_decimal(request.data.get("order_total") or 0)
    method_id = request.data.get("method_id")
    method = ShippingMethod.objects.filter(pk=method_id).first() if method_id else None
    shipping_fee, breakdown = compute_shipping_fee(
        sh,
        zone,
        order_total=order_total_dec,
        weight_kg=weight,
        method=method,
    )
    if sh.seller_pays_shipping:
        breakdown.append({"step": "seller_pays", "note": "fee shown but borne by seller"})

    breakdown_explained = _explain_shipping_breakdown(
        breakdown=breakdown,
        fee=shipping_fee,
        zone=zone,
        weight=weight,
        order_total=order_total_dec,
        method=method,
    )
    inputs_payload = {
        "order_total": float(order_total_dec),
        "weight_kg": weight,
        "zone_id": str(zone.pk),
        "zone_name": zone.name,
        "method_id": str(method.pk) if method else None,
        "method_name": method.name if method else None,
        "free_shipping_global": sh.free_shipping_global,
        "seller_pays_shipping": sh.seller_pays_shipping,
    }
    return Response(
        {
            "fee": float(shipping_fee),
            "currency": "NPR",
            "breakdown": breakdown,
            "breakdown_explained": breakdown_explained,
            "inputs": inputs_payload,
            "zone": {"id": str(zone.pk), "name": zone.name},
        }
    )


def _parse_reel_tags(raw):
    import json

    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return [s]
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_reel_create(request):
    if err := _forbidden(request):
        return err
    vendor_id = request.data.get("vendor_id")
    vendor = Vendor.objects.filter(pk=vendor_id).first()
    if not vendor:
        return validation_error("vendor_id is required")
    video_url = (request.data.get("video_url") or "").strip()
    if not video_url:
        return validation_error("video_url is required")
    platform = request.data.get("platform") or Reel.Platform.DIRECT_MP4
    if platform not in {c[0] for c in Reel.Platform.choices}:
        return validation_error("invalid platform")
    product_id = request.data.get("product_id")
    product = None
    if product_id:
        product = Product.objects.filter(pk=product_id, seller=vendor).first()
    tags = _parse_reel_tags(request.data.get("tags"))
    row = Reel.objects.create(
        vendor=vendor,
        video_url=video_url,
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


@api_view(["PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_reel_detail_write(request, pk):
    if err := _forbidden(request):
        return err
    row = Reel.objects.filter(pk=pk).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        row.delete()
        return Response({"ok": True})
    if "video_url" in request.data:
        row.video_url = (request.data.get("video_url") or "").strip() or row.video_url
    if "platform" in request.data:
        pl = request.data.get("platform")
        if pl not in {c[0] for c in Reel.Platform.choices}:
            return validation_error("invalid platform", field="platform")
        row.platform = pl
    if "caption" in request.data:
        row.caption = (request.data.get("caption") or "")[:200]
    if "tags" in request.data:
        row.tags = _parse_reel_tags(request.data.get("tags"))
    if "status" in request.data:
        st = request.data.get("status")
        valid_statuses = {c[0] for c in Reel.Status.choices}
        if st not in valid_statuses:
            return validation_error("invalid status", field="status")
        row.status = st
    if "product_id" in request.data:
        pid = request.data.get("product_id")
        row.product = (
            Product.objects.filter(pk=pid, seller=row.vendor).first() if pid else None
        )
    boost_err = apply_reel_boost_from_data(row, request.data)
    if boost_err:
        return validation_error(boost_err[0], field=boost_err[1])
    if row.status == Reel.Status.REJECTED and "rejection_reason" in request.data:
        row.rejection_reason = (request.data.get("rejection_reason") or "")[:500]
    if request.FILES.get("thumbnail"):
        row.thumbnail = request.FILES["thumbnail"]
    row.save()
    return Response({"id": str(row.pk)})
