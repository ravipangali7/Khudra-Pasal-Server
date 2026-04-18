from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.authentication import SessionAuthentication, TokenAuthentication

from core.models import DeliveryMan, Order, OrderItem, Product, User, Vendor, Wallet, WalletTransaction
from core.serializers import RecentOrderSerializer
from core.services import wallet_gateway_topup as wgt
from core.services.base import get_or_create_personal_wallet
from core.services.khalti_epayment_service import KhaltiApiError, KhaltiConfigError
from core.views.admin.resource_views import _to_decimal
from core.views.admin.admin_write_utils import validation_error


def _is_admin_like(user):
    return bool(user and user.is_authenticated and (user.is_staff or user.is_superuser))


def _forbidden_if_not_admin(request):
    from core.views.admin.admin_access import enforce_admin_api_access

    return enforce_admin_api_access(request)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def dashboard_summary(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    today = timezone.localdate()
    day_start = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))

    today_orders = Order.objects.filter(created_at__gte=day_start)
    today_sales = today_orders.aggregate(total=Sum("total"))["total"] or Decimal("0")

    platform_bal = (
        Wallet.objects.filter(type=Wallet.Type.PLATFORM).aggregate(total=Sum("balance"))["total"]
        or Decimal("0")
    )
    payload = {
        "today_sales": float(today_sales),
        "today_orders": today_orders.count(),
        "total_users": User.objects.count(),
        "total_vendors": Vendor.objects.count(),
        "delivery_men_count": DeliveryMan.objects.count(),
        "wallet_balance_total": float(Wallet.objects.aggregate(total=Sum("balance"))["total"] or Decimal("0")),
        "platform_wallet_balance": float(platform_bal),
    }
    return Response(payload)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def dashboard_recent_orders(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    limit = min(int(request.query_params.get("limit", 12)), 50)
    qs = Order.objects.select_related("customer", "seller").order_by("-created_at")

    days_raw = request.query_params.get("days")
    if days_raw is not None and str(days_raw).strip() != "":
        try:
            days = min(int(days_raw), 90)
        except (TypeError, ValueError):
            days = 7
        days = max(1, days)
        day_end = timezone.localdate()
        day_start = day_end - timedelta(days=days - 1)
        tz = timezone.get_current_timezone()
        start_dt = timezone.make_aware(datetime.combine(day_start, time.min), tz)
        end_dt = timezone.make_aware(datetime.combine(day_end, time.max), tz)
        qs = qs.filter(created_at__gte=start_dt, created_at__lte=end_dt)

    queryset = qs[:limit]
    serializer = RecentOrderSerializer(queryset, many=True)
    return Response(serializer.data)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def dashboard_sales_series(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    days = min(int(request.query_params.get("days", 7)), 90)
    day_end = timezone.localdate()
    day_start = day_end - timedelta(days=days - 1)
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(day_start, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(day_end, time.max), tz)

    series_qs = (
        Order.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(sales=Sum("total"), orders=Count("id"))
        .order_by("day")
    )
    rows_by_day = {}
    for row in series_qs:
        d = row["day"]
        if d is not None and hasattr(d, "isoformat"):
            rows_by_day[d] = {
                "sales": float(row["sales"] or Decimal("0")),
                "orders": int(row["orders"] or 0),
            }
    data = _fill_series_days(day_start, day_end, rows_by_day)
    return Response(data)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def dashboard_wallet_series(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    days = min(int(request.query_params.get("days", 7)), 90)
    day_end = timezone.localdate()
    day_start = day_end - timedelta(days=days - 1)
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(day_start, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(day_end, time.max), tz)

    done = WalletTransaction.Status.COMPLETED
    base = WalletTransaction.objects.filter(
        created_at__gte=start_dt,
        created_at__lte=end_dt,
        status=done,
    )
    rows = (
        base.annotate(day=TruncDate("created_at"))
        .values("day", "type")
        .annotate(vol=Sum("amount"))
    )
    buckets_by_day = {}
    for row in rows:
        d = row["day"]
        if d is None or not hasattr(d, "isoformat"):
            continue
        if d not in buckets_by_day:
            buckets_by_day[d] = {
                "topup": Decimal("0"),
                "transfer": Decimal("0"),
                "withdrawal": Decimal("0"),
            }
        t = row["type"]
        v = abs(row["vol"] or Decimal("0"))
        if t in (WalletTransaction.Type.TOPUP, WalletTransaction.Type.CREDIT):
            buckets_by_day[d]["topup"] += v
        elif t == WalletTransaction.Type.TRANSFER:
            buckets_by_day[d]["transfer"] += v
        elif t == WalletTransaction.Type.WITHDRAWAL:
            buckets_by_day[d]["withdrawal"] += v

    series = []
    d = day_start
    while d <= day_end:
        b = buckets_by_day.get(
            d,
            {"topup": Decimal("0"), "transfer": Decimal("0"), "withdrawal": Decimal("0")},
        )
        series.append(
            {
                "day": d.isoformat(),
                "topup": float(b["topup"]),
                "transfer": float(b["transfer"]),
                "withdrawal": float(b["withdrawal"]),
            }
        )
        d += timedelta(days=1)

    credit_types = (
        WalletTransaction.Type.CREDIT,
        WalletTransaction.Type.TOPUP,
        WalletTransaction.Type.BONUS,
    )
    debit_types = (
        WalletTransaction.Type.DEBIT,
        WalletTransaction.Type.PURCHASE,
        WalletTransaction.Type.WITHDRAWAL,
    )
    inflow = base.filter(type__in=credit_types).aggregate(t=Sum("amount"))["t"] or Decimal("0")
    outflow = base.filter(type__in=debit_types).aggregate(t=Sum("amount"))["t"] or Decimal("0")

    return Response(
        {
            "series": series,
            "totals": {
                "inflow": float(abs(inflow)),
                "outflow": float(abs(outflow)),
            },
        }
    )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def dashboard_low_stock(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    try:
        threshold = int(request.query_params.get("threshold", 15))
    except (TypeError, ValueError):
        threshold = 15
    threshold = max(0, min(threshold, 500))
    try:
        limit = int(request.query_params.get("limit", 25))
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, 100))

    qs = (
        Product.objects.select_related("seller")
        .filter(stock__lte=threshold)
        .filter(
            Q(status=Product.Status.ACTIVE)
            | Q(status=Product.Status.OUT_OF_STOCK)
        )
        .order_by("stock", "id")[:limit]
    )
    rows = [
        {
            "id": str(p.pk),
            "name": p.name,
            "sku": p.sku,
            "stock": p.stock,
            "status": p.status,
            "seller": p.seller.store_name if p.seller_id else "In-House",
        }
        for p in qs
    ]
    return Response({"threshold": threshold, "results": rows})


def _reports_parse_vendor_category(request):
    vendor_id = None
    raw_v = request.query_params.get("vendor_id") or request.query_params.get("seller_id")
    if raw_v is not None and str(raw_v).strip() != "":
        try:
            vendor_id = int(raw_v)
        except (TypeError, ValueError):
            vendor_id = None
    category_id = None
    raw_c = request.query_params.get("category_id")
    if raw_c is not None and str(raw_c).strip() != "":
        try:
            category_id = int(raw_c)
        except (TypeError, ValueError):
            category_id = None
    return vendor_id, category_id


def _orders_in_reports_window(start_dt, end_dt, vendor_id, category_id):
    qs = Order.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt)
    if vendor_id:
        qs = qs.filter(seller_id=vendor_id)
    if category_id:
        qs = qs.filter(items__product__category_id=category_id).distinct()
    return qs


def _pct_growth(current: float, previous: float):
    if previous == 0:
        return None if current == 0 else 100.0
    return round(100.0 * (current - previous) / previous, 2)


def _aggregate_kpis(qs):
    agg = qs.aggregate(total_sales=Sum("total"), total_orders=Count("id"))
    total_sales = float(agg["total_sales"] or Decimal("0"))
    total_orders = int(agg["total_orders"] or 0)
    aov = (total_sales / total_orders) if total_orders else 0.0
    return total_sales, total_orders, aov


def _fill_series_days(day_start, day_end, rows_by_day: dict):
    out = []
    d = day_start
    while d <= day_end:
        key = d.isoformat()
        row = rows_by_day.get(d)
        out.append(
            {
                "day": key,
                "sales": float(row["sales"]) if row else 0.0,
                "orders": int(row["orders"]) if row else 0,
            }
        )
        d += timedelta(days=1)
    return out


def _fill_signup_series(day_start, day_end, counts_by_day: dict):
    out = []
    d = day_start
    while d <= day_end:
        out.append({"day": d.isoformat(), "signups": int(counts_by_day.get(d, 0))})
        d += timedelta(days=1)
    return out


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def dashboard_reports(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    if not date_from or not date_to:
        return Response({"detail": "date_from and date_to are required (ISO dates)."}, status=400)
    d0 = parse_date(str(date_from).strip())
    d1 = parse_date(str(date_to).strip())
    if not d0 or not d1:
        return Response({"detail": "Invalid date_from or date_to."}, status=400)
    if d0 > d1:
        return Response({"detail": "date_from must be on or before date_to."}, status=400)
    span_days = (d1 - d0).days + 1
    if span_days > 366:
        return Response({"detail": "Date range cannot exceed 366 days."}, status=400)

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(d0, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(d1, time.max), tz)

    vendor_id, category_id = _reports_parse_vendor_category(request)

    qs = _orders_in_reports_window(start_dt, end_dt, vendor_id, category_id)
    total_sales, total_orders, aov = _aggregate_kpis(qs)

    prev_end_date = d0 - timedelta(days=1)
    prev_start_date = prev_end_date - timedelta(days=span_days - 1)
    prev_start_dt = timezone.make_aware(datetime.combine(prev_start_date, time.min), tz)
    prev_end_dt = timezone.make_aware(datetime.combine(prev_end_date, time.max), tz)
    prev_qs = _orders_in_reports_window(prev_start_dt, prev_end_dt, vendor_id, category_id)
    prev_sales, prev_orders, _prev_aov = _aggregate_kpis(prev_qs)

    series_qs = (
        qs.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(sales=Sum("total"), orders=Count("id"))
        .order_by("day")
    )
    rows_by_day = {}
    for row in series_qs:
        day = row["day"]
        if hasattr(day, "isoformat"):
            rows_by_day[day] = {
                "sales": float(row["sales"] or Decimal("0")),
                "orders": row["orders"],
            }
    series = _fill_series_days(d0, d1, rows_by_day)

    item_base = OrderItem.objects.filter(order__in=qs)
    if category_id:
        item_base = item_base.filter(product__category_id=category_id)

    category_breakdown = []
    cat_rows = (
        item_base.values("product__category__name", "product__category_id")
        .annotate(sales=Sum("total_price"), lines=Count("id"))
        .order_by("-sales")[:50]
    )
    for row in cat_rows:
        name = row["product__category__name"] or "Uncategorized"
        category_breakdown.append(
            {
                "name": name,
                "category_id": row["product__category_id"],
                "sales": float(row["sales"] or Decimal("0")),
                "lines": row["lines"],
            }
        )

    vendor_breakdown = []
    vend_rows = (
        qs.exclude(seller_id__isnull=True)
        .values("seller_id", "seller__store_name")
        .annotate(sales=Sum("total"), orders=Count("id"))
        .order_by("-sales")[:25]
    )
    for row in vend_rows:
        vendor_breakdown.append(
            {
                "vendor_id": row["seller_id"],
                "name": row["seller__store_name"] or "Vendor",
                "sales": float(row["sales"] or Decimal("0")),
                "orders": row["orders"],
            }
        )

    wallet_qs = WalletTransaction.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt)
    wallet_by_type = []
    for row in (
        wallet_qs.values("type")
        .annotate(total=Sum("amount"), count=Count("id"))
        .order_by("-total")
    ):
        wallet_by_type.append(
            {
                "type": row["type"],
                "amount": float(row["total"] or Decimal("0")),
                "count": row["count"],
            }
        )

    signup_rows = (
        User.objects.filter(date_joined__gte=start_dt, date_joined__lte=end_dt)
        .annotate(day=TruncDate("date_joined"))
        .values("day")
        .annotate(signups=Count("id"))
        .order_by("day")
    )
    signups_by_day = {}
    for row in signup_rows:
        day = row["day"]
        if hasattr(day, "isoformat"):
            signups_by_day[day] = row["signups"]
    signup_series = _fill_signup_series(d0, d1, signups_by_day)

    return Response(
        {
            "period": {"date_from": d0.isoformat(), "date_to": d1.isoformat()},
            "previous_period": {
                "date_from": prev_start_date.isoformat(),
                "date_to": prev_end_date.isoformat(),
            },
            "kpis": {
                "total_sales": total_sales,
                "total_orders": total_orders,
                "aov": round(aov, 2),
                "previous_total_sales": prev_sales,
                "previous_total_orders": prev_orders,
                "sales_growth_pct": _pct_growth(total_sales, prev_sales),
                "orders_growth_pct": _pct_growth(float(total_orders), float(prev_orders)),
            },
            "series": series,
            "category_breakdown": category_breakdown,
            "vendor_breakdown": vendor_breakdown,
            "wallet_by_type": wallet_by_type,
            "signup_series": signup_series,
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_topup(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden
    u = request.user
    w = get_or_create_personal_wallet(u)
    if w.status != Wallet.Status.ACTIVE:
        return Response({"detail": "Wallet is frozen."}, status=400)
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    method = (request.data.get("method") or "esewa").strip()[:50]
    method_norm = method.lower()
    if method_norm not in ("esewa", "khalti"):
        return validation_error("Only eSewa and Khalti are supported.", field="method")
    raw_return = (request.data.get("return_path") or "/admin/dashboard").strip()
    return_path = raw_return[:500] if raw_return.startswith("/") else f"/{raw_return[:499].lstrip('/')}"
    try:
        wgt.assert_can_topup_wallet(payer=u, wallet=w, target=wgt.TOPUP_TARGET_ADMIN_PERSONAL)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    if method_norm == "esewa":
        return Response(
            wgt.build_esewa_initiate_response(
                request=request,
                payer=u,
                wallet=w,
                amount=amount,
                method=method,
                topup_target=wgt.TOPUP_TARGET_ADMIN_PERSONAL,
                return_path=return_path,
                return_query_esewa=None,
                success_reverse_name="portal-wallet-topup-esewa-success",
                failure_reverse_name="portal-wallet-topup-esewa-failure",
            )
        )
    try:
        return Response(
            wgt.build_khalti_initiate_response(
                payer=u,
                wallet=w,
                amount=amount,
                method=method,
                topup_target=wgt.TOPUP_TARGET_ADMIN_PERSONAL,
                return_path=return_path,
                return_query_esewa=None,
                purchase_order_id=f"KP-A-{uuid4().hex[:24]}",
                purchase_order_name="Admin wallet top-up",
            )
        )
    except ValueError as e:
        return validation_error(str(e), field="amount")
    except KhaltiConfigError as e:
        return Response({"detail": str(e)}, status=503)
    except KhaltiApiError as e:
        return Response(
            {"detail": str(e), "khalti_error": (e.body or "")[:2000]},
            status=502,
        )


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_wallet_topup_khalti_verify(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden
    pidx = (request.query_params.get("pidx") or "").strip()
    if not pidx:
        return validation_error("pidx is required", field="pidx")
    body, status = wgt.khalti_wallet_topup_verify_payload(user=request.user, pidx=pidx)
    return Response(body, status=status)

