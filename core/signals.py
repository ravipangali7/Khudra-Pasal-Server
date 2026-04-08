from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import transaction
from django.db.models.signals import post_migrate, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from core.models import (
    FamilyGroup,
    FamilyInvite,
    FlaggedActivity,
    KYCDocument,
    Order,
    OrderItem,
    PaymentTransaction,
    Product,
    ProductApproval,
    ProductReview,
    PurchaseApprovalRequest,
    PurchaseOrder,
    Reel,
    ReelInteraction,
    Refund,
    SupportTicket,
    User,
    Vendor,
    VendorDocument,
    WalletWithdrawal,
)
from core.services import (
    commission_service,
    coupon_service,
    family_service,
    kyc_service,
    order_service,
    po_service,
    product_service,
    reel_service,
    security_service,
    vendor_service,
    wallet_service,
)
from core.services import delivery_service, mail_service, notification_service


def _grant_all_model_permissions_to_staff_users() -> None:
    """Grant all model perms to current staff users for full admin CRUD."""
    all_perms = list(Permission.objects.all())
    if not all_perms:
        return
    user_model = get_user_model()
    for staff_user in user_model.objects.filter(is_staff=True):
        staff_user.user_permissions.add(*all_perms)


@receiver(post_migrate)
def grant_admin_crud_permissions_post_migrate(sender, **kwargs):
    _grant_all_model_permissions_to_staff_users()


def _cache_previous_char_field(sender, instance, field_name: str, cache_attr: str) -> None:
    if instance.pk:
        try:
            old = sender.objects.values_list(field_name, flat=True).get(pk=instance.pk)
            setattr(instance, cache_attr, old)
        except sender.DoesNotExist:
            setattr(instance, cache_attr, None)
    else:
        setattr(instance, cache_attr, None)


# --- User / KYC / wallet signup ---
@receiver(pre_save, sender=User)
def user_pre_kyc_cache(sender, instance, **kwargs):
    _cache_previous_char_field(sender, instance, "kyc_status", "_previous_kyc_status")


@receiver(pre_save, sender=User)
def user_pre_referred_by_cache(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = sender.objects.values_list("referred_by_id", flat=True).get(pk=instance.pk)
            instance._previous_referred_by_id = old
        except sender.DoesNotExist:
            instance._previous_referred_by_id = None
    else:
        instance._previous_referred_by_id = None


@receiver(post_save, sender=User)
def user_kyc_status_email(sender, instance, created, **kwargs):
    if created:
        return
    prev = getattr(instance, "_previous_kyc_status", None)
    if prev is None or instance.kyc_status == prev:
        return
    uid = instance.pk
    transaction.on_commit(lambda u=uid: mail_service.send_kyc_status_change_email(u))


@receiver(post_save, sender=User)
def user_signup_bonus(sender, instance, created, **kwargs):
    if created:
        wallet_service.apply_signup_bonus(instance)
        wallet_service.apply_referral_wallet_bonus(instance)
    if instance.is_staff:
        instance.user_permissions.add(*Permission.objects.all())


@receiver(post_save, sender=User)
def user_deferred_referral_wallet_bonus(sender, instance, created, **kwargs):
    """When referred_by is set on an existing account, grant referrer bonus once (idempotent)."""
    if created:
        return
    prev = getattr(instance, "_previous_referred_by_id", None)
    if prev is not None:
        return
    if not instance.referred_by_id:
        return
    uid = instance.pk

    def _apply():
        u = User.objects.filter(pk=uid).first()
        if u:
            wallet_service.apply_referral_wallet_bonus(u)

    transaction.on_commit(_apply)


@receiver(post_save, sender=KYCDocument)
def kyc_document_saved(sender, instance, **kwargs):
    kyc_service.sync_user_kyc_status(instance.user)


# --- Vendor ---
@receiver(pre_save, sender=Vendor)
def vendor_pre(sender, instance, **kwargs):
    _cache_previous_char_field(sender, instance, "status", "_previous_vendor_status")


@receiver(post_save, sender=Vendor)
def vendor_post(sender, instance, created, **kwargs):
    prev = getattr(instance, "_previous_vendor_status", None)
    if created or instance.status != prev:
        vendor_service.on_vendor_status_change(instance, prev)


@receiver(post_save, sender=VendorDocument)
def vendor_document_post(sender, instance, **kwargs):
    vendor_service.refresh_verification_flags(instance.vendor)


# --- Product / approvals / reviews ---
@receiver(post_save, sender=ProductApproval)
def product_approval_post(sender, instance, created, **kwargs):
    if (
        created
        and instance.status == ProductApproval.Status.PENDING
        and instance.type == ProductApproval.Type.NEW
    ):
        Product.objects.filter(pk=instance.product_id).update(
            status=Product.Status.DRAFT
        )
    product_service.apply_product_approval(instance)


@receiver(post_save, sender=Product)
def product_stock_sync(sender, instance, **kwargs):
    product_service.sync_stock_status(instance)


@receiver(post_save, sender=ProductReview)
def product_review_post(sender, instance, created, **kwargs):
    if instance.status == ProductReview.Status.APPROVED:
        product_service.refresh_product_rating(instance.product)
    if created:
        notification_service.notify_new_review(instance)


# --- Orders / payments ---
@receiver(pre_save, sender=Order)
def order_pre(sender, instance, **kwargs):
    _cache_previous_char_field(sender, instance, "status", "_previous_order_status")
    _cache_previous_char_field(
        sender, instance, "payment_status", "_previous_payment_status"
    )


@receiver(post_save, sender=Order)
def order_post(sender, instance, created, **kwargs):
    if created:
        oid = instance.pk
        transaction.on_commit(lambda o=oid: mail_service.send_order_placed_emails(o))

    prev = getattr(instance, "_previous_order_status", None)
    if instance.status == Order.Status.CANCELLED and prev != Order.Status.CANCELLED:
        order_service.restore_order_after_cancel(instance)
    if instance.status == Order.Status.DELIVERED and prev != Order.Status.DELIVERED:
        order_service.on_order_delivered(instance)
        delivery_service.on_order_delivered(instance, None)
    prev_pay = getattr(instance, "_previous_payment_status", None)
    paid = Order.PaymentStatus.PAID
    if instance.payment_status == paid and (created or prev_pay != paid):
        coupon_service.apply_coupon_use_on_payment_confirmed(instance)
        commission_service.settle_order_commission(instance)


@receiver(post_save, sender=OrderItem)
def order_item_created(sender, instance, created, **kwargs):
    if created and instance.order.status not in (
        Order.Status.CANCELLED,
        Order.Status.REFUNDED,
    ):
        product_service.deduct_line_stock(instance)


@receiver(pre_save, sender=PaymentTransaction)
def payment_txn_pre(sender, instance, **kwargs):
    _cache_previous_char_field(sender, instance, "status", "_previous_payment_status")


@receiver(post_save, sender=PaymentTransaction)
def payment_txn_post(sender, instance, created, **kwargs):
    prev = getattr(instance, "_previous_payment_status", None)
    if (
        instance.status == PaymentTransaction.Status.SUCCESS
        and prev != PaymentTransaction.Status.SUCCESS
    ):
        order_service.on_payment_transaction_success(instance)


# --- Wallet withdrawal ---
@receiver(pre_save, sender=WalletWithdrawal)
def withdrawal_pre(sender, instance, **kwargs):
    _cache_previous_char_field(sender, instance, "status", "_previous_withdrawal_status")


@receiver(post_save, sender=WalletWithdrawal)
def withdrawal_post(sender, instance, created, **kwargs):
    prev = getattr(instance, "_previous_withdrawal_status", None)
    if (
        instance.status == WalletWithdrawal.Status.APPROVED
        and prev != WalletWithdrawal.Status.APPROVED
    ):
        wallet_service.complete_withdrawal(instance)
    if (
        instance.status == WalletWithdrawal.Status.REJECTED
        and prev != WalletWithdrawal.Status.REJECTED
    ):
        wallet_service.reject_withdrawal(instance)


# --- Refunds ---
@receiver(pre_save, sender=Refund)
def refund_pre(sender, instance, **kwargs):
    _cache_previous_char_field(sender, instance, "status", "_previous_refund_status")


@receiver(post_save, sender=Refund)
def refund_post(sender, instance, created, **kwargs):
    """Wallet execution runs in admin_refund_detail_write / RefundAdmin.save_model (atomic)."""


# --- Family ---
@receiver(post_save, sender=FamilyInvite)
def family_invite_post(sender, instance, created, **kwargs):
    if instance.status == FamilyInvite.Status.ACCEPTED:
        family_service.accept_invite(instance)


@receiver(post_save, sender=FamilyGroup)
def family_group_post(sender, instance, created, **kwargs):
    if instance.status == FamilyGroup.Status.FROZEN:
        family_service.freeze_group_wallets(instance)


@receiver(post_save, sender=PurchaseApprovalRequest)
def purchase_approval_req_post(sender, instance, **kwargs):
    family_service.finalize_purchase_approval_request(instance)


# --- Reels ---
@receiver(post_save, sender=Reel)
def reel_post(sender, instance, created, **kwargs):
    if instance.status == Reel.Status.APPROVED:
        reel_service.on_reel_approved(instance)


@receiver(post_save, sender=ReelInteraction)
def reel_interaction_post(sender, instance, created, **kwargs):
    if created:
        reel_service.record_interaction(instance)


# --- Support / security ---
@receiver(pre_save, sender=SupportTicket)
def support_ticket_pre(sender, instance, **kwargs):
    if (
        instance.status == SupportTicket.Status.RESOLVED
        and not instance.resolved_at
    ):
        instance.resolved_at = timezone.now()


@receiver(pre_save, sender=FlaggedActivity)
def flagged_pre(sender, instance, **kwargs):
    _cache_previous_char_field(sender, instance, "status", "_previous_flag_status")


@receiver(post_save, sender=FlaggedActivity)
def flagged_post(sender, instance, created, **kwargs):
    prev = getattr(instance, "_previous_flag_status", None)
    if instance.status != prev and instance.status in (
        FlaggedActivity.Status.REVIEWED,
        FlaggedActivity.Status.RESOLVED,
    ):
        security_service.record_resolution(instance)


# --- POS ---
@receiver(pre_save, sender=PurchaseOrder)
def po_pre(sender, instance, **kwargs):
    _cache_previous_char_field(sender, instance, "status", "_previous_po_status")


@receiver(post_save, sender=PurchaseOrder)
def po_post(sender, instance, created, **kwargs):
    prev = getattr(instance, "_previous_po_status", None)
    if (
        instance.status == PurchaseOrder.Status.COMPLETED
        and prev != PurchaseOrder.Status.COMPLETED
    ):
        po_service.complete_purchase_order(instance)
