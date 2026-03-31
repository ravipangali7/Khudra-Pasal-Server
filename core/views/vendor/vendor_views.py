"""Vendor portal API — authenticated vendor users (Token)."""

from datetime import datetime, time
from decimal import Decimal

from core.phone_auth import authenticate_user_by_phone
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.utils.urls import remove_query_param, replace_query_param

from django.db.models import Exists, OuterRef, Q

from core.models import (
    Notification,
    Order,
    OrderCommissionSettlement,
    Product,
    ProductReview,
    Refund,
    WalletTransaction,
    WalletWithdrawal,
)
from core.portal_roles import PORTAL_VENDOR, assert_portal_login_allowed
from core.services.wallet_txn_signed import signed_amount_for_wallet_transaction

from core.views.vendor.common import media_url, vendor_or_error, vendor_pending_withdrawal_total


class VendorPagination(PageNumberPagination):
    page_size = 30
    page_size_query_param = "page_size"
    max_page_size = 200


def _paginate(request, queryset):
    paginator = VendorPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator, page


_VENDOR_LEDGER_FETCH_CAP = 500


def _paginate_merged_ledger(request, rows: list[dict]) -> Response:
    """Page a pre-sorted list; preserves page_size query param in next/previous links."""
    paginator = VendorPagination()
    try:
        page_size = int(
            request.query_params.get(
                paginator.page_size_query_param, paginator.page_size
            )
        )
    except (TypeError, ValueError):
        page_size = paginator.page_size
    page_size = max(1, min(page_size, paginator.max_page_size))
    try:
        page_num = int(request.query_params.get("page", 1) or 1)
    except (TypeError, ValueError):
        page_num = 1
    if page_num < 1:
        page_num = 1

    total = len(rows)
    start = (page_num - 1) * page_size
    end = start + page_size
    page_rows = rows[start:end]

    base = request.build_absolute_uri()
    next_url = None
    if end < total:
        next_url = replace_query_param(base, "page", page_num + 1)
        next_url = replace_query_param(
            next_url, paginator.page_size_query_param, page_size
        )
    previous_url = None
    if start > 0:
        if page_num == 2:
            previous_url = remove_query_param(base, "page")
        else:
            previous_url = replace_query_param(base, "page", page_num - 1)
        previous_url = replace_query_param(
            previous_url, paginator.page_size_query_param, page_size
        )

    return Response(
        {
            "count": total,
            "next": next_url,
            "previous": previous_url,
            "results": page_rows,
        }
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def vendor_login(request):
    phone = request.data.get("phone", "").strip()
    password = request.data.get("password", "")
    user = authenticate_user_by_phone(request, phone, password)
    if not user:
        return Response({"detail": "Invalid credentials."}, status=400)
    denied = assert_portal_login_allowed(user, PORTAL_VENDOR)
    if denied:
        return denied
    token, _ = Token.objects.get_or_create(user=user)
    v = getattr(user, "vendor_profile", None)
    if v:
        return Response(
            {
                "token": token.key,
                "user": {
                    "id": user.id,
                    "name": user.name,
                    "store_name": v.store_name,
                    "store_slug": v.store_slug,
                    "status": v.status,
                },
            }
        )
    return Response(
        {
            "token": token.key,
            "user": {
                "id": user.id,
                "name": user.name,
                "role": user.role,
                "store_name": "",
                "store_slug": "",
                "status": "staff",
            },
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_me(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    return Response(
        {
            "id": str(vendor.pk),
            "user_id": request.user.pk,
            "store_name": vendor.store_name,
            "store_slug": vendor.store_slug,
            "status": vendor.status,
            "rating": float(vendor.rating),
            "commission_rate": float(vendor.commission_rate),
            "description": vendor.description,
            "contact_email": vendor.contact_email,
            "phone": vendor.phone,
            "address": vendor.address,
            "logo_url": media_url(request, vendor.logo),
            "banner_url": media_url(request, vendor.banner),
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_summary(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err

    today = timezone.localdate()
    day_start = timezone.make_aware(datetime.combine(today, time.min))

    today_orders = Order.objects.filter(seller=vendor, created_at__gte=day_start)
    today_sales = today_orders.aggregate(t=Sum("total"))["t"] or Decimal("0")

    pending_orders = Order.objects.filter(seller=vendor, status=Order.Status.PENDING).count()

    products_qs = Product.objects.filter(seller=vendor)
    product_count = products_qs.count()
    pending_approval = products_qs.filter(status=Product.Status.DRAFT).count()

    wallet_bal = Decimal("0")
    try:
        wallet_bal = vendor.wallet.balance
    except ObjectDoesNotExist:
        pass

    pending_payout = vendor_pending_withdrawal_total(vendor)

    return Response(
        {
            "today_sales": float(today_sales),
            "today_orders": today_orders.count(),
            "pending_orders": pending_orders,
            "product_count": product_count,
            "pending_product_approval": pending_approval,
            "wallet_balance": float(wallet_bal),
            "pending_payout": float(pending_payout),
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_orders_list(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = (
        Order.objects.filter(seller=vendor)
        .select_related("customer", "delivery_address")
        .annotate(item_count=Count("items"))
        .annotate(has_refund=Exists(Refund.objects.filter(order_id=OuterRef("pk"))))
        .order_by("-created_at")
    )
    status = request.query_params.get("status")
    if status:
        qs = qs.filter(status=status)
    if request.query_params.get("has_refund") in ("1", "true", "True"):
        qs = qs.filter(has_refund=True)
    paginator, page = _paginate(request, qs)
    rows = []
    for o in page:
        addr = getattr(o, "delivery_address", None)
        rows.append(
            {
                "id": o.order_number,
                "customer": o.customer.name,
                "items": o.item_count,
                "total": float(o.total),
                "status": o.status,
                "date": o.created_at.strftime("%Y-%m-%d %H:%M"),
                "payment": o.get_payment_method_display(),
                "payment_status": o.payment_status,
                "has_refund": bool(getattr(o, "has_refund", False)),
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_products_list(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = (
        Product.objects.filter(seller=vendor)
        .select_related("category", "brand")
        .annotate(sales=Count("orderitem"))
        .order_by("-created_at")
    )
    st = request.query_params.get("status")
    if st:
        qs = qs.filter(status=st)
    search = (request.query_params.get("search") or "").strip()
    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(sku__icontains=search))
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(p.pk),
            "name": p.name,
            "slug": p.slug,
            "sku": p.sku,
            "price": float(p.discount_price or p.price),
            "stock": p.stock,
            "status": p.status,
            "category": p.category.name,
            "category_id": str(p.category_id),
            "sales": int(p.sales),
            "image_url": media_url(request, p.image),
        }
        for p in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_reviews_list(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = (
        ProductReview.objects.filter(product__seller=vendor)
        .select_related("product", "customer")
        .order_by("-created_at")
    )
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(r.pk),
            "product": r.product.name,
            "product_id": str(r.product_id),
            "customer": r.customer.name,
            "rating": r.rating,
            "comment": r.comment,
            "date": r.created_at.date().isoformat(),
            "replied": bool(r.reply_text),
            "status": r.status,
            "vendor_read_at": r.vendor_read_at.isoformat() if r.vendor_read_at else None,
        }
        for r in page
    ]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_notifications_list(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    u = request.user
    qs = Notification.objects.filter(recipient=u).order_by("-created_at")[:50]
    rows = [
        {
            "id": str(n.pk),
            "type": n.type,
            "title": n.title,
            "message": n.message,
            "time": n.created_at.isoformat(),
            "is_read": n.is_read,
            "action_url": n.action_url or "",
        }
        for n in qs
    ]
    return Response({"results": rows})


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_wallet_transactions(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    try:
        vw = vendor.wallet
    except ObjectDoesNotExist:
        return Response({"count": 0, "next": None, "previous": None, "results": []})

    merged: list[dict] = []

    wt_qs = (
        WalletTransaction.objects.filter(wallet=vw)
        .order_by("-created_at")[:_VENDOR_LEDGER_FETCH_CAP]
    )
    for t in wt_qs:
        signed = signed_amount_for_wallet_transaction(t, vw)
        merged.append(
            {
                "id": t.txn_id,
                "source": "wallet",
                "type": t.type,
                "amount": signed,
                "status": t.status,
                "date": t.created_at.date().isoformat(),
                "datetime": t.created_at.isoformat(),
                "description": t.description,
                "fund_source": (t.fund_source or "").strip(),
            }
        )

    cms_qs = (
        OrderCommissionSettlement.objects.filter(vendor=vendor)
        .select_related("order")
        .order_by("-created_at")[:_VENDOR_LEDGER_FETCH_CAP]
    )
    for s in cms_qs:
        comm = float(s.commission_amount)
        merged.append(
            {
                "id": f"cms-{s.pk}",
                "source": "platform_commission",
                "type": "platform_commission",
                "amount": -comm,
                "status": "completed",
                "date": s.created_at.date().isoformat(),
                "datetime": s.created_at.isoformat(),
                "description": (
                    f"Commission deducted by Super Admin ({float(s.commission_percent)}%) — "
                    f"order {s.order.order_number}"
                ),
                "fund_source": "",
                "order_number": s.order.order_number,
            }
        )

    # Pending / rejected only — completed payouts appear as wallet withdrawal rows.
    wd_qs = (
        WalletWithdrawal.objects.filter(
            wallet=vw,
            status__in=(
                WalletWithdrawal.Status.PENDING,
                WalletWithdrawal.Status.REJECTED,
            ),
        )
        .order_by("-created_at")[:300]
    )
    for w in wd_qs:
        suffix = ""
        if w.status == WalletWithdrawal.Status.REJECTED:
            suffix = " (rejected — not debited)"
        merged.append(
            {
                "id": w.withdrawal_number,
                "source": "withdrawal_request",
                "type": "withdrawal_request",
                "amount": -float(w.amount),
                "status": w.status,
                "date": w.created_at.date().isoformat(),
                "datetime": w.created_at.isoformat(),
                "description": f"Withdrawal request — {w.get_method_display()}{suffix}",
                "fund_source": "",
            }
        )

    merged.sort(key=lambda r: r["datetime"], reverse=True)
    return _paginate_merged_ledger(request, merged)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_commission_settlements(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    qs = (
        OrderCommissionSettlement.objects.filter(vendor=vendor)
        .select_related("order")
        .order_by("-created_at")
    )
    paginator, page = _paginate(request, qs)
    rows = [
        {
            "id": str(s.pk),
            "order_number": s.order.order_number,
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


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_logout(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    del vendor
    if request.auth:
        # DRF authtoken: invalidate current token server-side
        request.auth.delete()
    return Response({"detail": "Logged out."})
