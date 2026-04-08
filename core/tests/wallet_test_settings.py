"""Relax WalletSettings defaults for legacy API tests (OTP gates, min withdrawal)."""

from decimal import Decimal

from core.models import SecuritySettings, WalletSettings


def relax_wallet_settings_for_tests() -> None:
    ws = WalletSettings.load()
    ws.otp_for_withdrawals = False
    # WalletSettings.otp_for_transfers_above max_digits=10, decimal_places=2
    ws.otp_for_transfers_above = Decimal("99999999.99")
    ws.min_withdrawal = Decimal("0.01")
    ws.save(
        update_fields=[
            "otp_for_withdrawals",
            "otp_for_transfers_above",
            "min_withdrawal",
        ]
    )
    ss = SecuritySettings.load()
    ss.otp_sensitive_crud = False
    ss.save(update_fields=["otp_sensitive_crud"])
