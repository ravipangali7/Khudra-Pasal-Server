"""In-app notifications for wallet withdrawal lifecycle."""

from __future__ import annotations

from decimal import Decimal

from core.models import Notification, User, WalletWithdrawal


def notify_customer_wallet(user: User, title: str, message: str, action_url: str = "") -> None:
    Notification.objects.create(
        recipient=user,
        title=title[:150],
        message=message,
        type=Notification.Type.WALLET,
        target=Notification.Target.CUSTOMERS,
        action_url=(action_url or "")[:255],
    )


def notify_admin_wallet(user: User, title: str, message: str, action_url: str = "") -> None:
    Notification.objects.create(
        recipient=user,
        title=title[:150],
        message=message,
        type=Notification.Type.WALLET,
        target=Notification.Target.ADMINS,
        action_url=(action_url or "")[:255],
    )


def notify_family_withdrawal_submitted(
    withdrawal: WalletWithdrawal, submitter: User, *, submitter_name: str = ""
) -> None:
    name = (submitter_name or submitter.name or submitter.phone or "User").strip()
    amt = withdrawal.amount
    if isinstance(amt, Decimal):
        amt_str = f"{amt:.2f}"
    else:
        amt_str = str(amt)
    notify_customer_wallet(
        submitter,
        "Withdrawal request submitted",
        f"Your request {withdrawal.withdrawal_number} for Rs. {amt_str} is pending admin review.",
        "/family-portal/wallets-withdraw",
    )
    msg = (
        f"Family wallet withdrawal {withdrawal.withdrawal_number} from {name} — "
        f"Rs. {amt_str} (pending)."
    )
    for admin in User.objects.filter(is_superuser=True, is_active=True):
        notify_admin_wallet(
            admin,
            "New withdrawal request",
            msg,
            "/admin/withdrawals",
        )


def _withdrawal_portal_action_url(withdrawal: WalletWithdrawal) -> str:
    w = withdrawal.wallet
    if getattr(w, "family_group_id", None):
        return "/family-portal/wallets-withdraw"
    if getattr(w, "vendor_id", None):
        return "/vendor/withdrawals"
    return "/portal/wallet-withdraw"


def notify_withdrawal_approved(withdrawal: WalletWithdrawal) -> None:
    user = _payout_user(withdrawal)
    if not user:
        return
    amt = withdrawal.amount
    amt_str = f"{amt:.2f}" if isinstance(amt, Decimal) else str(amt)
    notify_customer_wallet(
        user,
        "Withdrawal approved",
        f"Your withdrawal {withdrawal.withdrawal_number} for Rs. {amt_str} has been approved.",
        _withdrawal_portal_action_url(withdrawal),
    )


def notify_withdrawal_rejected(withdrawal: WalletWithdrawal) -> None:
    user = _payout_user(withdrawal)
    if not user:
        return
    amt = withdrawal.amount
    amt_str = f"{amt:.2f}" if isinstance(amt, Decimal) else str(amt)
    reason = (withdrawal.reject_reason or "").strip()
    tail = f" Reason: {reason}" if reason else ""
    notify_customer_wallet(
        user,
        "Withdrawal rejected",
        f"Your withdrawal {withdrawal.withdrawal_number} for Rs. {amt_str} was rejected.{tail}",
        _withdrawal_portal_action_url(withdrawal),
    )


def _payout_user(withdrawal: WalletWithdrawal) -> User | None:
    pid = withdrawal.payout_account_id
    if not pid:
        return None
    from core.models import PayoutAccount

    pa = PayoutAccount.objects.filter(pk=pid).select_related("user").first()
    return pa.user if pa else None
