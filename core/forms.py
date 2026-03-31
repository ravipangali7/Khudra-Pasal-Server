"""
Custom admin / validation forms for KhudraPasal core models.
"""
from __future__ import annotations

import re

from django import forms
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from django.core.exceptions import ValidationError
from .models import Product, Refund, User, Vendor, WalletSettings

# Nepal mobile: +977- followed by 10 digits (common format in spec)
NEPAL_PHONE_PATTERN = re.compile(r"^\+977-\d{10}$")


class UserCreationAdminForm(UserCreationForm):
    """Admin user creation: phone as login (USERNAME_FIELD), username for wallet handle."""

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("phone", "username", "name", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "usable_password" in self.fields:
            pass
        self.fields["phone"].help_text = (
            "Login identifier. Format: +977-XXXXXXXXXX (10 digits after +977-)."
        )
        self.fields["username"].help_text = (
            "Public handle for wallet transfers (e.g. ramesh_sharma)."
        )

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "").strip()
        if not NEPAL_PHONE_PATTERN.match(phone):
            raise ValidationError(
                "Phone must match Nepal format +977-XXXXXXXXXX (exactly 10 digits after +977-)."
            )
        return phone


class UserChangeAdminForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "").strip()
        if phone and not NEPAL_PHONE_PATTERN.match(phone):
            raise ValidationError(
                "Phone must match Nepal format +977-XXXXXXXXXX (exactly 10 digits after +977-)."
            )
        return phone


class VendorApprovalForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = "__all__"

    def clean(self):
        data = super().clean()
        status = data.get("status")
        reason = (data.get("rejection_reason") or "").strip()
        if status in (Vendor.Status.REJECTED, Vendor.Status.SUSPENDED) and not reason:
            raise ValidationError(
                {
                    "rejection_reason": "Rejection or suspension reason is required for vendors."
                }
            )
        return data


class RefundProcessForm(forms.ModelForm):
    class Meta:
        model = Refund
        fields = "__all__"

    def clean(self):
        data = super().clean()
        status = data.get("status")
        note = (data.get("admin_note") or "").strip()
        if status == Refund.Status.REJECTED and not note:
            raise ValidationError(
                {"admin_note": "Admin note is required when rejecting a refund."}
            )
        return data


class WalletSettingsForm(forms.ModelForm):
    class Meta:
        model = WalletSettings
        fields = "__all__"
        help_texts = {
            "max_balance_per_user": "Maximum wallet balance per user (Rs.).",
            "daily_transfer_limit": "Max total outbound transfers per user per day (Rs.).",
            "min_withdrawal": "Minimum single withdrawal amount (Rs.).",
            "max_withdrawal_per_day": "Max total withdrawals per user per day (Rs.).",
            "transaction_fee_value": "If fee type is percentage, this is %; if flat, Rs. per transaction.",
            "vendor_settlement_days": "Days before vendor earnings are released.",
            "otp_for_transfers_above": "OTP required for peer transfers above this amount (Rs.).",
        }


class ProductAdminForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = "__all__"

    def clean(self):
        data = super().clean()
        price = data.get("price")
        discount = data.get("discount_price")
        if price is not None and discount is not None and discount >= price:
            raise ValidationError(
                {"discount_price": "Discount price must be less than regular price."}
            )
        return data
