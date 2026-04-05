"""Marketplace-wide purchase (order) analytics for the admin portal."""

from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils.dateparse import parse_date
from django.utils import timezone
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import Order, OrderCommissionSettlement, OrderItem
from core.views.admin.admin_access import enforce_admin_api_access


def _portal_label(placed_portal: str | None) -> str:
    if not placed_portal:
        return "Legacy / unspecified"
    labels = dict(Order.PlacedPortal.choices)
    return labels.get(placed_portal, placed_portal)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_purchase_insights(request):
    if err := enforce_admin_api_access(request):
        return err

    end_d = parse_date(request.query_params.get("to") or "") or timezone.localdate()
    start_d = parse_date(request.query_params.get("from") or "") or (end_d - timedelta(days=30))
    start_dt = timezone.make_aware(datetime.combine(start_d, datetime.min.time()))
    end_dt = timezone.make_aware(datetime.combine(end_d, datetime.max.time()))
    oq = Order.objects.filter(created_at__gte=start_dt, created_at__lte=end_dt)

    order_count = oq.count()
    gross = float(oq.aggregate(t=Sum("total"))["t"] or 0)
    aov = (gross / order_count) if order_count else 0.0
    paid_orders = oq.filter(payment_status=Order.PaymentStatus.PAID).count()
    pending_pay = oq.filter(payment_status=Order.PaymentStatus.PENDING).count()
    unique_customers = oq.values("customer_id").distinct().count()
    active_vendors = oq.filter(seller__isnull=False).values("seller_id").distinct().count()

    item_f = OrderItem.objects.filter(
        order__created_at__gte=start_dt,
        order__created_at__lte=end_dt,
    )
    items_sold = int(item_f.aggregate(qty=Sum("quantity"))["qty"] or 0)

    settlements = OrderCommissionSettlement.objects.filter(
        created_at__gte=start_dt,
        created_at__lte=end_dt,
    )
    platform_commission = float(
        settlements.aggregate(s=Sum("commission_amount"))["s"] or Decimal("0")
    )
    vendor_payouts = float(settlements.aggregate(s=Sum("vendor_amount"))["s"] or Decimal("0"))

    daily = list(
        oq.annotate(d=TruncDate("created_at"))
        .values("d")
        .annotate(revenue=Sum("total"), orders=Count("id"))
        .order_by("d")
    )
    daily_out = [
        {
            "day": (x["d"].isoformat() if x["d"] else ""),
            "revenue": float(x["revenue"] or 0),
            "orders": x["orders"],
        }
        for x in daily
    ]

    pay_rows = oq.values("payment_method").annotate(count=Count("id"), revenue=Sum("total"))
    payment_mix = [
        {
            "method": r["payment_method"] or "—",
            "count": r["count"],
            "revenue": float(r["revenue"] or 0),
        }
        for r in pay_rows
    ]

    portal_rows = oq.values("placed_portal").annotate(count=Count("id"), revenue=Sum("total"))
    portal_mix = [
        {
            "portal": _portal_label(r["placed_portal"]),
            "portal_key": r["placed_portal"] or "",
            "count": r["count"],
            "revenue": float(r["revenue"] or 0),
        }
        for r in portal_rows
    ]

    pos_q = oq.filter(is_pos_order=True)
    online_q = oq.filter(is_pos_order=False)
    channel_mix = [
        {
            "channel": "POS",
            "count": pos_q.count(),
            "revenue": float(pos_q.aggregate(t=Sum("total"))["t"] or 0),
        },
        {
            "channel": "Online & other",
            "count": online_q.count(),
            "revenue": float(online_q.aggregate(t=Sum("total"))["t"] or 0),
        },
    ]

    status_rows = oq.values("status").annotate(count=Count("id"))
    status_funnel = [{"status": r["status"], "count": r["count"]} for r in status_rows]

    top_products = list(
        item_f.values("product_id", "product__name", "product__sku")
        .annotate(qty=Sum("quantity"), revenue=Sum("total_price"))
        .order_by("-revenue")[:15]
    )
    top_products_out = [
        {
            "product_id": r["product_id"],
            "name": r["product__name"] or "—",
            "sku": r["product__sku"] or "",
            "qty": int(r["qty"] or 0),
            "revenue": float(r["revenue"] or 0),
        }
        for r in top_products
    ]

    top_vendors = list(
        oq.filter(seller__isnull=False)
        .values("seller_id", "seller__store_name")
        .annotate(orders=Count("id"), gross=Sum("total"))
        .order_by("-gross")[:20]
    )
    top_vendors_out = [
        {
            "vendor_id": r["seller_id"],
            "store_name": r["seller__store_name"] or "—",
            "orders": r["orders"],
            "gross": float(r["gross"] or 0),
        }
        for r in top_vendors
    ]

    vendor_pie = top_vendors_out[:8]

    recent = (
        oq.select_related("customer", "seller")
        .order_by("-created_at")[:20]
        .values(
            "id",
            "order_number",
            "created_at",
            "customer__name",
            "seller__store_name",
            "status",
            "payment_status",
            "total",
            "payment_method",
        )
    )
    recent_orders = [
        {
            "id": r["id"],
            "order_number": r["order_number"],
            "date": r["created_at"].isoformat() if r["created_at"] else "",
            "customer_name": r["customer__name"] or "—",
            "seller_name": r["seller__store_name"] or "—",
            "status": r["status"],
            "payment_status": r["payment_status"],
            "payment_method": r["payment_method"],
            "total": float(r["total"] or 0),
        }
        for r in recent
    ]

    return Response(
        {
            "role": "admin",
            "range": {"from": start_d.isoformat(), "to": end_d.isoformat()},
            "kpis": {
                "gross_sales": gross,
                "order_count": order_count,
                "aov": round(aov, 2),
                "items_sold": items_sold,
                "paid_orders": paid_orders,
                "pending_payment_orders": pending_pay,
                "unique_customers": unique_customers,
                "active_vendors": active_vendors,
                "platform_commission": platform_commission,
                "vendor_payouts": vendor_payouts,
            },
            "daily": daily_out,
            "payment_mix": payment_mix,
            "portal_mix": portal_mix,
            "channel_mix": channel_mix,
            "status_funnel": status_funnel,
            "top_products": top_products_out,
            "top_vendors": top_vendors_out,
            "vendor_share_chart": [
                {"name": v["store_name"], "value": v["gross"], "orders": v["orders"]}
                for v in vendor_pie
            ],
            "recent_orders": recent_orders,
        }
    )
