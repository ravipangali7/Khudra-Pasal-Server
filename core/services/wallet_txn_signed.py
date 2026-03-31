"""Signed amounts for wallet transactions (child + family member spending)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from collections import defaultdict

from core.models import Wallet, WalletTransaction


def signed_amount_for_wallet_transaction(t: WalletTransaction, w: Wallet) -> float:
    amt = float(t.amount)
    if t.type in (
        WalletTransaction.Type.TOPUP,
        WalletTransaction.Type.BONUS,
        WalletTransaction.Type.CREDIT,
    ):
        return amt
    if t.type in (
        WalletTransaction.Type.WITHDRAWAL,
        WalletTransaction.Type.PURCHASE,
        WalletTransaction.Type.DEBIT,
    ):
        return -amt
    if t.type == WalletTransaction.Type.TRANSFER:
        if t.to_wallet_id == w.pk:
            return amt
        if t.from_wallet_id == w.pk:
            return -amt
        return amt
    return amt


def month_start_local(now: datetime | None = None) -> datetime:
    n = now or timezone.now()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def sum_monthly_spent_from_wallet(w: Wallet | None, *, now=None) -> Decimal:
    """Sum of outflows (absolute) for completed txns in the current calendar month."""
    if not w:
        return Decimal("0")
    d = aggregate_monthly_spent_for_wallet_ids([w.pk], now=now)
    return d.get(w.pk, Decimal("0"))


def aggregate_monthly_spent_for_wallet_ids(
    wallet_ids: list[int],
    *,
    now=None,
) -> dict[int, Decimal]:
    """Per-wallet sum of outflow amounts (absolute) this calendar month."""
    ids = [i for i in wallet_ids if i]
    if not ids:
        return {}
    start = month_start_local(now)
    wallets = {w.pk: w for w in Wallet.objects.filter(pk__in=ids)}
    out: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    qs = WalletTransaction.objects.filter(
        wallet_id__in=ids,
        created_at__gte=start,
        status=WalletTransaction.Status.COMPLETED,
    ).only("wallet_id", "type", "amount", "from_wallet_id", "to_wallet_id")
    for t in qs.iterator(chunk_size=400):
        w = wallets.get(t.wallet_id)
        if not w:
            continue
        s = signed_amount_for_wallet_transaction(t, w)
        if s < 0:
            out[t.wallet_id] += Decimal(str(abs(s)))
    return dict(out)
