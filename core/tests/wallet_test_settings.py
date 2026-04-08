"""Relax WalletSettings defaults for legacy API tests (OTP gates, min withdrawal)."""

from decimal import Decimal

from core.models import WalletSettings


def relax_wallet_settings_for_tests() -> None:
    ws = WalletSettings.load()
    ws.otp_for_withdrawals = False
    ws.otp_for_transfers_above = Decimal("999999999")
    ws.min_withdrawal = Decimal("0.01")
    ws.save(
        update_fields=[
            "otp_for_withdrawals",
            "otp_for_transfers_above",
            "min_withdrawal",
        ]
    )
