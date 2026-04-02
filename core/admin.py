"""
Django admin for KhudraPasal — Jazzmin-friendly, module-oriented UX.
"""
from __future__ import annotations

from django.apps import apps
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import JSONField
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.html import format_html
from django.utils import timezone

from . import models
from .forms import (
    ProductAdminForm,
    RefundProcessForm,
    UserChangeAdminForm,
    UserCreationAdminForm,
    VendorApprovalForm,
    WalletSettingsForm,
)


# --- List filters -------------------------------------------------------------

class KYCStatusFilter(admin.SimpleListFilter):
    title = "KYC status"
    parameter_name = "kyc_status"

    def lookups(self, request, model_admin):
        return models.User.KYCStatus.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(kyc_status=self.value())
        return queryset


class OrderStatusFilter(admin.SimpleListFilter):
    title = "order status"
    parameter_name = "order_status"

    def lookups(self, request, model_admin):
        return models.Order.Status.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


class VendorStatusFilter(admin.SimpleListFilter):
    title = "vendor status"
    parameter_name = "vendor_status"

    def lookups(self, request, model_admin):
        return models.Vendor.Status.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


class WalletTypeFilter(admin.SimpleListFilter):
    title = "wallet type"
    parameter_name = "wallet_type"

    def lookups(self, request, model_admin):
        return models.Wallet.Type.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(type=self.value())
        return queryset


class ProductStatusFilter(admin.SimpleListFilter):
    title = "product status"
    parameter_name = "product_status"

    def lookups(self, request, model_admin):
        return models.Product.Status.choices

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(status=self.value())
        return queryset


def _badge(text: str, color: str = "#6c757d") -> str:
    return format_html(
        '<span style="padding:2px 8px;border-radius:4px;background:{};color:#fff;font-size:11px;">{}</span>',
        color,
        text,
    )


# --- Inlines ------------------------------------------------------------------

class KYCDocumentInline(admin.TabularInline):
    model = models.KYCDocument
    fk_name = "user"
    extra = 0
    readonly_fields = ("submitted_at", "reviewed_at")
    autocomplete_fields = ("reviewer",)


class OrderItemInline(admin.TabularInline):
    model = models.OrderItem
    extra = 0
    autocomplete_fields = ("product",)
    readonly_fields = ("total_price",)


class DeliveryAddressInline(admin.StackedInline):
    model = models.DeliveryAddress
    extra = 0
    max_num = 1
    can_delete = False


class ProductImageInline(admin.TabularInline):
    model = models.ProductImage
    extra = 0


class ProductApprovalInline(admin.StackedInline):
    model = models.ProductApproval
    extra = 0
    autocomplete_fields = ("reviewer",)


class VendorDocumentInline(admin.TabularInline):
    model = models.VendorDocument
    extra = 0
    readonly_fields = ("submitted_at", "reviewed_at")
    autocomplete_fields = ("reviewer",)


class VendorImpersonationLogInline(admin.TabularInline):
    model = models.VendorImpersonationLog
    extra = 0
    readonly_fields = ("timestamp", "expires_at", "session_token")
    autocomplete_fields = ("admin",)


class WalletTransactionInline(admin.TabularInline):
    model = models.WalletTransaction
    fk_name = "wallet"
    extra = 0
    readonly_fields = ("txn_id", "created_at")
    autocomplete_fields = ("from_wallet", "to_wallet", "performed_by")


class FamilyMemberInline(admin.TabularInline):
    model = models.FamilyMember
    extra = 0
    autocomplete_fields = ("user",)


class FamilyGroupPermissionInline(admin.StackedInline):
    model = models.FamilyGroupPermission
    extra = 0
    max_num = 1
    can_delete = False


class AutoApprovalRuleInline(admin.TabularInline):
    model = models.AutoApprovalRule
    extra = 0
    autocomplete_fields = ("category",)


class TimeBasedRuleInline(admin.TabularInline):
    model = models.TimeBasedRule
    extra = 0


class FlashDealProductInline(admin.TabularInline):
    model = models.FlashDealProduct
    extra = 0
    autocomplete_fields = ("product",)


class ReelInteractionInline(admin.TabularInline):
    model = models.ReelInteraction
    extra = 0
    autocomplete_fields = ("user",)


class PurchaseOrderLineInline(admin.TabularInline):
    model = models.PurchaseOrderLine
    extra = 0
    autocomplete_fields = ("product",)


# --- User ---------------------------------------------------------------------

@admin.register(models.User)
class UserAdmin(BaseUserAdmin):
    add_form = UserCreationAdminForm
    form = UserChangeAdminForm
    ordering = ("phone",)
    list_display = (
        "phone",
        "username",
        "name",
        "KID_col",
        "role",
        "kyc_badge",
        "is_active",
        "is_staff",
        "created_at",
    )
    list_filter = ("role", KYCStatusFilter, "is_active", "is_staff", "is_superuser")
    search_fields = ("phone", "username", "name", "KID", "email")
    readonly_fields = ("KID", "created_at", "updated_at", "last_login", "date_joined")
    inlines = [KYCDocumentInline]
    autocomplete_fields = ()

    fieldsets = (
        (None, {"fields": ("phone", "password")}),
        (
            "Profile",
            {"fields": ("username", "name", "email", "avatar")},
        ),
        (
            "KhudraPasal",
            {
                "fields": (
                    "KID",
                    "role",
                    "kyc_status",
                    "social_provider",
                    "social_provider_id",
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (
            "Dates",
            {
                "fields": (
                    "last_login",
                    "date_joined",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("phone", "username", "name", "email", "password1", "password2"),
            },
        ),
    )

    @admin.display(description="KID")
    def KID_col(self, obj):
        return obj.KID or "—"

    @admin.display(description="KYC")
    def kyc_badge(self, obj):
        colors = {
            "pending": "#6c757d",
            "review": "#fd7e14",
            "verified": "#198754",
            "rejected": "#dc3545",
        }
        return _badge(obj.get_kyc_status_display(), colors.get(obj.kyc_status, "#6c757d"))

    @admin.action(description="Activate selected users")
    def activate_users(self, request, queryset):
        queryset.update(is_active=True)
        self.message_user(request, "Users activated.", messages.SUCCESS)

    @admin.action(description="Deactivate selected users")
    def deactivate_users(self, request, queryset):
        queryset.update(is_active=False)
        self.message_user(request, "Users deactivated.", messages.WARNING)

    @admin.action(description="Mark KYC as verified (bulk)")
    def verify_kyc_bulk(self, request, queryset):
        queryset.update(kyc_status=models.User.KYCStatus.VERIFIED)
        self.message_user(request, "KYC marked verified.", messages.SUCCESS)

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff

    def has_add_permission(self, request):
        return request.user.is_staff

    def has_change_permission(self, request, obj=None):
        return request.user.is_staff

    def has_delete_permission(self, request, obj=None):
        return request.user.is_staff

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)

    actions = ["delete_selected", "activate_users", "deactivate_users", "verify_kyc_bulk"]


# --- Orders -------------------------------------------------------------------

@admin.register(models.Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_number",
        "customer",
        "seller",
        "status_badge",
        "payment_status",
        "total",
        "created_at",
    )
    list_filter = (OrderStatusFilter, "payment_status", "payment_method", "is_pos_order")
    search_fields = ("order_number", "customer__phone", "customer__name", "notes")
    readonly_fields = ("created_at", "updated_at")
    inlines = [OrderItemInline, DeliveryAddressInline]
    autocomplete_fields = ("customer", "seller", "coupon")
    date_hierarchy = "created_at"

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "pending": "#6c757d",
            "processing": "#0d6efd",
            "shipped": "#6610f2",
            "delivered": "#198754",
            "cancelled": "#dc3545",
            "refunded": "#fd7e14",
        }
        return _badge(obj.get_status_display(), colors.get(obj.status, "#6c757d"))

    @admin.action(description="Mark as processing")
    def mark_processing(self, request, queryset):
        queryset.update(status=models.Order.Status.PROCESSING)
        self.message_user(request, "Updated to processing.", messages.SUCCESS)

    @admin.action(description="Mark as shipped")
    def mark_shipped(self, request, queryset):
        queryset.update(status=models.Order.Status.SHIPPED)
        self.message_user(request, "Updated to shipped.", messages.SUCCESS)

    @admin.action(description="Mark as delivered")
    def mark_delivered(self, request, queryset):
        queryset.update(status=models.Order.Status.DELIVERED)
        self.message_user(request, "Updated to delivered.", messages.SUCCESS)

    actions = ["mark_processing", "mark_shipped", "mark_delivered"]


@admin.register(models.OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "product", "quantity", "unit_price", "total_price")
    autocomplete_fields = ("order", "product")


@admin.register(models.OrderCommissionSettlement)
class OrderCommissionSettlementAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "vendor",
        "total_amount",
        "commission_percent",
        "commission_amount",
        "vendor_amount",
        "payment_status",
        "created_at",
    )
    list_filter = ("payment_status",)
    search_fields = ("order__order_number", "vendor__store_name")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("order", "vendor")


@admin.register(models.DeliveryAddress)
class DeliveryAddressAdmin(admin.ModelAdmin):
    list_display = ("order", "full_name", "mobile", "area_location")
    search_fields = ("full_name", "mobile", "area_location")


@admin.register(models.Refund)
class RefundAdmin(admin.ModelAdmin):
    form = RefundProcessForm
    list_display = ("refund_number", "order", "customer", "amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("refund_number", "customer__phone", "order__order_number")
    readonly_fields = ("created_at", "processed_at")
    autocomplete_fields = ("order", "customer")

    @admin.action(description="Approve refunds (set processed time)")
    def approve_refunds(self, request, queryset):
        now = timezone.now()
        queryset.filter(status=models.Refund.Status.PENDING).update(
            status=models.Refund.Status.APPROVED, processed_at=now
        )
        self.message_user(request, "Pending refunds approved.", messages.SUCCESS)

    actions = ["approve_refunds"]


@admin.register(models.PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("po_number", "customer", "seller", "total", "status", "created_at")
    list_filter = ("status", "payment_method")
    search_fields = ("po_number",)
    autocomplete_fields = ("customer", "seller")
    inlines = [PurchaseOrderLineInline]


@admin.register(models.PurchaseOrderLine)
class PurchaseOrderLineAdmin(admin.ModelAdmin):
    list_display = ("purchase_order", "product", "quantity", "unit_price", "line_total")
    autocomplete_fields = ("purchase_order", "product")


@admin.register(models.Cart)
class CartAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "created_at", "updated_at")
    search_fields = ("user__phone", "user__name")
    autocomplete_fields = ("user",)


@admin.register(models.CartItem)
class CartItemAdmin(admin.ModelAdmin):
    list_display = ("cart", "product", "quantity", "updated_at")
    autocomplete_fields = ("cart", "product")


# --- Products & catalog -------------------------------------------------------

@admin.register(models.Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "level", "parent", "status", "sort_order")
    list_filter = ("status", "level")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("parent",)


@admin.register(models.Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "status")
    list_filter = ("status",)
    search_fields = ("name",)  # required for ProductAdmin.autocomplete_fields


@admin.register(models.Attribute)
class AttributeAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "status")
    list_filter = ("type", "status")
    search_fields = ("name",)


@admin.register(models.AttributeValue)
class AttributeValueAdmin(admin.ModelAdmin):
    list_display = ("attribute", "value", "sort_order", "status")
    list_filter = ("status",)
    autocomplete_fields = ("attribute",)


@admin.register(models.Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "type", "status")
    list_filter = ("type", "status")
    search_fields = ("name", "short_name")


@admin.register(models.Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    list_display = (
        "name",
        "sku",
        "price",
        "stock_indicator",
        "status_badge",
        "is_featured",
        "is_bestseller",
        "seller",
        "updated_at",
    )
    list_filter = (ProductStatusFilter, "type", "is_featured", "is_bestseller", "enable_pos", "enable_reels")
    search_fields = ("name", "sku", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("rating", "review_count", "created_at", "updated_at")
    inlines = [ProductImageInline, ProductApprovalInline]
    autocomplete_fields = ("category", "brand", "unit", "seller")
    date_hierarchy = "created_at"

    @admin.display(description="Stock")
    def stock_indicator(self, obj):
        if obj.stock == 0:
            return _badge("0", "#dc3545")
        if obj.stock < 10:
            return _badge(str(obj.stock), "#fd7e14")
        return _badge(str(obj.stock), "#198754")

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "draft": "#6c757d",
            "active": "#198754",
            "out_of_stock": "#dc3545",
        }
        return _badge(obj.get_status_display(), colors.get(obj.status, "#6c757d"))


@admin.register(models.ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ("product", "sort_order")
    autocomplete_fields = ("product",)


@admin.register(models.ProductApproval)
class ProductApprovalAdmin(admin.ModelAdmin):
    list_display = ("product", "vendor", "type", "status", "submitted_at")
    list_filter = ("status", "type")
    autocomplete_fields = ("product", "vendor", "reviewer")


@admin.register(models.ProductReview)
class ProductReviewAdmin(admin.ModelAdmin):
    list_display = ("product", "customer", "rating", "status", "created_at")
    list_filter = ("status", "rating")
    autocomplete_fields = ("product", "customer")


@admin.register(models.ProductWishlist)
class ProductWishlistAdmin(admin.ModelAdmin):
    list_display = ("user", "product", "created_at")
    autocomplete_fields = ("user", "product")


# --- Vendor -------------------------------------------------------------------

@admin.register(models.Vendor)
class VendorAdmin(admin.ModelAdmin):
    form = VendorApprovalForm
    list_display = (
        "store_name",
        "store_slug",
        "user",
        "status_badge",
        "is_verified",
        "can_sell",
        "commission_rate",
        "created_at",
    )
    list_filter = (VendorStatusFilter, "is_verified", "can_post", "can_sell")
    search_fields = ("store_name", "store_slug", "phone", "user__phone")
    prepopulated_fields = {"store_slug": ("store_name",)}
    readonly_fields = ("rating", "created_at")
    inlines = [VendorDocumentInline, VendorImpersonationLogInline]
    autocomplete_fields = ("user",)

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "pending": "#fd7e14",
            "approved": "#198754",
            "rejected": "#dc3545",
            "suspended": "#6c757d",
        }
        return _badge(obj.get_status_display(), colors.get(obj.status, "#6c757d"))

    @admin.action(description="Approve selected vendors")
    def approve_vendor(self, request, queryset):
        queryset.update(status=models.Vendor.Status.APPROVED, rejection_reason="")
        self.message_user(request, "Vendors approved.", messages.SUCCESS)

    @admin.action(description="Suspend selected vendors")
    def suspend_vendor(self, request, queryset):
        queryset.update(status=models.Vendor.Status.SUSPENDED)
        self.message_user(request, "Vendors suspended.", messages.WARNING)

    actions = ["approve_vendor", "suspend_vendor"]


@admin.register(models.VendorDocument)
class VendorDocumentAdmin(admin.ModelAdmin):
    list_display = ("vendor", "type", "status", "submitted_at")
    list_filter = ("status", "type")
    autocomplete_fields = ("vendor", "reviewer")


@admin.register(models.VendorBankDetail)
class VendorBankDetailAdmin(admin.ModelAdmin):
    list_display = ("vendor", "bank_name", "account_holder")
    autocomplete_fields = ("vendor",)


@admin.register(models.VendorImpersonationLog)
class VendorImpersonationLogAdmin(admin.ModelAdmin):
    list_display = ("admin", "vendor", "timestamp", "expires_at", "notify_vendor")
    autocomplete_fields = ("admin", "vendor")


# --- Wallet -------------------------------------------------------------------

@admin.register(models.Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "type",
        "owner",
        "vendor",
        "balance_display",
        "currency",
        "status_badge",
        "family_group",
    )
    list_filter = (WalletTypeFilter, "status", "currency")
    search_fields = ("owner__phone", "owner__name", "label", "vendor__store_name")
    readonly_fields = ("created_at", "updated_at")
    inlines = [WalletTransactionInline]
    autocomplete_fields = ("owner", "vendor", "family_group")

    @admin.display(description="Balance")
    def balance_display(self, obj):
        return format_html("<strong>Rs. {}</strong>", obj.balance)

    @admin.display(description="Status")
    def status_badge(self, obj):
        c = "#198754" if obj.status == models.Wallet.Status.ACTIVE else "#dc3545"
        return _badge(obj.get_status_display(), c)

    @admin.action(description="Freeze wallets")
    def freeze_wallets(self, request, queryset):
        queryset.update(status=models.Wallet.Status.FROZEN)
        self.message_user(request, "Wallets frozen.", messages.WARNING)

    @admin.action(description="Unfreeze wallets")
    def unfreeze_wallets(self, request, queryset):
        queryset.update(status=models.Wallet.Status.ACTIVE)
        self.message_user(request, "Wallets active.", messages.SUCCESS)

    actions = ["freeze_wallets", "unfreeze_wallets"]


@admin.register(models.WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ("txn_id", "wallet", "type", "amount", "fund_source", "status", "created_at")
    list_filter = ("type", "status")
    search_fields = ("txn_id", "description", "reference_id")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("wallet", "from_wallet", "to_wallet", "performed_by")


@admin.register(models.WalletWithdrawal)
class WalletWithdrawalAdmin(admin.ModelAdmin):
    list_display = ("withdrawal_number", "wallet", "amount", "method", "status", "created_at")
    list_filter = ("status", "method")
    search_fields = ("withdrawal_number",)
    readonly_fields = ("created_at", "processed_at")
    autocomplete_fields = ("wallet",)


@admin.register(models.WalletBonus)
class WalletBonusAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "amount", "status", "used_count", "expires_at")
    list_filter = ("status", "type")


@admin.register(models.LoyaltyRule)
class LoyaltyRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "event", "multiplier", "status")
    list_filter = ("status", "event")


@admin.register(models.LoyaltySettings)
class LoyaltySettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "loyalty_program_enabled",
        "points_per_currency_unit",
        "redeem_points_per_currency",
        "min_redeem_points",
        "updated_at",
    )

    def has_add_permission(self, request):
        return not models.LoyaltySettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if models.LoyaltySettings.objects.exists():
            return redirect(reverse("admin:core_loyaltysettings_change", args=(1,)))
        return super().changelist_view(request, extra_context)


# --- Family -------------------------------------------------------------------

@admin.register(models.FamilyGroup)
class FamilyGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "leader", "type", "status", "created_at")
    list_filter = ("type", "status")
    search_fields = ("name", "leader__phone", "leader__name")
    inlines = [
        FamilyMemberInline,
        FamilyGroupPermissionInline,
        AutoApprovalRuleInline,
        TimeBasedRuleInline,
    ]
    autocomplete_fields = ("leader",)


@admin.register(models.FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    list_display = ("group", "user", "role", "status", "joined_at")
    list_filter = ("role", "status")
    search_fields = ("user__phone", "user__name", "group__name")
    autocomplete_fields = ("group", "user")


@admin.register(models.FamilyInvite)
class FamilyInviteAdmin(admin.ModelAdmin):
    list_display = ("group", "invite_method", "role", "status", "expires_at")
    list_filter = ("status", "invite_method", "role")
    search_fields = ("token", "phone")
    autocomplete_fields = ("group", "invited_by")


@admin.register(models.FamilyWalletCategory)
class FamilyWalletCategoryAdmin(admin.ModelAdmin):
    list_display = ("group", "name", "sort_order", "created_at")
    autocomplete_fields = ("group",)
    formfield_overrides = {
        JSONField: {"widget": admin.widgets.AdminTextareaWidget(attrs={"rows": 4, "cols": 80})},
    }


@admin.register(models.FamilyPortalJoinLink)
class FamilyPortalJoinLinkAdmin(admin.ModelAdmin):
    list_display = ("group", "token", "default_role", "created_by", "expires_at", "revoked_at", "created_at")
    list_filter = ("default_role",)
    search_fields = ("token", "title", "group__name")
    autocomplete_fields = ("group", "created_by")


@admin.register(models.FamilyJoinRequest)
class FamilyJoinRequestAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "group", "status", "role", "source", "created_at")
    list_filter = ("status", "source", "role")
    search_fields = ("name", "phone", "email", "group__name")
    autocomplete_fields = ("group", "requested_by", "invite", "join_link", "reviewed_by")


@admin.register(models.FamilyGroupPermission)
class FamilyGroupPermissionAdmin(admin.ModelAdmin):
    list_display = ("group", "allow_online_purchases", "daily_spending_limit", "updated_at")
    autocomplete_fields = ("group",)


@admin.register(models.ProductRestriction)
class ProductRestrictionAdmin(admin.ModelAdmin):
    list_display = ("group", "family_member", "category", "is_blocked", "requires_approval")
    autocomplete_fields = ("group", "family_member", "category")


@admin.register(models.AutoApprovalRule)
class AutoApprovalRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "group", "category", "max_amount", "is_enabled")
    list_filter = ("is_enabled",)
    autocomplete_fields = ("group", "category")


@admin.register(models.TimeBasedRule)
class TimeBasedRuleAdmin(admin.ModelAdmin):
    list_display = ("group", "rule_type", "start_time", "end_time", "is_enabled")
    list_filter = ("rule_type", "is_enabled")
    autocomplete_fields = ("group",)


@admin.register(models.PurchaseApprovalRequest)
class PurchaseApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ("child", "parent", "product", "amount", "status", "created_at")
    list_filter = ("status",)
    autocomplete_fields = ("child", "parent", "product")


# --- Marketing ----------------------------------------------------------------

@admin.register(models.Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("code", "type", "value", "status", "used_count", "expires_at", "vendor")
    list_filter = ("status", "type")
    search_fields = ("code",)
    autocomplete_fields = ("vendor", "category")


@admin.register(models.FlashDeal)
class FlashDealAdmin(admin.ModelAdmin):
    list_display = ("name", "vendor", "discount_percent", "start_at", "end_at", "status", "priority")
    list_filter = ("status",)
    search_fields = ("name",)
    autocomplete_fields = ("vendor",)
    inlines = [FlashDealProductInline]
    date_hierarchy = "start_at"


@admin.register(models.FlashDealProduct)
class FlashDealProductAdmin(admin.ModelAdmin):
    list_display = ("flash_deal", "product", "override_price")
    autocomplete_fields = ("flash_deal", "product")


@admin.register(models.Banner)
class BannerAdmin(admin.ModelAdmin):
    list_display = ("title", "placement", "sort_order", "status", "click_count", "start_date", "end_date")
    list_filter = ("placement", "status")
    autocomplete_fields = ("category",)


# --- FAQs ---------------------------------------------------------------------

@admin.register(models.FAQ)
class FAQAdmin(admin.ModelAdmin):
    list_display = ("question", "surface", "sort_order", "is_published", "updated_at")
    list_filter = ("surface", "is_published")


# --- CMS ----------------------------------------------------------------------

@admin.register(models.CMSPage)
class CMSPageAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "status", "last_updated")
    list_filter = ("status",)
    prepopulated_fields = {"slug": ("title",)}
    search_fields = ("title", "slug")


@admin.register(models.BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "author", "status", "published_at", "created_at")
    list_filter = ("status",)
    prepopulated_fields = {"slug": ("title",)}
    search_fields = ("title", "slug")
    autocomplete_fields = ("author",)


# --- Shipping / delivery -------------------------------------------------------

@admin.register(models.ShippingMethod)
class ShippingMethodAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "cost", "free_threshold", "status")
    list_filter = ("type", "status")


@admin.register(models.ShippingZone)
class ShippingZoneAdmin(admin.ModelAdmin):
    list_display = ("name", "flat_rate", "free_above", "status")
    list_filter = ("status",)
    search_fields = ("name", "areas")


@admin.register(models.ShippingSettings)
class ShippingSettingsAdmin(admin.ModelAdmin):
    list_display = ("id", "seller_pays_shipping", "free_shipping_global", "default_zone", "updated_at")
    autocomplete_fields = ("default_zone",)

    def has_add_permission(self, request):
        return not models.ShippingSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if models.ShippingSettings.objects.exists():
            return redirect(reverse("admin:core_shippingsettings_change", args=(1,)))
        return super().changelist_view(request, extra_context)


@admin.register(models.WeightRule)
class WeightRuleAdmin(admin.ModelAdmin):
    list_display = ("zone", "min_weight", "max_weight", "rate_per_kg")
    autocomplete_fields = ("zone",)


@admin.register(models.DeliveryMan)
class DeliveryManAdmin(admin.ModelAdmin):
    list_display = ("user", "zone", "status", "deliveries_count", "rating", "total_earnings")
    list_filter = ("status",)
    autocomplete_fields = ("user", "zone")


# --- Reels --------------------------------------------------------------------

@admin.register(models.Reel)
class ReelAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "vendor",
        "platform",
        "status_badge",
        "views",
        "likes",
        "is_sponsored",
        "created_at",
    )
    list_filter = ("status", "platform", "is_sponsored")
    search_fields = ("caption", "video_url", "vendor__store_name")
    readonly_fields = ("views", "likes", "shares", "cart_adds", "created_at")
    inlines = [ReelInteractionInline]
    autocomplete_fields = ("vendor", "product")

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "draft": "#6c757d",
            "pending": "#fd7e14",
            "approved": "#0d6efd",
            "rejected": "#dc3545",
            "active": "#198754",
        }
        return _badge(obj.get_status_display(), colors.get(obj.status, "#6c757d"))

    @admin.action(description="Approve reels")
    def approve_reels(self, request, queryset):
        queryset.update(status=models.Reel.Status.APPROVED, rejection_reason="")
        self.message_user(request, "Reels approved.", messages.SUCCESS)

    @admin.action(description="Reject reels (set reason in form)")
    def reject_reels_placeholder(self, request, queryset):
        self.message_user(
            request,
            "Use change form to set rejection_reason, or reject individually.",
            messages.INFO,
        )

    actions = ["approve_reels", "reject_reels_placeholder"]


@admin.register(models.ReelInteraction)
class ReelInteractionAdmin(admin.ModelAdmin):
    list_display = ("reel", "user", "type", "created_at")
    list_filter = ("type",)
    autocomplete_fields = ("reel", "user")


@admin.register(models.ReelView)
class ReelViewAdmin(admin.ModelAdmin):
    list_display = ("reel", "user", "created_at")
    autocomplete_fields = ("reel", "user")


@admin.register(models.ReelComment)
class ReelCommentAdmin(admin.ModelAdmin):
    list_display = ("id", "reel", "user", "parent_id", "created_at")
    search_fields = ("body",)
    autocomplete_fields = ("reel", "user", "parent")

    @admin.display(description="Parent")
    def parent_id(self, obj):
        return obj.parent_id


# --- Staff / audit ------------------------------------------------------------

@admin.register(models.Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("name", "is_system", "status")
    list_filter = ("status", "is_system")
    search_fields = ("name",)
    formfield_overrides = {
        JSONField: {"widget": admin.widgets.AdminTextareaWidget(attrs={"rows": 6, "cols": 80})},
    }


@admin.register(models.EmployeeProfile)
class EmployeeProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "status")
    list_filter = ("status",)
    autocomplete_fields = ("user", "role")
    formfield_overrides = {
        JSONField: {"widget": admin.widgets.AdminTextareaWidget(attrs={"rows": 4, "cols": 80})},
    }


@admin.register(models.AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "type",
        "action_kind",
        "module",
        "action_short",
        "performed_by",
        "object_type",
        "object_id",
    )
    list_filter = ("type", "action_kind", "module")
    search_fields = ("action", "object_type", "object_id", "module")
    readonly_fields = (
        "action",
        "performed_by",
        "type",
        "action_kind",
        "module",
        "metadata",
        "object_type",
        "object_id",
        "ip_address",
        "created_at",
    )
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        # View-only: change page opens but all fields are read-only
        return True

    @admin.display(description="Action")
    def action_short(self, obj):
        return (obj.action[:120] + "…") if len(obj.action) > 120 else obj.action


# --- Support / security -------------------------------------------------------

@admin.register(models.SupportTicketMessage)
class SupportTicketMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "ticket", "sender", "created_at")
    search_fields = ("body", "ticket__ticket_number")
    autocomplete_fields = ("ticket", "sender")


@admin.register(models.SupportTicketMessageAttachment)
class SupportTicketMessageAttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "message", "original_name", "size", "content_type")
    search_fields = ("original_name", "message__ticket__ticket_number")
    autocomplete_fields = ("message",)


@admin.register(models.SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = (
        "ticket_number",
        "submitter",
        "subject",
        "source_panel",
        "category",
        "priority",
        "status",
        "created_at",
    )
    list_filter = ("status", "priority", "source_panel", "category")
    search_fields = ("ticket_number", "subject", "submitter__phone")
    readonly_fields = ("created_at", "resolved_at")
    autocomplete_fields = ("submitter", "assigned_to")

    @admin.action(description="Assign tickets to me")
    def assign_to_me(self, request, queryset):
        if request.user.is_authenticated:
            queryset.update(assigned_to=request.user)
            self.message_user(request, "Tickets assigned.", messages.SUCCESS)

    actions = ["assign_to_me"]


@admin.register(models.FlaggedActivity)
class FlaggedActivityAdmin(admin.ModelAdmin):
    list_display = ("activity_type", "user", "severity", "status", "created_at")
    list_filter = ("severity", "status")
    search_fields = ("activity_type", "detail")
    readonly_fields = ("created_at",)
    autocomplete_fields = ("user", "reviewed_by")


# --- Notifications ------------------------------------------------------------

@admin.register(models.Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "type", "target", "recipient", "is_read", "created_at")
    list_filter = ("type", "target", "is_read")
    search_fields = ("title", "message")
    autocomplete_fields = ("recipient",)


# --- Settings (singletons) -----------------------------------------------------

@admin.register(models.SecuritySettings)
class SecuritySettingsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "otp_sensitive_crud",
        "rbac_enforced",
        "duplicate_prevention",
        "auto_lock_failed_logins",
        "ip_rate_limiting",
        "updated_at",
    )

    def has_add_permission(self, request):
        return not models.SecuritySettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if models.SecuritySettings.objects.exists():
            return redirect(reverse("admin:core_securitysettings_change", args=(1,)))
        return super().changelist_view(request, extra_context)


@admin.register(models.SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ("site_name", "currency", "maintenance_mode", "updated_at")

    def has_add_permission(self, request):
        return not models.SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if models.SiteSettings.objects.exists():
            return redirect(reverse("admin:core_sitesettings_change", args=(1,)))
        return super().changelist_view(request, extra_context)


@admin.register(models.OrderSettings)
class OrderSettingsAdmin(admin.ModelAdmin):
    list_display = ("id", "refund_validity_days", "auto_cancel_hours", "updated_at")

    def has_add_permission(self, request):
        return not models.OrderSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if models.OrderSettings.objects.exists():
            return redirect(reverse("admin:core_ordersettings_change", args=(1,)))
        return super().changelist_view(request, extra_context)


@admin.register(models.WalletSettings)
class WalletSettingsAdmin(admin.ModelAdmin):
    form = WalletSettingsForm
    list_display = ("id", "max_balance_per_user", "daily_transfer_limit", "updated_at")

    def has_add_permission(self, request):
        return not models.WalletSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if models.WalletSettings.objects.exists():
            return redirect(reverse("admin:core_walletsettings_change", args=(1,)))
        return super().changelist_view(request, extra_context)


@admin.register(models.PaymentGatewaySettings)
class PaymentGatewaySettingsAdmin(admin.ModelAdmin):
    list_display = ("gateway", "is_enabled", "environment", "updated_at")
    list_filter = ("is_enabled", "environment")


# --- Misc auth-related --------------------------------------------------------

@admin.register(models.OTPVerification)
class OTPVerificationAdmin(admin.ModelAdmin):
    list_display = ("phone", "purpose", "is_used", "created_at", "expires_at")
    list_filter = ("purpose", "is_used")
    readonly_fields = ("created_at",)


@admin.register(models.KYCDocument)
class KYCDocumentAdmin(admin.ModelAdmin):
    list_display = ("user", "document_type", "document_id_number", "status", "submitted_at", "reviewer")
    list_filter = ("status", "document_type")
    search_fields = ("user__phone", "user__name")
    readonly_fields = ("submitted_at", "reviewed_at")
    autocomplete_fields = ("user", "reviewer")

    @admin.action(description="Approve selected KYC documents")
    def approve_kyc(self, request, queryset):
        now = timezone.now()
        queryset.update(
            status=models.KYCDocument.Status.APPROVED,
            reviewed_at=now,
            reviewer=request.user if request.user.is_authenticated else None,
            rejection_reason="",
        )
        models.User.objects.filter(
            pk__in=queryset.values_list("user_id", flat=True)
        ).update(kyc_status=models.User.KYCStatus.VERIFIED)
        self.message_user(request, "KYC documents approved.", messages.SUCCESS)

    actions = ["approve_kyc"]


@admin.register(models.PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ("txn_ref", "customer", "amount", "method", "status", "created_at")
    list_filter = ("status", "method")
    search_fields = ("txn_ref",)
    readonly_fields = ("created_at", "verified_at", "gateway_response")
    autocomplete_fields = ("order", "wallet_transaction", "customer")


@admin.register(models.NavigationItem)
class NavigationItemAdmin(admin.ModelAdmin):
    list_display = ("surface", "key", "view_key", "label", "parent_key", "sort_order", "icon")
    list_filter = ("surface",)
    search_fields = ("key", "label")
    ordering = ("surface", "parent_key", "sort_order", "key")


class AutoCRUDModelAdmin(admin.ModelAdmin):
    """Fallback admin for models not explicitly registered."""

    def get_list_display(self, request):
        field_names = []
        for field in self.model._meta.concrete_fields:
            # Avoid giant tables with very wide text/blob columns.
            if getattr(field, "many_to_many", False):
                continue
            field_names.append(field.name)
            if len(field_names) >= 6:
                break
        return tuple(field_names) or ("pk",)

    def has_module_permission(self, request):
        return request.user.is_staff

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff

    def has_add_permission(self, request):
        return request.user.is_staff

    def has_change_permission(self, request, obj=None):
        return request.user.is_staff

    def has_delete_permission(self, request, obj=None):
        return request.user.is_staff


def register_unregistered_models():
    """Register installed-app models that do not have explicit admin classes yet."""
    for model in apps.get_models():
        opts = model._meta
        if model in admin.site._registry:
            continue
        if opts.abstract or opts.proxy or opts.auto_created:
            continue
        if not opts.managed:
            continue
        admin.site.register(model, AutoCRUDModelAdmin)
