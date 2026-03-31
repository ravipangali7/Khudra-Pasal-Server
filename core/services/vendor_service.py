from __future__ import annotations

from django.db import transaction
from core.models import Product, Vendor, VendorDocument, Wallet


@transaction.atomic
def ensure_vendor_wallet(vendor: Vendor) -> Wallet:
    w, _ = Wallet.objects.get_or_create(
        vendor=vendor,
        defaults={
            "type": Wallet.Type.VENDOR,
            "status": Wallet.Status.ACTIVE,
            "label": vendor.store_name[:100],
        },
    )
    return w


@transaction.atomic
def apply_vendor_suspension(vendor: Vendor) -> None:
    if vendor.status not in (
        Vendor.Status.REJECTED,
        Vendor.Status.SUSPENDED,
    ):
        return
    Wallet.objects.filter(vendor=vendor).update(status=Wallet.Status.FROZEN)
    Vendor.objects.filter(pk=vendor.pk).update(can_sell=False, can_post=False)
    Product.objects.filter(seller=vendor).exclude(status=Product.Status.DRAFT).update(
        status=Product.Status.DRAFT
    )


@transaction.atomic
def refresh_verification_flags(vendor: Vendor) -> None:
    qs = VendorDocument.objects.filter(vendor=vendor)
    pending = qs.filter(status=VendorDocument.Status.PENDING).exists()
    has_verified = qs.filter(status=VendorDocument.Status.VERIFIED).exists()
    is_verified = has_verified and not pending
    Vendor.objects.filter(pk=vendor.pk).update(is_verified=is_verified)


@transaction.atomic
def on_vendor_status_change(vendor: Vendor, previous: str | None) -> None:
    if vendor.status == Vendor.Status.APPROVED:
        ensure_vendor_wallet(vendor)
        Wallet.objects.filter(vendor=vendor).update(status=Wallet.Status.ACTIVE)
    if vendor.status in (Vendor.Status.REJECTED, Vendor.Status.SUSPENDED):
        apply_vendor_suspension(vendor)
