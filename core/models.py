"""
KhudraPasal domain models — aligned with models.md (all portals).
"""
from __future__ import annotations

import random
import string
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify


def _generate_kid() -> str:
    digits = "".join(random.choices(string.digits, k=6))
    return f"KP{digits}"


class User(AbstractUser):
    """Core user account shared across all portals."""

    class Role(models.TextChoices):
        NORMAL = "normal", "Normal"
        PARENT = "parent", "Parent"
        CHILD = "child", "Child"
        SUPER_ADMIN = "super_admin", "Super Admin"
        STAFF = "staff", "Staff"
        FINANCE = "finance", "Finance"
        MODERATOR = "moderator", "Moderator"
        VIEWER = "viewer", "Viewer"

    class KYCStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        REVIEW = "review", "Review"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    class SocialProvider(models.TextChoices):
        GOOGLE = "google", "Google"
        FACEBOOK = "facebook", "Facebook"
        PHONE = "phone", "Phone"

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["username", "name"]

    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=15, unique=True, db_index=True)
    email = models.EmailField(blank=True)
    username = models.CharField(
        max_length=50,
        unique=True,
        help_text="Handle for wallet transfers (e.g. @ramesh_sharma)",
    )
    KID = models.CharField(max_length=10, unique=True, editable=False, db_index=True)
    role = models.CharField(
        max_length=20, choices=Role.choices, default=Role.NORMAL, db_index=True
    )
    kyc_status = models.CharField(
        max_length=20,
        choices=KYCStatus.choices,
        default=KYCStatus.PENDING,
        db_index=True,
    )
    referred_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="referrals",
    )
    avatar = models.ImageField(upload_to="avatars/", blank=True)
    # HTTPS URL from Google userinfo `picture` (or other OAuth providers); used when no uploaded avatar.
    social_avatar_url = models.URLField(max_length=512, blank=True, default="")
    profile_cover = models.ImageField(upload_to="customers/covers/", blank=True)
    profile_description = models.TextField(blank=True)
    address = models.TextField(blank=True)
    default_area_location = models.CharField(max_length=255, blank=True)
    default_landmark = models.CharField(max_length=255, blank=True)
    default_google_map_link = models.URLField(blank=True, max_length=500)
    default_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    default_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    customer_document = models.FileField(
        upload_to="customers/documents/", blank=True
    )
    social_provider = models.CharField(
        max_length=20, choices=SocialProvider.choices, blank=True
    )
    social_provider_id = models.CharField(max_length=255, blank=True)
    oauth_phone_completed = models.BooleanField(
        default=True,
        db_index=True,
        help_text="False until OAuth sign-up completes phone verification (OTP).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    fcm_token = models.TextField(blank=True, default="")

    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self) -> str:
        return f"{self.name} ({self.phone})"

    def save(self, *args, **kwargs):
        if not self.KID:
            for _ in range(50):
                kid = _generate_kid()
                if not User.objects.filter(KID=kid).exclude(pk=self.pk).exists():
                    self.KID = kid
                    break
            else:
                raise ValidationError("Could not generate unique KID")
        super().save(*args, **kwargs)

    def get_full_name(self) -> str:
        return self.name


class UserFcmDevice(models.Model):
    """FCM registration token for one browser or app install (many per user)."""

    class Platform(models.TextChoices):
        WEB = "web", "Web"
        ANDROID = "android", "Android"
        IOS = "ios", "iOS"
        UNKNOWN = "", "Unknown"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="fcm_devices",
    )
    token = models.CharField(max_length=8192, unique=True, db_index=True)
    platform = models.CharField(
        max_length=16,
        choices=Platform.choices,
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "FCM device"
        verbose_name_plural = "FCM devices"
        indexes = [
            models.Index(fields=["user", "updated_at"]),
        ]

    def __str__(self) -> str:
        plat = self.platform or "device"
        return f"{plat} …{self.token[-12:]}" if len(self.token) > 12 else plat


class OTPVerification(models.Model):
    class Purpose(models.TextChoices):
        LOGIN = "login", "Login"
        SIGNUP = "signup", "Signup"
        OAUTH_PHONE = "oauth_phone", "OAuth phone"
        TRANSFER = "transfer", "Transfer"
        WITHDRAW = "withdraw", "Withdraw"
        FREEZE = "freeze", "Freeze"
        FAMILY_INVITE = "family_invite", "Family invite"
        ADMIN_SENSITIVE = "admin_sensitive", "Admin sensitive"

    phone = models.CharField(max_length=15)
    otp = models.CharField(max_length=6)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    signup_name = models.CharField(max_length=150, blank=True)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "OTP verification"
        verbose_name_plural = "OTP verifications"

    def __str__(self) -> str:
        return f"{self.phone} — {self.purpose}"


class KYCDocument(models.Model):
    class DocumentType(models.TextChoices):
        CITIZENSHIP = "citizenship", "Citizenship"
        PASSPORT = "passport", "Passport"
        DRIVING_LICENSE = "driving_license", "Driving license"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        REVIEW = "review", "Review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="kyc_documents"
    )
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    document_image = models.ImageField(upload_to="kyc/", blank=True)
    document_file = models.FileField(upload_to="kyc/files/", blank=True)
    document_back = models.ImageField(upload_to="kyc/", blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="kyc_reviews",
    )
    rejection_reason = models.TextField(blank=True)
    document_id_number = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        verbose_name = "KYC document"
        verbose_name_plural = "KYC documents"

    def __str__(self) -> str:
        return f"{self.user} — {self.document_type}"


class Category(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, unique=True)
    icon = models.CharField(max_length=50, blank=True)
    image = models.ImageField(upload_to="categories/", blank=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )
    level = models.PositiveSmallIntegerField(default=0)
    seo_title = models.CharField(max_length=70, blank=True)
    seo_description = models.TextField(max_length=160, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name


class Brand(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100)
    logo = models.ImageField(upload_to="brands/", blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )

    def __str__(self) -> str:
        return self.name


class Attribute(models.Model):
    class Type(models.TextChoices):
        COLOR = "color", "Color"
        DROPDOWN = "dropdown", "Dropdown"
        TEXT = "text", "Text"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=Type.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )

    def __str__(self) -> str:
        return self.name


class AttributeValue(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    attribute = models.ForeignKey(
        Attribute, on_delete=models.CASCADE, related_name="values"
    )
    value = models.CharField(max_length=100)
    sort_order = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )

    class Meta:
        ordering = ["sort_order", "value"]
        verbose_name_plural = "Attribute values"

    def __str__(self) -> str:
        return f"{self.attribute}: {self.value}"


class Unit(models.Model):
    class Type(models.TextChoices):
        WEIGHT = "weight", "Weight"
        QUANTITY = "quantity", "Quantity"
        VOLUME = "volume", "Volume"
        LENGTH = "length", "Length"
        TIME = "time", "Time"
        DIGITAL = "digital", "Digital"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=50)
    short_name = models.CharField(max_length=10)
    type = models.CharField(max_length=20, choices=Type.choices)
    conversion = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )

    def __str__(self) -> str:
        return f"{self.name} ({self.short_name})"


class FamilyGroup(models.Model):
    class Type(models.TextChoices):
        FAMILY = "family", "Family"
        FLAT = "flat", "Flat"
        HOTEL = "hotel", "Hotel"
        HOSTEL = "hostel", "Hostel"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FROZEN = "frozen", "Frozen"

    name = models.CharField(max_length=100)
    leader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="led_groups",
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    is_platform_hub = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Singleton group shared by all users of this type (flat/hotel/hostel).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


class FamilyMember(models.Model):
    class Role(models.TextChoices):
        PARENT = "parent", "Parent"
        CHILD = "child", "Child"
        SPOUSE = "spouse", "Spouse"
        GUEST = "guest", "Guest"
        MANAGER = "manager", "Manager"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FROZEN = "frozen", "Frozen"
        PENDING = "pending", "Pending"

    group = models.ForeignKey(
        FamilyGroup, on_delete=models.CASCADE, related_name="members"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="family_memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    spending_limit_daily = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    spending_limit_weekly = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    spending_limit_monthly = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    initial_balance = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["group", "user"]]

    def __str__(self) -> str:
        return f"{self.user} in {self.group}"


class FamilyInvite(models.Model):
    class InviteMethod(models.TextChoices):
        LINK = "link", "Link"
        PHONE = "phone", "Phone"

    class Role(models.TextChoices):
        CHILD = "child", "Child"
        SPOUSE = "spouse", "Spouse"
        GUEST = "guest", "Guest"
        MANAGER = "manager", "Manager"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"

    group = models.ForeignKey(
        FamilyGroup, on_delete=models.CASCADE, related_name="invites"
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE
    )
    invite_method = models.CharField(max_length=10, choices=InviteMethod.choices)
    phone = models.CharField(max_length=15, blank=True)
    token = models.CharField(max_length=64, unique=True)
    role = models.CharField(max_length=20, choices=Role.choices)
    spending_limit = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    initial_balance = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Invite to {self.group}"


def _default_wallet_category_roles():
    return ["child"]


class FamilyWalletCategory(models.Model):
    """User-defined wallet buckets within a family group (e.g. Groceries, School)."""

    group = models.ForeignKey(
        FamilyGroup, on_delete=models.CASCADE, related_name="wallet_categories"
    )
    name = models.CharField(max_length=100)
    image = models.ImageField(
        upload_to="family_wallet_categories/", blank=True
    )
    sort_order = models.PositiveIntegerField(default=0)
    allowed_member_roles = models.JSONField(
        default=_default_wallet_category_roles,
        help_text='FamilyMember.role values allowed for distribution/transfers, e.g. ["child"].',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "Family wallet categories"
        unique_together = [["group", "name"]]

    def __str__(self) -> str:
        return f"{self.group} — {self.name}"


class FamilyPortalJoinLink(models.Model):
    """Shareable URL token for external users to request joining a family group."""

    group = models.ForeignKey(
        FamilyGroup, on_delete=models.CASCADE, related_name="portal_join_links"
    )
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="family_portal_join_links_created",
    )
    default_role = models.CharField(
        max_length=20,
        choices=[
            ("child", "Child"),
            ("spouse", "Spouse"),
            ("guest", "Guest"),
            ("manager", "Manager"),
        ],
        default="child",
    )
    title = models.CharField(
        max_length=120,
        blank=True,
        help_text="Short heading shown on the public join page.",
    )
    welcome_message = models.TextField(
        blank=True,
        help_text="Optional message shown to applicants.",
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="If set, link stops accepting applications after this time.",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Join link {self.token[:8]}… → {self.group_id}"


class FamilyJoinRequest(models.Model):
    """Pending / processed request to join a family (may exist before invitee has an account)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class Role(models.TextChoices):
        CHILD = "child", "Child"
        SPOUSE = "spouse", "Spouse"
        GUEST = "guest", "Guest"
        MANAGER = "manager", "Manager"

    class Source(models.TextChoices):
        PARENT_INVITE = "parent_invite", "Parent invite"
        SHARE_LINK = "share_link", "Share link"

    group = models.ForeignKey(
        FamilyGroup, on_delete=models.CASCADE, related_name="join_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="family_join_requests_created",
    )
    name = models.CharField(max_length=150)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=15)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CHILD)
    age = models.PositiveSmallIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    invite = models.ForeignKey(
        FamilyInvite,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="join_requests",
    )
    join_link = models.ForeignKey(
        "FamilyPortalJoinLink",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="join_requests",
    )
    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.PARENT_INVITE,
    )
    applicant_note = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="family_join_requests_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} → {self.group}"


class FamilyGroupPermission(models.Model):
    group = models.OneToOneField(
        FamilyGroup, on_delete=models.CASCADE, related_name="permissions"
    )
    allow_online_purchases = models.BooleanField(default=True)
    allow_cash_withdrawal = models.BooleanField(default=True)
    allow_peer_transfers = models.BooleanField(default=False)
    category_restrictions = models.BooleanField(default=False)
    time_based_restrictions = models.BooleanField(default=False)
    daily_spending_limit = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    default_invite_spending_limit = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Default monthly spending limit for batch member invites (family portal).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Permissions: {self.group}"


class ProductRestriction(models.Model):
    group = models.ForeignKey(
        FamilyGroup,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="product_restrictions",
    )
    family_member = models.ForeignKey(
        FamilyMember,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="product_restrictions",
    )
    category = models.ForeignKey(Category, on_delete=models.CASCADE)
    is_blocked = models.BooleanField(default=False)
    requires_approval = models.BooleanField(default=False)
    max_price = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )

    def __str__(self) -> str:
        return f"{self.category} restriction"


class AutoApprovalRule(models.Model):
    group = models.ForeignKey(
        FamilyGroup, on_delete=models.CASCADE, related_name="auto_approval_rules"
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL
    )
    max_amount = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    is_enabled = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.name


class TimeBasedRule(models.Model):
    class RuleType(models.TextChoices):
        ALLOW_HOURS = "allow_hours", "Allow hours"
        BLOCK_HOURS = "block_hours", "Block hours"
        WEEKEND_BOOST = "weekend_boost", "Weekend boost"

    group = models.ForeignKey(
        FamilyGroup, on_delete=models.CASCADE, related_name="time_rules"
    )
    rule_type = models.CharField(max_length=20, choices=RuleType.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()
    extra_amount = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    is_enabled = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.group} — {self.rule_type}"


class Vendor(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUSPENDED = "suspended", "Suspended"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="vendor_profile",
    )
    store_name = models.CharField(max_length=150)
    store_slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    banner = models.ImageField(upload_to="vendors/banners/", blank=True)
    logo = models.ImageField(upload_to="vendors/logos/", blank=True)
    contact_email = models.EmailField(blank=True)
    phone = models.CharField(max_length=15, blank=True)
    address = models.TextField(blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    rejection_reason = models.CharField(max_length=500, blank=True)
    commission_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("10.00")
    )
    refund_commission_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("3.00"),
        help_text="Percentage of the proportional commission slice retained by the platform on refunds (0–100).",
    )
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.00"))
    is_verified = models.BooleanField(default=False)
    can_post = models.BooleanField(default=True)
    can_sell = models.BooleanField(default=True)
    pos_enabled = models.BooleanField(
        default=True,
        help_text="When false, this vendor cannot use the POS module (site-wide POS must also be on).",
    )
    portal_email_notifications = models.BooleanField(default=True)
    portal_sms_notifications = models.BooleanField(default=False)
    portal_language = models.CharField(max_length=10, default="en")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.store_name


class Wallet(models.Model):
    class Type(models.TextChoices):
        PERSONAL = "personal", "Personal"
        PARENT = "parent", "Parent"
        CHILD = "child", "Child"
        VENDOR = "vendor", "Vendor"
        SHARED = "shared", "Shared"
        PLATFORM = "platform", "Platform"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        FROZEN = "frozen", "Frozen"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="wallets",
    )
    vendor = models.OneToOneField(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="wallet",
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    label = models.CharField(max_length=100, blank=True)
    balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    currency = models.CharField(max_length=3, default="NPR")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    family_group = models.ForeignKey(
        FamilyGroup,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallets",
    )
    family_category = models.ForeignKey(
        "FamilyWalletCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Wallets"
        constraints = [
            models.UniqueConstraint(
                fields=["type"],
                condition=models.Q(type="platform"),
                name="wallet_unique_platform_type",
            ),
        ]

    def __str__(self) -> str:
        if self.vendor_id:
            return f"Vendor wallet: {self.vendor}"
        if self.type == self.Type.PLATFORM:
            return "Platform commission wallet"
        if self.owner_id:
            return f"{self.owner} — {self.type}"
        return f"Wallet #{self.pk}"

    def clean(self):
        if self.type == self.Type.VENDOR and not self.vendor_id:
            raise ValidationError("Vendor wallet must be linked to a vendor.")
        if self.type == self.Type.PLATFORM:
            if self.owner_id or self.vendor_id:
                raise ValidationError("Platform wallet must not have owner or vendor.")
            if self.family_group_id or self.family_category_id:
                raise ValidationError(
                    "Platform wallet must not be linked to family wallets."
                )
        if self.type != self.Type.VENDOR and self.vendor_id:
            raise ValidationError("Only vendor wallets may set vendor.")
        if self.owner_id and self.vendor_id:
            raise ValidationError("Wallet cannot have both owner and vendor set.")


class WalletTransaction(models.Model):
    class Type(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"
        TRANSFER = "transfer", "Transfer"
        TOPUP = "topup", "Top-up"
        PURCHASE = "purchase", "Purchase"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        BONUS = "bonus", "Bonus"
        COMMISSION_IN = "commission_in", "Commission in"
        VENDOR_SETTLEMENT = "vendor_settlement", "Vendor settlement"
        REFUND_VENDOR_DEBIT = "refund_vendor_dbt", "Refund vendor clawback"
        REFUND_PLATFORM_DEBIT = "refund_plat_dbt", "Refund platform clawback"
        REFUND_PLATFORM_FEE = "refund_plat_fee", "Refund platform fee retained"
        REFUND_CREDIT = "refund_credit", "Refund to customer"

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        PENDING = "pending", "Pending"
        FAILED = "failed", "Failed"
        BLOCKED = "blocked", "Blocked"
        FLAGGED = "flagged", "Flagged"

    txn_id = models.CharField(max_length=30, unique=True)
    wallet = models.ForeignKey(
        Wallet, on_delete=models.PROTECT, related_name="transactions"
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    from_wallet = models.ForeignKey(
        Wallet,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sent_transactions",
    )
    to_wallet = models.ForeignKey(
        Wallet,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="received_transactions",
    )
    reference_type = models.CharField(max_length=50, blank=True)
    reference_id = models.CharField(max_length=50, blank=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    family_wallet_category = models.ForeignKey(
        "FamilyWalletCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_transactions",
    )
    fund_source = models.CharField(
        max_length=200,
        blank=True,
        help_text="Human-readable origin of funds (e.g. Personal wallet, Child wallet). Shown in portal history.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.txn_id


class PayoutAccount(models.Model):
    """Saved payout destination (eSewa, Khalti, bank) for a user across portals."""

    class Type(models.TextChoices):
        ESEWA = "esewa", "eSewa"
        KHALTI = "khalti", "Khalti"
        BANK = "bank", "Bank"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="payout_accounts",
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    qr_image = models.ImageField(upload_to="payout_qr/", blank=True)
    phone = models.CharField(max_length=20, blank=True)
    bank_account_no = models.CharField(max_length=64, blank=True)
    bank_account_holder = models.CharField(max_length=150, blank=True)
    bank_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def clean(self):
        super().clean()
        if self.type == self.Type.BANK:
            if not (self.bank_account_no or "").strip():
                raise ValidationError({"bank_account_no": "Bank account number is required."})
        elif self.type in (self.Type.ESEWA, self.Type.KHALTI):
            if not (self.phone or "").strip():
                raise ValidationError({"phone": "Phone / wallet ID is required."})

    def __str__(self) -> str:
        return f"{self.user_id} — {self.get_type_display()}"


class WalletTransferCode(models.Model):
    """Globally unique transfer ID for cross-portal wallet receive (optional QR image)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wallet_transfer_code_row",
    )
    code = models.CharField(max_length=32, unique=True, db_index=True)
    qr_image = models.ImageField(upload_to="wallet_transfer_codes/", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Wallet transfer code"
        verbose_name_plural = "Wallet transfer codes"

    def __str__(self) -> str:
        return f"{self.code} → user {self.user_id}"


class WalletTransferIdempotency(models.Model):
    """Client idempotency key for POST wallet-hub transfer (one logical debit per key)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wallet_hub_idempotency_rows",
    )
    client_key = models.CharField(max_length=128)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    outbound_txn_id = models.CharField(max_length=30, blank=True)
    cached_response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["sender", "client_key"],
                name="wallet_hub_idem_sender_client_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.sender_id}:{self.client_key[:16]}… ({self.status})"


class WalletWithdrawal(models.Model):
    class Method(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        ESEWA = "esewa", "eSewa"
        KHALTI = "khalti", "Khalti"
        MOBILE_BANKING = "mobile_banking", "Mobile banking"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    withdrawal_number = models.CharField(max_length=30, unique=True)
    wallet = models.ForeignKey(
        Wallet, on_delete=models.PROTECT, related_name="withdrawals"
    )
    payout_account = models.ForeignKey(
        PayoutAccount,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="withdrawals",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=30, choices=Method.choices)
    method_account = models.CharField(max_length=100)
    bank_name = models.CharField(max_length=100, blank=True)
    account_holder = models.CharField(max_length=150, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    admin_note = models.TextField(blank=True)
    reject_reason = models.TextField(blank=True)
    proof_image = models.ImageField(upload_to="withdrawal_proofs/", blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.withdrawal_number


class WalletBonus(models.Model):
    class Type(models.TextChoices):
        SIGNUP = "signup", "Signup"
        TOPUP = "topup", "Top-up"
        REFERRAL = "referral", "Referral"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    title = models.CharField(max_length=150)
    type = models.CharField(max_length=20, choices=Type.choices)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    is_percentage = models.BooleanField(default=False)
    min_topup = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    used_count = models.PositiveIntegerField(default=0)
    expires_at = models.DateField(null=True, blank=True)

    def __str__(self) -> str:
        return self.title


class LoyaltyRule(models.Model):
    class Event(models.TextChoices):
        PURCHASE = "purchase", "Purchase"
        REVIEW = "review", "Review"
        BIRTHDAY = "birthday", "Birthday"
        REFERRAL = "referral", "Referral"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=150)
    rule_description = models.TextField()
    event = models.CharField(max_length=20, choices=Event.choices)
    multiplier = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices)

    def __str__(self) -> str:
        return self.name


class LoyaltySettings(models.Model):
    """Singleton: earn/redeem configuration for loyalty (admin-managed)."""

    points_per_currency_unit = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0.0100"),
        help_text="Points earned per 1.00 currency unit spent (e.g. 0.01 = 1 pt per Rs.100).",
    )
    redeem_points_per_currency = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("100.0000"),
        help_text="Points required to redeem 1.00 currency unit as wallet credit.",
    )
    min_redeem_points = models.PositiveIntegerField(default=100)
    max_redeem_per_order = models.PositiveIntegerField(default=50000)
    referral_bonus_points = models.PositiveIntegerField(default=50)
    loyalty_program_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Loyalty settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Loyalty settings (singleton)"


class SecuritySettings(models.Model):
    """Singleton: admin security policy toggles (UI mirrors these; no fake metrics)."""

    otp_sensitive_crud = models.BooleanField(default=True)
    rbac_enforced = models.BooleanField(default=True)
    duplicate_prevention = models.BooleanField(default=True)
    auto_lock_failed_logins = models.BooleanField(default=True)
    ip_rate_limiting = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Security settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Security settings (singleton)"


class ShippingSettings(models.Model):
    """Singleton: global shipping behaviour flags."""

    seller_pays_shipping = models.BooleanField(default=False)
    free_shipping_global = models.BooleanField(default=False)
    default_zone = models.ForeignKey(
        "ShippingZone",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    default_checkout_weight_kg = models.DecimalField(
        max_digits=8,
        decimal_places=3,
        default=Decimal("1.000"),
        help_text="Used for storefront quotes when cart has no per-product weights.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Shipping settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Shipping settings (singleton)"


class VendorDocument(models.Model):
    class Type(models.TextChoices):
        PAN = "pan", "PAN"
        BUSINESS_REGISTRATION = "business_registration", "Business registration"
        VAT = "vat", "VAT"
        BANK_DETAILS = "bank_details", "Bank details"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="documents"
    )
    type = models.CharField(max_length=30, choices=Type.choices)
    document = models.FileField(upload_to="vendor_docs/")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    def __str__(self) -> str:
        return f"{self.vendor} — {self.type}"


class VendorBankDetail(models.Model):
    vendor = models.OneToOneField(
        Vendor, on_delete=models.CASCADE, related_name="bank_detail"
    )
    bank_name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=50)
    account_holder = models.CharField(max_length=150)
    esewa_id = models.CharField(max_length=20, blank=True)
    khalti_id = models.CharField(max_length=20, blank=True)

    def __str__(self) -> str:
        return f"Bank: {self.vendor}"


class VendorImpersonationLog(models.Model):
    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="impersonation_logs",
    )
    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="impersonation_logs"
    )
    session_token = models.CharField(max_length=100, unique=True)
    notify_vendor = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.admin} → {self.vendor}"


class Product(models.Model):
    class Type(models.TextChoices):
        PHYSICAL = "physical", "Physical"
        DIGITAL = "digital", "Digital"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DRAFT = "draft", "Draft"
        OUT_OF_STOCK = "out_of_stock", "Out of stock"

    class DiscountType(models.TextChoices):
        FLAT = "flat", "Flat (amount off list price)"
        PERCENTAGE = "percentage", "Percentage off list price"

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=300, unique=True)
    description = models.TextField(blank=True)
    short_description = models.CharField(max_length=500, blank=True)
    sku = models.CharField(max_length=100, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_type = models.CharField(
        max_length=20,
        choices=DiscountType.choices,
        blank=True,
        default="",
    )
    discount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    tax_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("13.00")
    )
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    brand = models.ForeignKey(
        Brand, null=True, blank=True, on_delete=models.SET_NULL
    )
    unit = models.ForeignKey(
        Unit, null=True, blank=True, on_delete=models.SET_NULL
    )
    image = models.ImageField(upload_to="products/")
    type = models.CharField(max_length=20, choices=Type.choices)
    stock = models.PositiveIntegerField(default=0)
    seller = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    is_featured = models.BooleanField(default=False)
    is_bestseller = models.BooleanField(default=False)
    has_variations = models.BooleanField(default=False)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.00"))
    review_count = models.PositiveIntegerField(default=0)
    seo_title = models.CharField(max_length=70, blank=True)
    seo_description = models.TextField(max_length=160, blank=True)
    seo_keywords = models.CharField(max_length=255, blank=True)
    enable_reels = models.BooleanField(default=False)
    enable_pos = models.BooleanField(default=False)
    attributes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:295]
        super().save(*args, **kwargs)


class ProductImage(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(upload_to="products/gallery/")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"Image for {self.product}"


class ProductApproval(models.Model):
    class Type(models.TextChoices):
        NEW = "new", "New"
        UPDATE = "update", "Update"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"

    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE)
    type = models.CharField(max_length=20, choices=Type.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="product_reviews",
    )
    rejection_reason = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Product approvals"
        ordering = ["-submitted_at"]

    def __str__(self) -> str:
        return f"{self.product} — {self.status}"


class ProductReview(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REPORTED = "reported", "Reported"
        REJECTED = "rejected", "Rejected"

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="reviews"
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews",
    )
    rating = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    reply_text = models.TextField(blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    vendor_read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.product} — {self.rating}★"


class ProductWishlist(models.Model):
    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="wishlisted_by"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wishlist_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "user"],
                name="unique_product_wishlist_per_user",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.product_id}"


class Coupon(models.Model):
    class Type(models.TextChoices):
        PERCENTAGE = "percentage", "Percentage"
        FIXED = "fixed", "Fixed"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        INACTIVE = "inactive", "Inactive"

    code = models.CharField(max_length=30, unique=True)
    type = models.CharField(max_length=20, choices=Type.choices)
    value = models.DecimalField(max_digits=8, decimal_places=2)
    min_order = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    usage_limit = models.PositiveIntegerField(null=True, blank=True)
    used_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    vendor = models.ForeignKey(
        Vendor, null=True, blank=True, on_delete=models.SET_NULL
    )
    category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL
    )
    products = models.ManyToManyField(
        "Product",
        blank=True,
        related_name="coupon_targets",
        help_text="If non-empty, coupon applies only to these products (and vendor/category rules).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.code


class FlashDeal(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"

    name = models.CharField(max_length=150)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2)
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices)
    priority = models.PositiveIntegerField(default=0)
    vendor = models.ForeignKey(
        "Vendor",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="owned_flash_deals",
    )
    products = models.ManyToManyField(
        "Product",
        through="FlashDealProduct",
        related_name="flash_deals",
    )

    class Meta:
        ordering = ["-priority", "-start_at"]

    def __str__(self) -> str:
        return self.name


class FlashDealProduct(models.Model):
    flash_deal = models.ForeignKey(
        FlashDeal, on_delete=models.CASCADE, related_name="deal_products"
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    override_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    class Meta:
        unique_together = [["flash_deal", "product"]]

    def __str__(self) -> str:
        return f"{self.flash_deal} — {self.product}"


class Banner(models.Model):
    class Placement(models.TextChoices):
        HOMEPAGE = "homepage", "Homepage"
        CATEGORY = "category", "Category"
        SIDEBAR = "sidebar", "Sidebar"
        PROMO_STRIP = "promo_strip", "Home promo strip"
        SMALL_STRIP = "small_strip", "Small strip"
        FOOTER_PROMO = "footer_promo", "Footer promo"

    class CardVariant(models.TextChoices):
        TEAL_BUTTON = "teal_button", "Teal + pill CTA"
        WHITE_DISCOUNT = "white_discount", "White + discount badge + link"
        WHITE_LINK = "white_link", "White + underlined link"
        MAGENTA_BUTTON = "magenta_button", "Magenta + pill CTA"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        SCHEDULED = "scheduled", "Scheduled"
        EXPIRED = "expired", "Expired"

    title = models.CharField(max_length=150)
    subtitle = models.CharField(max_length=255, blank=True)
    placement = models.CharField(max_length=20, choices=Placement.choices)
    image = models.ImageField(upload_to="banners/", blank=True, null=True)
    click_url = models.URLField(blank=True)
    category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL
    )
    gradient = models.CharField(max_length=100, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    click_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    sort_order = models.SmallIntegerField(default=0)
    card_variant = models.CharField(max_length=32, blank=True, default="")
    cta_text = models.CharField(max_length=40, blank=True)
    badge_text = models.CharField(max_length=80, blank=True)

    def __str__(self) -> str:
        return self.title


class FAQ(models.Model):
    """Help articles; editable in admin, readable on vendor (and other) portals."""

    class Surface(models.TextChoices):
        VENDOR = "vendor", "Vendor"
        CUSTOMER = "customer", "Customer"
        GENERAL = "general", "General"

    question = models.CharField(max_length=300)
    answer = models.TextField()
    surface = models.CharField(
        max_length=20, choices=Surface.choices, default=Surface.VENDOR
    )
    sort_order = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["surface", "sort_order", "id"]
        verbose_name_plural = "FAQs"

    def __str__(self) -> str:
        return self.question[:80]


class CMSPage(models.Model):
    class Status(models.TextChoices):
        PUBLISHED = "published", "Published"
        DRAFT = "draft", "Draft"

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=300, unique=True)
    content = models.TextField()
    featured_image = models.ImageField(upload_to="cms/", blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    seo_title = models.CharField(max_length=70, blank=True)
    seo_description = models.TextField(max_length=160, blank=True)
    last_updated = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.title


class BlogPost(models.Model):
    class Status(models.TextChoices):
        PUBLISHED = "published", "Published"
        DRAFT = "draft", "Draft"

    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=300, unique=True)
    content = models.TextField()
    excerpt = models.CharField(max_length=500, blank=True)
    cover_image = models.ImageField(upload_to="blog/", blank=True)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    seo_title = models.CharField(max_length=70, blank=True)
    seo_description = models.TextField(max_length=160, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class Order(models.Model):
    class PlacedPortal(models.TextChoices):
        PORTAL_MAIN = "portal_main", "Customer portal"
        PORTAL_FAMILY = "portal_family", "Family portal"
        PORTAL_CHILD = "portal_child", "Child portal"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SHIPPED = "shipped", "Shipped"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    class PaymentMethod(models.TextChoices):
        COD = "cod", "COD"
        ESEWA = "esewa", "eSewa"
        KHALTI = "khalti", "Khalti"
        IME_PAY = "ime_pay", "IME Pay"
        WALLET = "wallet", "Wallet"
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        NCHL_QR = "nchl_qr", "NCHL QR"

    class PaymentStatus(models.TextChoices):
        PAID = "paid", "Paid"
        PENDING = "pending", "Pending"
        REFUNDED = "refunded", "Refunded"
        FAILED = "failed", "Failed"

    order_number = models.CharField(max_length=20, unique=True)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="orders",
    )
    seller = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
    )
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    delivery_fee = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    discount_amount = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    app_promo_discount_amount = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="First-order discount from app promotion banner attribution.",
    )
    total = models.DecimalField(max_digits=10, decimal_places=2)
    want_delivery = models.BooleanField(default=True)
    coupon = models.ForeignKey(
        Coupon, null=True, blank=True, on_delete=models.SET_NULL
    )
    notes = models.TextField(blank=True)
    is_pos_order = models.BooleanField(default=False)
    tracking_number = models.CharField(max_length=100, blank=True)
    carrier = models.CharField(max_length=100, blank=True)
    placed_portal = models.CharField(
        max_length=20,
        choices=PlacedPortal.choices,
        null=True,
        blank=True,
        db_index=True,
        help_text="Portal surface used at checkout; null = legacy (listed on main portal only).",
    )
    payment_wallet = models.ForeignKey(
        "Wallet",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders_paid_from",
        help_text="Wallet debited for wallet checkout; used for refund credit.",
    )
    bill_image = models.ImageField(
        upload_to="orders/bills/",
        blank=True,
        help_text="Auto-generated order bill (PNG) for portal customers.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.order_number


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    list_unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Product list price at checkout (before product-level sale).",
    )
    flash_deal = models.ForeignKey(
        "FlashDeal",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="order_items",
    )
    unit_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Unit price after flash (and product sale); before coupon allocation.",
    )
    coupon_discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="This line's share of order-level coupon discount.",
    )
    total_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text="Line total after coupon: unit_price × quantity − coupon_discount_amount.",
    )

    def __str__(self) -> str:
        return f"{self.order} — {self.product}"


class DeliveryAddress(models.Model):
    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name="delivery_address"
    )
    shipping_zone = models.ForeignKey(
        "ShippingZone",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="delivery_addresses",
    )
    full_name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=15)
    secondary_contact = models.CharField(max_length=15, blank=True)
    area_location = models.CharField(max_length=255)
    landmark = models.CharField(max_length=255, blank=True)
    google_map_link = models.URLField(blank=True)
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    delivery_notes = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Delivery for {self.order}"


class Refund(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    refund_number = models.CharField(max_length=20, unique=True)
    order = models.ForeignKey(
        Order, on_delete=models.PROTECT, related_name="refunds"
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    platform_fee_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    net_credit_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    reason = models.TextField()
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    admin_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.refund_number


class OrderCommissionSettlement(models.Model):
    """One row per paid order; idempotent commission split (platform + vendor wallets)."""

    order = models.OneToOneField(
        Order,
        on_delete=models.CASCADE,
        related_name="commission_settlement",
    )
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.PROTECT,
        related_name="commission_settlements",
    )
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2)
    vendor_amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_status = models.CharField(max_length=20)
    platform_wallet_txn = models.ForeignKey(
        WalletTransaction,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="commission_settlement_platform",
    )
    vendor_wallet_txn = models.ForeignKey(
        WalletTransaction,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="commission_settlement_vendor",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Settlement {self.order_id}"


class PurchaseOrder(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        WALLET = "wallet", "Wallet"
        ESEWA = "esewa", "eSewa"
        KHALTI = "khalti", "Khalti"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        COMPLETED = "completed", "Completed"

    po_number = models.CharField(max_length=20, unique=True)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    seller = models.ForeignKey(
        Vendor, null=True, blank=True, on_delete=models.SET_NULL
    )
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    tax = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    discount = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    total = models.DecimalField(max_digits=10, decimal_places=2)
    delivery_fee = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.po_number


class PurchaseOrderLine(models.Model):
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.purchase_order.po_number} — {self.product_id}"


class Supplier(models.Model):
    """Wholesaler / supplier contact, scoped to a vendor store."""

    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="suppliers", db_index=True
    )
    name = models.CharField(max_length=200)
    supplier_code = models.CharField(max_length=50, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "supplier_code"],
                name="supplier_unique_code_per_vendor",
                condition=models.Q(supplier_code__gt=""),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.vendor_id})"


class VendorStockPurchase(models.Model):
    """Procurement from a supplier (increases product stock when posted). Not the legacy POS PurchaseOrder."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"

    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="stock_purchases", db_index=True
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="stock_purchases"
    )
    reference = models.CharField(max_length=40, unique=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.reference


class VendorStockPurchaseLine(models.Model):
    purchase = models.ForeignKey(
        VendorStockPurchase, on_delete=models.CASCADE, related_name="lines"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.purchase.reference} — {self.product_id}"


class VendorLedgerEntry(models.Model):
    """Per-vendor accounting-style trail (complements WalletTransaction)."""

    class EntryType(models.TextChoices):
        SALE_SETTLEMENT = "sale_settlement", "Sale settlement"
        SALE_REVERSAL = "sale_reversal", "Sale reversal"
        PURCHASE_COST = "purchase_cost", "Stock purchase"
        ADJUSTMENT = "adjustment", "Adjustment"

    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="ledger_entries", db_index=True
    )
    entry_type = models.CharField(max_length=30, choices=EntryType.choices)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Signed: positive credits vendor book, negative is cost/outflow.",
    )
    reference_type = models.CharField(max_length=50, blank=True)
    reference_id = models.CharField(max_length=50, blank=True)
    wallet_transaction = models.ForeignKey(
        WalletTransaction,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="vendor_ledger_entries",
    )
    description = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["vendor", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "entry_type", "reference_type", "reference_id"],
                name="uniq_vendor_ledger_sale_settlement_order",
                condition=models.Q(entry_type="sale_settlement")
                & models.Q(reference_type="Order")
                & ~models.Q(reference_id=""),
            ),
            models.UniqueConstraint(
                fields=["vendor", "entry_type", "reference_type", "reference_id"],
                name="uniq_vendor_ledger_sale_reversal_order",
                condition=models.Q(entry_type="sale_reversal")
                & models.Q(reference_type="Order")
                & ~models.Q(reference_id=""),
            ),
            models.UniqueConstraint(
                fields=["vendor", "entry_type", "reference_type", "reference_id"],
                name="uniq_vendor_ledger_purchase_doc",
                condition=models.Q(entry_type="purchase_cost")
                & models.Q(reference_type="VendorStockPurchase")
                & ~models.Q(reference_id=""),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vendor_id} {self.entry_type} {self.amount}"


class PaymentTransaction(models.Model):
    class Method(models.TextChoices):
        ESEWA = "esewa", "eSewa"
        KHALTI = "khalti", "Khalti"
        CONNECTIPS = "connectips", "ConnectIPS"
        BANK_QR = "bank_qr", "Bank QR"
        MOBILE_BANKING = "mobile_banking", "Mobile banking"
        CARD = "card", "Card"
        COD = "cod", "COD"
        CASH = "cash", "Cash"
        IME_PAY = "ime_pay", "IME Pay"

    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        PENDING = "pending", "Pending"
        FAILED = "failed", "Failed"

    txn_ref = models.CharField(max_length=100, unique=True)
    order = models.ForeignKey(
        Order, null=True, blank=True, on_delete=models.SET_NULL
    )
    wallet_transaction = models.ForeignKey(
        WalletTransaction, null=True, blank=True, on_delete=models.SET_NULL
    )
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=30, choices=Method.choices)
    method_account = models.CharField(max_length=100, blank=True)
    screenshot = models.ImageField(
        upload_to="payment_screenshots/", blank=True
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    gateway_response = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.txn_ref


class PurchaseApprovalRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    child = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="purchase_requests",
    )
    parent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="purchase_approvals",
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    parent_note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when the child completes checkout using this approval (single-use).",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.child} → {self.product}"


class ShippingMethod(models.Model):
    class Type(models.TextChoices):
        FREE = "free", "Free"
        FLAT = "flat", "Flat"
        PICKUP = "pickup", "Pickup"
        WEIGHT = "weight", "Weight"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100)
    type = models.CharField(max_length=20, choices=Type.choices)
    free_threshold = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    status = models.CharField(max_length=20, choices=Status.choices)

    def __str__(self) -> str:
        return self.name


class ShippingZone(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100)
    areas = models.TextField()
    flat_rate = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("0.00")
    )
    free_above = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(max_length=20, choices=Status.choices)

    def __str__(self) -> str:
        return self.name


class WeightRule(models.Model):
    zone = models.ForeignKey(
        ShippingZone, on_delete=models.CASCADE, related_name="weight_rules"
    )
    min_weight = models.DecimalField(max_digits=6, decimal_places=3)
    max_weight = models.DecimalField(max_digits=6, decimal_places=3)
    rate_per_kg = models.DecimalField(max_digits=8, decimal_places=2)

    def __str__(self) -> str:
        return f"{self.zone} {self.min_weight}-{self.max_weight} kg"


class DeliveryMan(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="delivery_profile",
    )
    zone = models.ForeignKey(
        ShippingZone, null=True, blank=True, on_delete=models.SET_NULL
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    deliveries_count = models.PositiveIntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.00"))
    emergency_contact = models.CharField(max_length=15, blank=True)
    license_number = models.CharField(max_length=50, blank=True)
    total_earnings = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    pending_earnings = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    id_document_front = models.ImageField(upload_to="delivery/kyc/", blank=True)
    id_document_back = models.ImageField(upload_to="delivery/kyc/", blank=True)
    selfie = models.ImageField(upload_to="delivery/kyc/", blank=True)

    def __str__(self) -> str:
        return str(self.user)


class Reel(models.Model):
    class Platform(models.TextChoices):
        YOUTUBE_SHORTS = "youtube_shorts", "YouTube Shorts"
        TIKTOK = "tiktok", "TikTok"
        INSTAGRAM = "instagram", "Instagram"
        DIRECT_MP4 = "direct_mp4", "Direct MP4"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        ACTIVE = "active", "Active"

    class BoostTier(models.TextChoices):
        STANDARD = "standard", "Standard"
        PREMIUM = "premium", "Premium"
        MEGA = "mega", "Mega"

    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="reels"
    )
    video_url = models.URLField()
    platform = models.CharField(max_length=20, choices=Platform.choices)
    product = models.ForeignKey(
        Product,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reels",
    )
    caption = models.CharField(max_length=200, blank=True)
    tags = models.JSONField(default=list)
    thumbnail = models.ImageField(upload_to="reels/thumbnails/", blank=True)
    is_auto_thumbnail = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    is_sponsored = models.BooleanField(default=False)
    boost_expires_at = models.DateTimeField(null=True, blank=True)
    boost_expected_views = models.PositiveIntegerField(null=True, blank=True)
    boost_tier = models.CharField(
        max_length=20, choices=BoostTier.choices, blank=True, default=""
    )
    boost_daily_budget_npr = models.PositiveIntegerField(null=True, blank=True)
    views = models.PositiveIntegerField(default=0)
    likes = models.PositiveIntegerField(default=0)
    shares = models.PositiveIntegerField(default=0)
    bookmarks = models.PositiveIntegerField(default=0)
    cart_adds = models.PositiveIntegerField(default=0)
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Reel {self.pk} — {self.vendor}"


class ReelInteraction(models.Model):
    class Type(models.TextChoices):
        LIKE = "like", "Like"
        BOOKMARK = "bookmark", "Bookmark"
        SHARE = "share", "Share"
        CART_ADD = "cart_add", "Cart add"

    reel = models.ForeignKey(
        Reel, on_delete=models.CASCADE, related_name="interactions"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    type = models.CharField(max_length=20, choices=Type.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["reel", "user", "type"]]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} — {self.type}"


class ReelView(models.Model):
    reel = models.ForeignKey(
        Reel,
        on_delete=models.CASCADE,
        related_name="unique_views",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reel_views",
    )
    watch_seconds = models.PositiveSmallIntegerField(null=True, blank=True)
    quick_skip = models.BooleanField(default=False)
    watch_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["reel", "user"]]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"View {self.user_id} -> reel {self.reel_id}"


class ReelComment(models.Model):
    reel = models.ForeignKey(Reel, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reel_comments",
    )
    body = models.TextField(max_length=500)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="replies",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Comment {self.pk} on reel {self.reel_id}"


class Cart(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cart",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"Cart {self.pk} — {self.user_id}"


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="cart_items")
    quantity = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["cart", "product"]]
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"CartItem {self.pk} ({self.product_id} x {self.quantity})"


class Role(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100)
    permissions = models.JSONField(default=dict)
    is_system = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=Status.choices)

    def __str__(self) -> str:
        return self.name


class EmployeeProfile(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="employee_profile",
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT)
    modules_access = models.JSONField(default=list)
    status = models.CharField(max_length=20, choices=Status.choices)

    def __str__(self) -> str:
        return f"Employee: {self.user}"


class AuditLog(models.Model):
    class Type(models.TextChoices):
        VENDOR = "vendor", "Vendor"
        FAMILY = "family", "Family"
        KYC = "kyc", "KYC"
        WALLET = "wallet", "Wallet"
        PRODUCT = "product", "Product"
        MARKETING = "marketing", "Marketing"
        ORDER = "order", "Order"
        USER = "user", "User"
        SECURITY = "security", "Security"
        SETTINGS = "settings", "Settings"

    class ActionKind(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        READ = "read", "Read"
        OTHER = "other", "Other"

    action = models.CharField(max_length=500)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
    )
    type = models.CharField(max_length=20, choices=Type.choices)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=50, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    action_kind = models.CharField(
        max_length=20,
        choices=ActionKind.choices,
        default=ActionKind.OTHER,
        db_index=True,
    )
    module = models.CharField(max_length=64, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.action[:80]


class SupportTicket(models.Model):
    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    class SourcePanel(models.TextChoices):
        VENDOR = "vendor", "Vendor"
        CUSTOMER = "customer", "Customer"
        FAMILY = "family", "Family"
        CHILD = "child", "Child"

    class Category(models.TextChoices):
        BILLING = "billing", "Billing"
        ACCOUNT = "account", "Account"
        ORDERS = "orders", "Orders"
        WALLET = "wallet", "Wallet"
        TECHNICAL = "technical", "Technical"
        OTHER = "other", "Other"

    ticket_number = models.CharField(max_length=20, unique=True)
    submitter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_tickets",
    )
    subject = models.CharField(max_length=255)
    description = models.TextField()
    source_panel = models.CharField(
        max_length=20,
        choices=SourcePanel.choices,
        default=SourcePanel.CUSTOMER,
        db_index=True,
    )
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.OTHER,
    )
    priority = models.CharField(
        max_length=20, choices=Priority.choices, default=Priority.MEDIUM
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tickets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_activity_at", "-created_at"]

    def __str__(self) -> str:
        return self.ticket_number


class SupportTicketMessage(models.Model):
    ticket = models.ForeignKey(
        SupportTicket,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="support_ticket_messages",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["ticket", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.ticket_id}:{self.pk}"


class SupportTicketReaderState(models.Model):
    """Per-user read cursor for a ticket thread (unread bold + read receipts context)."""

    ticket = models.ForeignKey(
        SupportTicket,
        on_delete=models.CASCADE,
        related_name="reader_states",
    )
    reader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_ticket_reader_states",
    )
    last_read_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["ticket", "reader"],
                name="uniq_support_ticket_reader_state_ticket_reader",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.ticket_id}:{self.reader_id}"


class SupportTicketMessageAttachment(models.Model):
    """File attached to a support ticket message (images, video, documents)."""

    message = models.ForeignKey(
        SupportTicketMessage,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="support_tickets/%Y/%m/")
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    content_type = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["message", "id"]),
        ]

    def __str__(self) -> str:
        return f"{self.message_id}:{self.pk}"


class FlaggedActivity(models.Model):
    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        REVIEWED = "reviewed", "Reviewed"
        RESOLVED = "resolved", "Resolved"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    activity_type = models.CharField(max_length=150)
    detail = models.TextField(blank=True)
    severity = models.CharField(max_length=20, choices=Severity.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_flags",
    )

    class Meta:
        verbose_name_plural = "Flagged activities"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.activity_type


class Notification(models.Model):
    class Type(models.TextChoices):
        ORDER = "order", "Order"
        WALLET = "wallet", "Wallet"
        KYC = "kyc", "KYC"
        FAMILY = "family", "Family"
        STOCK = "stock", "Stock"
        MARKETING = "marketing", "Marketing"
        SECURITY = "security", "Security"
        SYSTEM = "system", "System"
        SUPPORT = "support", "Support"

    class Target(models.TextChoices):
        ALL = "all", "All"
        VENDORS = "vendors", "Vendors"
        CUSTOMERS = "customers", "Customers"
        ADMINS = "admins", "Admins"

    title = models.CharField(max_length=150)
    message = models.TextField()
    type = models.CharField(max_length=20, choices=Type.choices)
    target = models.CharField(max_length=20, choices=Target.choices)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    is_read = models.BooleanField(default=False)
    action_url = models.CharField(max_length=255, blank=True)
    image_url = models.URLField(
        max_length=500,
        blank=True,
        help_text="Optional image for rich notifications (e.g. order bill on delivery).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


class WalletSettings(models.Model):
    class FeeType(models.TextChoices):
        FLAT = "flat", "Flat"
        PERCENTAGE = "percentage", "Percentage"

    max_balance_per_user = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("500000.00")
    )
    daily_transfer_limit = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("50000.00")
    )
    min_withdrawal = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("100.00")
    )
    max_withdrawal_per_day = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("100000.00")
    )
    transaction_fee_type = models.CharField(
        max_length=20, choices=FeeType.choices, default=FeeType.PERCENTAGE
    )
    transaction_fee_value = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("1.50")
    )
    vendor_settlement_days = models.PositiveIntegerField(default=7)
    otp_for_withdrawals = models.BooleanField(default=True)
    otp_for_transfers_above = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("5000.00")
    )
    auto_flag_suspicious = models.BooleanField(default=True)
    shared_wallet_enabled = models.BooleanField(default=True)
    individual_wallet_enabled = models.BooleanField(default=True)
    flat_wallet_enabled = models.BooleanField(default=True)
    vendor_wallet_enabled = models.BooleanField(default=True)
    family_wallet_enabled = models.BooleanField(default=True)
    child_wallet_enabled = models.BooleanField(default=True)
    cross_portal_transfer_by_code_enabled = models.BooleanField(
        default=False,
        help_text="Allow wallet-hub transfers by transfer code across portals (parent/child/personal rules apply).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Wallet settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Wallet settings (singleton)"


class AppPromotionAttribution(models.Model):
    """Tracks app-download banner clicks, install claims, and first-order discount redemption."""

    class Status(models.TextChoices):
        CLICKED = "clicked", "Banner clicked"
        INSTALLED = "installed", "App install claimed"
        REDEEMED = "redeemed", "First-order discount used"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="app_promotion_attribution",
    )
    visit_token = models.CharField(max_length=64, unique=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.CLICKED,
        db_index=True,
    )
    clicked_at = models.DateTimeField(auto_now_add=True)
    installed_at = models.DateTimeField(null=True, blank=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Percent off merchandise on first order after install claim.",
    )
    first_order = models.ForeignKey(
        "Order",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    banner_headline = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)

    class Meta:
        ordering = ["-clicked_at"]

    def __str__(self) -> str:
        label = self.user.name if self.user_id else self.visit_token[:8]
        return f"App promo — {label} ({self.status})"


class SiteSettings(models.Model):
    site_name = models.CharField(max_length=150, default="Khudra Pasal")
    site_logo = models.ImageField(upload_to="site/", blank=True)
    site_favicon = models.ImageField(upload_to="site/", blank=True)
    cover_image = models.ImageField(
        upload_to="site/",
        blank=True,
        help_text="Default Open Graph image when a page has no featured image.",
    )
    site_email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    currency = models.CharField(max_length=10, default="NPR")
    timezone = models.CharField(max_length=50, default="Asia/Kathmandu")
    site_description = models.TextField(blank=True)
    meta_keywords = models.CharField(max_length=500, blank=True)
    footer_text = models.CharField(max_length=255, blank=True)
    maintenance_mode = models.BooleanField(default=False)
    temporary_shop_close = models.BooleanField(default=False)
    new_registrations = models.BooleanField(default=True)
    kyc_required = models.BooleanField(default=True)
    pos_enabled = models.BooleanField(default=True)
    smtp_host = models.CharField(
        max_length=255,
        blank=True,
        help_text="SMTP server hostname. Gmail: smtp.gmail.com. Alternatives: SendGrid, SES, Resend, Mailgun SMTP hosts.",
    )
    smtp_port = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="587 with TLS (default) or 465 with SSL. Overridable via KP_SMTP_PORT.",
    )
    smtp_username = models.CharField(
        max_length=255,
        blank=True,
        help_text="SMTP login user. Gmail: full Google account email. Overridable via KP_SMTP_USERNAME.",
    )
    smtp_password = models.CharField(
        max_length=255,
        blank=True,
        help_text="Gmail/Google: use an App Password (2-Step Verification required), not your normal password. Prefer KP_SMTP_PASSWORD in server environment for production.",
    )
    smtp_from_name = models.CharField(
        max_length=150,
        blank=True,
        help_text="Display name for the From header. Overridable via KP_SMTP_FROM_NAME.",
    )
    smtp_from_email = models.EmailField(
        blank=True,
        help_text="Must be the authenticated mailbox or a verified Send mail as alias for that account. Overridable via KP_SMTP_FROM_EMAIL.",
    )
    search_placeholders = models.JSONField(
        default=list,
        blank=True,
        help_text='List of strings for the storefront search bar, e.g. ["dal","rice"].',
    )
    admin_extras = models.JSONField(
        default=dict,
        blank=True,
        help_text="Nested settings for admin tabs (analytics, appearance, email, etc.).",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Site settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Site settings (singleton)"


class OrderSettings(models.Model):
    """Singleton row (pk=1) for admin order processing rules."""

    refund_validity_days = models.PositiveIntegerField(default=7)
    auto_cancel_hours = models.PositiveIntegerField(default=48)
    guest_checkout = models.BooleanField(default=True)
    order_verification_required = models.BooleanField(default=True)
    auto_assign_delivery = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Order settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self) -> str:
        return "Order settings (singleton)"


class PaymentGatewaySettings(models.Model):
    class Gateway(models.TextChoices):
        ESEWA = "esewa", "eSewa"
        KHALTI = "khalti", "Khalti"
        CONNECTIPS = "connectips", "ConnectIPS"
        FONEPAY = "fonepay", "Fonepay"
        COD = "cod", "COD"
        NCHL_QR = "nchl_qr", "NCHL QR"

    class Environment(models.TextChoices):
        TEST = "test", "Test"
        SANDBOX = "sandbox", "Sandbox"
        LIVE = "live", "Live"

    gateway = models.CharField(max_length=20, choices=Gateway.choices, unique=True)
    is_enabled = models.BooleanField(default=False)
    secret_key_live = models.CharField(max_length=255, blank=True)
    secret_key_test = models.CharField(max_length=255, blank=True)
    merchant_id = models.CharField(max_length=100, blank=True)
    merchant_name = models.CharField(max_length=150, blank=True)
    api_key_live = models.CharField(max_length=255, blank=True)
    api_key_test = models.CharField(max_length=255, blank=True)
    callback_url = models.URLField(blank=True)
    environment = models.CharField(
        max_length=20, choices=Environment.choices, default=Environment.TEST
    )
    gateway_extras = models.JSONField(
        default=dict,
        blank=True,
        help_text="Gateway-specific options (eSewa: form_url, status_url_base; Khalti: api_base_url).",
    )
    certificate = models.FileField(upload_to="gateway_certs/", blank=True)
    qr_expiry_seconds = models.PositiveIntegerField(default=300)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Payment gateway settings"

    def __str__(self) -> str:
        return self.get_gateway_display()


class PosPaymentSession(models.Model):
    """Pending POS gateway payment (eSewa redirect or NCHL dynamic QR) before order creation."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    class Method(models.TextChoices):
        ESEWA = "esewa", "eSewa"
        NCHL_QR = "nchl_qr", "NCHL QR"

    session_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="pos_payment_sessions",
    )
    vendor = models.ForeignKey(
        Vendor,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="pos_payment_sessions",
    )
    payment_method = models.CharField(max_length=20, choices=Method.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    txn_ref = models.CharField(max_length=100, unique=True)
    cart_payload = models.JSONField(default=dict, blank=True)
    qr_payload = models.TextField(
        blank=True,
        help_text="Base64 data URL or image URL for dynamic QR display.",
    )
    qr_string = models.TextField(
        blank=True,
        help_text="EMVCo / payload string for client-side QR rendering.",
    )
    payment_transaction = models.ForeignKey(
        "PaymentTransaction",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pos_sessions",
    )
    order = models.ForeignKey(
        Order,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pos_payment_sessions",
    )
    gateway_response = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.txn_ref} ({self.get_payment_method_display()})"


class NavigationItem(models.Model):
    """Sidebar / menu structure per app surface; editable in DB (seed via management command)."""

    class Surface(models.TextChoices):
        ADMIN = "admin", "Admin"
        VENDOR = "vendor", "Vendor"
        PORTAL_MAIN = "portal_main", "Customer portal"
        PORTAL_FAMILY = "portal_family", "Family portal"
        PORTAL_CHILD = "portal_child", "Child portal"

    surface = models.CharField(max_length=20, choices=Surface.choices, db_index=True)
    key = models.SlugField(max_length=80)
    label = models.CharField(max_length=120)
    icon = models.CharField(max_length=80, help_text="Lucide icon export name, e.g. Home, Wallet")
    view_key = models.SlugField(
        max_length=80,
        blank=True,
        default="",
        help_text="Frontend screen id (empty = same as key). URL segment stays `key`.",
    )
    parent_key = models.CharField(max_length=80, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)
    badge_key = models.CharField(
        max_length=80,
        blank=True,
        help_text="Resolved server-side to a count when applicable (e.g. pending_orders).",
    )
    roles_filter = models.CharField(
        max_length=200,
        blank=True,
        help_text="Comma-separated User.role values (portal surfaces only); empty = visible to all.",
    )

    class Meta:
        ordering = ["surface", "parent_key", "sort_order", "key"]
        constraints = [
            models.UniqueConstraint(fields=["surface", "key"], name="uniq_navigation_surface_key"),
        ]
        verbose_name_plural = "Navigation items"

    def __str__(self) -> str:
        return f"{self.surface} / {self.key}"
