from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.authentication import SessionAuthentication, TokenAuthentication

from core.models import DeliveryMan, Order, User, Vendor, Wallet
from core.serializers import RecentOrderSerializer


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
    queryset = Order.objects.select_related("customer", "seller").order_by("-created_at")[:limit]
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
    start_date = timezone.now() - timedelta(days=days - 1)
    queryset = (
        Order.objects.filter(created_at__gte=start_date)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(sales=Sum("total"), orders=Count("id"))
        .order_by("day")
    )

    data = [
        {
            "day": row["day"].isoformat(),
            "sales": float(row["sales"] or Decimal("0")),
            "orders": row["orders"],
        }
        for row in queryset
    ]
    return Response(data)

