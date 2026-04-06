"""Child spending limits: order totals paid from non-personal wallets (per FamilyMember)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db.models import Sum
from django.utils import timezone

from core.models import FamilyMember, Order, User, Wallet

if TYPE_CHECKING:
    pass


def active_child_family_member(user: User) -> FamilyMember | None:
    if user.role != User.Role.CHILD:
        return None
    return (
        FamilyMember.objects.filter(
            user=user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .select_related("group")
        .first()
    )


def _local_now(now=None):
    return timezone.localtime(now)


def day_start_local(now=None):
    n = _local_now(now)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start_local_monday(now=None):
    """Calendar week: Monday 00:00 through following Monday 00:00 (local)."""
    start_of_day = day_start_local(now)
    return start_of_day - timedelta(days=start_of_day.weekday())


def month_start_local(now=None):
    n = _local_now(now)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_end_exclusive_local(now=None):
    start = month_start_local(now)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def sum_non_personal_wallet_order_totals(
    user: User,
    start,
    end,
) -> Decimal:
    """Sum order totals for PAID wallet checkouts excluding personal payment_wallet."""
    agg = (
        Order.objects.filter(
            customer=user,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            payment_wallet__isnull=False,
            created_at__gte=start,
            created_at__lt=end,
        )
        .exclude(payment_wallet__type=Wallet.Type.PERSONAL)
        .aggregate(s=Sum("total"))
    )
    return agg["s"] if agg["s"] is not None else Decimal("0")


def child_non_personal_spent_windows(user: User, *, now=None) -> dict[str, Decimal]:
    """Spent amounts (order totals) for current local day, week, and month."""
    d0, d1 = day_start_local(now), day_start_local(now) + timedelta(days=1)
    w0 = week_start_local_monday(now)
    w1 = w0 + timedelta(days=7)
    m0, m1 = month_start_local(now), month_end_exclusive_local(now)
    return {
        "daily": sum_non_personal_wallet_order_totals(user, d0, d1),
        "weekly": sum_non_personal_wallet_order_totals(user, w0, w1),
        "monthly": sum_non_personal_wallet_order_totals(user, m0, m1),
    }


def validate_child_spending_limits(
    user: User,
    pay_wallet: Wallet,
    order_total: Decimal,
) -> None:
    """
    Raise ValueError if checkout would exceed configured limits.

    Skips when paying from personal wallet. Only applies to child accounts with
    an active CHILD FamilyMember row.
    """
    if pay_wallet.type == Wallet.Type.PERSONAL:
        return
    fm = active_child_family_member(user)
    if not fm:
        return

    lim_d = fm.spending_limit_daily or Decimal("0")
    lim_w = fm.spending_limit_weekly or Decimal("0")
    lim_m = fm.spending_limit_monthly or Decimal("0")
    if lim_d <= 0 and lim_w <= 0 and lim_m <= 0:
        return

    spent = child_non_personal_spent_windows(user)
    if lim_d > 0 and spent["daily"] + order_total > lim_d:
        raise ValueError(
            "This order would exceed your daily spending limit "
            f"(Rs. {lim_d})."
        )
    if lim_w > 0 and spent["weekly"] + order_total > lim_w:
        raise ValueError(
            "This order would exceed your weekly spending limit "
            f"(Rs. {lim_w})."
        )
    if lim_m > 0 and spent["monthly"] + order_total > lim_m:
        raise ValueError(
            "This order would exceed your monthly spending limit "
            f"(Rs. {lim_m})."
        )
