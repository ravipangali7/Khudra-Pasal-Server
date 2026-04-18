"""Customer / family / child portal API (JWT, Token, or Session auth)."""

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode
from uuid import uuid4

from core.phone_auth import authenticate_user_by_phone, normalize_nepal_phone
from django.conf import settings
from django.core.exceptions import ValidationError
from django.contrib.sessions.models import Session
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
)
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response

PORTAL_API_AUTHENTICATION = [
    JWTAuthentication,
    TokenAuthentication,
    SessionAuthentication,
]

from core.portal_roles import (
    PORTAL_CHILD,
    PORTAL_FAMILY,
    PORTAL_MAIN,
    assert_portal_login_allowed,
    user_has_family_portal_access,
)
from core.models import (
    AutoApprovalRule,
    DeliveryAddress,
    FAQ,
    FamilyGroup,
    FamilyGroupPermission,
    FamilyInvite,
    FamilyJoinRequest,
    FamilyMember,
    FamilyWalletCategory,
    Notification,
    OTPVerification,
    Order,
    OrderItem,
    OrderSettings,
    PaymentTransaction,
    Product,
    PurchaseApprovalRequest,
    ProductReview,
    Reel,
    ReelInteraction,
    Refund,
    ShippingSettings,
    ShippingZone,
    SupportTicket,
    SupportTicketMessage,
    SupportTicketReaderState,
    PayoutAccount,
    User,
    Vendor,
    Wallet,
    WalletTransaction,
    WalletWithdrawal,
)
from core.serializers import (
    FamilyJoinRequestReadSerializer,
    FamilyWalletCategorySerializer,
    PortalAutoApprovalRuleCreateSerializer,
    PortalAutoApprovalRulePatchSerializer,
    PortalAutoApprovalRuleReadSerializer,
    PortalFamilyAddMemberSerializer,
    PortalFamilyAddMembersBatchSerializer,
    PortalFamilyJoinRequestPatchSerializer,
    PortalFamilyJoinShareLinkCreateSerializer,
    PortalFamilyMemberPatchSerializer,
    PortalProductRestrictionReadSerializer,
    PortalProductRestrictionsReplaceSerializer,
    PortalProductRestrictionUpsertSerializer,
    ReelPublicSerializer,
)
from core.services.portal_checkout_pricing import (
    apply_coupon_split,
    build_orders_plan,
    checkout_quote_line_rows,
    compute_delivery_allocation,
    parse_checkout_items,
    resolve_checkout_lines,
    savings_from_flash_vs_product_sale,
)
from core.services.coupon_validation import split_seller_discount_across_lines
from core.services.child_spending_service import (
    child_non_personal_spent_windows,
    validate_child_spending_limits,
)
from core.services.purchase_approval_service import consume_purchase_approvals_after_checkout
from core.services.site_settings_policy import site_kyc_required_flag, storefront_orders_gate_response
from core.services import (
    family_join_request_service,
    family_member_provision_service,
    family_portal_join_link_service,
    family_portal_wallet_service,
    family_product_restriction_service,
    family_service,
    otp_service,
    product_service,
    wallet_policy,
    wallet_service,
)
from core.services.nominatim_geocode import (
    NominatimError,
    area_and_landmark_from_nominatim,
    reverse_geocode,
)
from core.services.base import get_or_create_personal_wallet, personal_wallet_qs
from core.services.khalti_epayment_service import KhaltiApiError, KhaltiConfigError
from core.services.withdrawal_notifications import notify_family_withdrawal_submitted
from core.services.withdrawal_requests import (
    create_pending_withdrawal,
    payout_required_block_payload,
)
from core.services.wallet_txn_signed import (
    aggregate_monthly_spent_for_wallet_ids,
    signed_amount_for_wallet_transaction,
    sum_monthly_spent_from_wallet,
)
from core.services.order_service import pay_with_wallet
from core.services import refund_notification_service, support_notification_service, support_ticket_service
from core.services.user_presence import online_user_ids_for
from core.services import refund_service
from core.views.admin.admin_write_utils import (
    absolute_media_url,
    user_public_avatar_url,
    validation_error,
)
from core.views.admin.resource_views import _to_decimal
from core.views.vendor.vendor_resources import _gen_order_number, _gen_ticket_number
from core.views.website.home_views import annotate_reels_comments
from core.services import wallet_gateway_topup as wgt


class IsPortalCustomer(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.role == User.Role.NORMAL)


class IsPortalParent(BasePermission):
    """Parent / co-parent / manager in an active family group (not only User.role)."""

    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and user_has_family_portal_access(u))


class IsPortalWalletOtpUser(BasePermission):
    """Customer, family-portal, or child user (wallet transfer / withdraw OTP)."""

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if u.role == User.Role.NORMAL:
            return True
        if user_has_family_portal_access(u):
            return True
        if u.role == User.Role.CHILD:
            if getattr(settings, "CHILD_PORTAL_REQUIRE_MEMBERSHIP", False):
                return FamilyMember.objects.filter(
                    user=u,
                    role=FamilyMember.Role.CHILD,
                    status=FamilyMember.Status.ACTIVE,
                ).exists()
            return True
        return False


class IsPortalChild(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated or u.role != User.Role.CHILD:
            return False
        if getattr(settings, "CHILD_PORTAL_REQUIRE_MEMBERSHIP", False):
            return FamilyMember.objects.filter(
                user=u,
                role=FamilyMember.Role.CHILD,
                status=FamilyMember.Status.ACTIVE,
            ).exists()
        return True


class IsPortalShopper(BasePermission):
    """Storefront checkout and order history for normal, family-parent, and child accounts."""

    def has_permission(self, request, view):
        u = request.user
        return bool(
            u
            and u.is_authenticated
            and u.role
            in (User.Role.NORMAL, User.Role.PARENT, User.Role.CHILD)
        )


class IsPortalSelf(BasePermission):
    """Customer, parent, or child — self-service profile / password."""

    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        return u.role in (
            User.Role.NORMAL,
            User.Role.PARENT,
            User.Role.CHILD,
        )


class PortalPagination(PageNumberPagination):
    page_size = 30
    page_size_query_param = "page_size"
    max_page_size = 100


def _paginate(request, queryset):
    paginator = PortalPagination()
    page = paginator.paginate_queryset(queryset, request)
    return paginator, page


def _orders_surface_q(list_placed_portal: str) -> Q:
    """List orders for a portal surface; legacy NULL rows appear on main only."""
    if list_placed_portal == Order.PlacedPortal.PORTAL_MAIN:
        return Q(placed_portal=Order.PlacedPortal.PORTAL_MAIN) | Q(
            placed_portal__isnull=True
        )
    return Q(placed_portal=list_placed_portal)


def _user_may_use_placed_portal(u: User, placed: str) -> bool:
    if placed == Order.PlacedPortal.PORTAL_CHILD:
        if u.role != User.Role.CHILD:
            return False
        if getattr(settings, "CHILD_PORTAL_REQUIRE_MEMBERSHIP", False):
            return FamilyMember.objects.filter(
                user=u,
                role=FamilyMember.Role.CHILD,
                status=FamilyMember.Status.ACTIVE,
            ).exists()
        return True
    if placed == Order.PlacedPortal.PORTAL_FAMILY:
        return user_has_family_portal_access(u)
    if placed == Order.PlacedPortal.PORTAL_MAIN:
        return u.role in (User.Role.NORMAL, User.Role.PARENT, User.Role.CHILD)
    return False


def _parse_placed_portal_body(raw) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    s = str(raw).strip()
    if s in dict(Order.PlacedPortal.choices):
        return s
    return None


def _order_matches_list_surface(o: Order, surface: str) -> bool:
    if surface == Order.PlacedPortal.PORTAL_MAIN:
        return o.placed_portal in (None, Order.PlacedPortal.PORTAL_MAIN)
    return o.placed_portal == surface


def _order_within_refund_validity(order: Order, order_settings: OrderSettings) -> bool:
    """True when order is still inside configured refund validity days."""
    max_age = timedelta(days=int(order_settings.refund_validity_days or 0))
    if not max_age:
        return True
    return timezone.now() - order.created_at <= max_age


def _serialize_portal_order_row(o: Order, order_settings: OrderSettings) -> dict:
    """Shape one portal order for list/detail APIs (customer-scoped queryset)."""
    lines = [
        {
            "product_id": it.product_id,
            "name": it.product.name,
            "quantity": it.quantity,
            "unit_price": float(it.unit_price),
            "line_total": float(it.total_price),
        }
        for it in o.items.all()
    ]
    refund_rows = []
    for r in o.refunds.all().order_by("-created_at"):
        fee, net = refund_service.breakdown_for_refund(r)
        refund_rows.append(
            {
                "refund_number": r.refund_number,
                "status": r.status,
                "amount": float(r.amount),
                "gross_amount": float(r.amount),
                "platform_fee": float(fee),
                "net_credit": float(net),
                "reason": r.reason,
                "created_at": r.created_at.isoformat(),
            }
        )
    already = Decimal("0")
    for r in o.refunds.all():
        if r.status == Refund.Status.APPROVED:
            already += r.amount
    remaining = max(Decimal("0"), Decimal(o.total) - already)
    refund_estimate = None
    refund_allowed = False
    if (
        remaining > 0
        and o.payment_method == Order.PaymentMethod.WALLET
        and o.payment_status == Order.PaymentStatus.PAID
        and o.status not in (Order.Status.CANCELLED, Order.Status.REFUNDED)
        and not o.refunds.filter(status=Refund.Status.PENDING).exists()
        and _order_within_refund_validity(o, order_settings)
    ):
        refund_allowed = True
        try:
            fe = refund_service.refund_financials(
                o, remaining, persist_settlement=False
            )
            refund_estimate = {
                "gross": float(remaining),
                "platform_fee": float(fe.fee_retained),
                "net_credit": float(fe.customer_credit),
                "platform_retention_label": refund_service.commission_slice_retention_short_label(),
            }
        except ValueError:
            refund_estimate = None

    item_count = getattr(o, "item_count", None)
    if item_count is None:
        item_count = o.items.count()

    return {
        "id": o.order_number,
        "pk": o.pk,
        "date": o.created_at.date().isoformat(),
        "status": o.status,
        "total": float(o.total),
        "items": item_count,
        "payment": o.get_payment_method_display(),
        "seller": o.seller.store_name if o.seller_id else "In-House",
        "seller_id": o.seller_id,
        "lines": lines,
        "refunds": refund_rows,
        "refund_estimate": refund_estimate,
        "refund_allowed": refund_allowed,
    }


def _notify_wallet_recipient(
    recipient: User, title: str, message: str, action_url: str = ""
) -> None:
    Notification.objects.create(
        recipient=recipient,
        title=title[:150],
        message=message,
        type=Notification.Type.WALLET,
        target=Notification.Target.CUSTOMERS,
        action_url=(action_url or "")[:255],
    )


def _distribution_recipient_error(
    fm: FamilyMember, category: FamilyWalletCategory | None
) -> str | None:
    if fm.role != FamilyMember.Role.CHILD:
        return "Only child members can receive distributed funds."
    if not family_portal_wallet_service.category_allows_member_role(category, fm.role):
        return "This category cannot be used for this member role."
    return None


def _parse_allowed_member_roles(data: Mapping) -> list[str]:
    raw = data.get("allowed_member_roles")
    if raw is None and hasattr(data, "getlist"):
        lst = [x for x in data.getlist("allowed_member_roles") if str(x).strip()]
        if lst:
            raw = lst
    valid = {c[0] for c in FamilyMember.Role.choices}
    if raw is None:
        return ["child"]
    if isinstance(raw, str) and raw.strip().startswith("["):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError("allowed_member_roles must be valid JSON array") from e
    if not isinstance(raw, list):
        raise ValueError("allowed_member_roles must be a list of role strings")
    roles = [str(x).strip().lower() for x in raw if str(x).strip()]
    roles = [r for r in roles if r in valid]
    return roles if roles else ["child"]


def _wallet_for_user(user, wallet_type=None, family_group=None):
    qs = Wallet.objects.filter(owner=user).exclude(type=Wallet.Type.VENDOR)
    if family_group is not None:
        w = qs.filter(family_group=family_group).first()
        if w:
            return w
    if wallet_type:
        w = qs.filter(type=wallet_type).first()
        if w:
            return w
    return qs.filter(type=Wallet.Type.PERSONAL).first() or qs.order_by("id").first()


def _wallet_balance(user, family_group=None):
    w = _wallet_for_user(user, family_group=family_group)
    return float(w.balance) if w else 0.0


def _family_groups_for_parent_user(user):
    """Collect active groups; private families first, then platform hub groups."""
    led = list(
        FamilyGroup.objects.filter(leader=user, status=FamilyGroup.Status.ACTIVE)
    )
    member_groups = list(
        FamilyMember.objects.filter(
            user=user, status=FamilyMember.Status.ACTIVE
        ).select_related("group")
    )
    groups = {g.id: g for g in led}
    for fm in member_groups:
        groups[fm.group_id] = fm.group
    out = list(groups.values())
    out.sort(key=lambda g: (1 if g.is_platform_hub else 0, g.id))
    return out


def _primary_family_group(user):
    """Prefer a group where this user may create invites (leader or parent/spouse/manager)."""
    gl = _family_groups_for_parent_user(user)
    if not gl:
        return None
    for g in gl:
        if family_service.user_can_manage_family_invites(user, g):
            return g
    return gl[0]


def _add_member_roles_payload():
    return [{"value": c[0], "label": c[1]} for c in FamilyJoinRequest.Role.choices]


def _online_user_ids_for_users(user_ids: list[int]) -> set[int]:
    """Treat users with at least one active Django session as online."""
    if not user_ids:
        return set()
    user_id_set = {int(uid) for uid in user_ids}
    online_user_ids: set[int] = set()
    active_sessions = Session.objects.filter(expire_date__gte=timezone.now())
    for session in active_sessions.iterator():
        data = session.get_decoded()
        raw_uid = data.get("_auth_user_id")
        if raw_uid is None:
            continue
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue
        if uid in user_id_set:
            online_user_ids.add(uid)
    return online_user_ids


def _family_member_portal_row(
    m: FamilyMember,
    group: FamilyGroup,
    *,
    spent_by_wallet: dict[int, Decimal] | None = None,
    online_user_ids: set[int] | None = None,
    monthly_spent_override: Decimal | None = None,
) -> dict:
    ginfo = {"id": str(group.pk), "name": group.name}
    mw = family_portal_wallet_service.get_member_family_wallet(group, m.user)
    portal_w = _wallet_for_user(m.user)
    if mw and portal_w and mw.pk == portal_w.pk:
        bal = float(mw.balance)
    elif portal_w and portal_w.type == Wallet.Type.SHARED:
        bal = float(mw.balance) if mw else _wallet_balance(m.user, family_group=group)
    elif mw and portal_w and mw.pk != portal_w.pk and portal_w.type != Wallet.Type.SHARED:
        bal = float(mw.balance) + float(portal_w.balance)
    elif mw:
        bal = float(mw.balance)
    else:
        bal = _wallet_balance(m.user, family_group=group)
    if monthly_spent_override is not None:
        spent = float(monthly_spent_override)
    elif mw and spent_by_wallet is not None:
        spent = float(spent_by_wallet.get(mw.pk, Decimal("0")))
    elif mw:
        spent = float(sum_monthly_spent_from_wallet(mw))
    else:
        spent = 0.0
    return {
        "id": str(m.pk),
        "name": m.user.name,
        "role": m.role,
        "phone": m.user.phone,
        "email": m.user.email or "",
        "balance": bal,
        "status": m.status,
        "avatar": "👤",
        "spending": spent,
        "limit": float(m.spending_limit_monthly or 0),
        "spending_limit_daily": float(m.spending_limit_daily or 0),
        "spending_limit_weekly": float(m.spending_limit_weekly or 0),
        "spending_limit_monthly": float(m.spending_limit_monthly or 0),
        "is_leader": group.leader_id == m.user_id,
        "is_online": bool(online_user_ids and m.user_id in online_user_ids),
        "group": ginfo,
        "wallet_id": str(mw.pk) if mw else None,
    }


def _sync_member_family_wallets_status(
    group: FamilyGroup, user: User, *, frozen: bool
) -> None:
    wst = Wallet.Status.FROZEN if frozen else Wallet.Status.ACTIVE
    Wallet.objects.filter(family_group=group, owner=user).exclude(
        type=Wallet.Type.VENDOR
    ).update(status=wst)


def _family_portal_overview_payload(user: User, request=None) -> dict:
    """Payload for GET /portal/family/members/ (family dashboard overview)."""
    groups = _family_groups_for_parent_user(user)
    if not groups:
        return {
            "group": None,
            "members": [],
            "pending": [],
            "join_requests": [],
            "wallet_categories": [],
            "master_wallet_balance": 0.0,
            "add_member_roles": _add_member_roles_payload(),
            "viewer": None,
            "batch_invite_defaults": {"spending_limit": None},
        }

    primary = _primary_family_group(user) or groups[0]
    member_qs = FamilyMember.objects.filter(group=primary).select_related("user")
    member_user_ids = list(member_qs.values_list("user_id", flat=True))
    online_user_ids = _online_user_ids_for_users(member_user_ids)
    wallet_ids: list[int] = []
    for m in member_qs:
        mw = family_portal_wallet_service.get_member_family_wallet(primary, m.user)
        if mw:
            wallet_ids.append(mw.pk)
    spent_by_wallet = aggregate_monthly_spent_for_wallet_ids(wallet_ids)
    members_out = []
    for m in member_qs:
        child_month_override = None
        if m.role == FamilyMember.Role.CHILD:
            child_month_override = child_non_personal_spent_windows(m.user)["monthly"]
        members_out.append(
            _family_member_portal_row(
                m,
                primary,
                spent_by_wallet=spent_by_wallet,
                online_user_ids=online_user_ids,
                monthly_spent_override=child_month_override,
            )
        )

    pending_inv = FamilyInvite.objects.filter(
        group=primary, status=FamilyInvite.Status.PENDING
    ).order_by("-created_at")[:20]
    pending_out = []
    for inv in pending_inv:
        jr_id = (
            FamilyJoinRequest.objects.filter(
                invite=inv, status=FamilyJoinRequest.Status.PENDING
            )
            .values_list("id", flat=True)
            .first()
        )
        pending_out.append(
            {
                "id": str(inv.pk),
                "join_request_id": str(jr_id) if jr_id else None,
                "member": inv.phone or "Invite",
                "type": "join",
                "item": f"Role: {inv.role}",
                "amount": float(inv.spending_limit),
                "time": inv.created_at.strftime("%Y-%m-%d"),
            }
        )

    if primary.leader_id == user.pk:
        for par in (
            PurchaseApprovalRequest.objects.filter(
                parent_id=primary.leader_id,
                status=PurchaseApprovalRequest.Status.PENDING,
            )
            .select_related("child", "product")
            .order_by("-created_at")[:25]
        ):
            pending_out.append(
                {
                    "id": str(par.pk),
                    "join_request_id": None,
                    "member": (par.child.name or par.child.phone or "Child").strip(),
                    "type": "purchase_approval",
                    "item": par.product.name,
                    "amount": float(par.amount),
                    "time": par.created_at.strftime("%Y-%m-%d"),
                }
            )

    master = family_portal_wallet_service.get_default_shared_wallet(primary)
    master_balance = float(master.balance) if master else 0.0

    wallet_cats = []
    if master:
        wallet_cats.append(
            {
                "id": str(master.pk),
                "category_id": None,
                "name": master.label or "Family wallet",
                "balance": float(master.balance),
                "members": member_qs.count(),
                "allowed_member_roles": [c[0] for c in FamilyMember.Role.choices],
                "icon": "👨‍👩‍👧‍👦",
                "color": "bg-blue-500",
                "image_url": "",
            }
        )
    leader_user = primary.leader
    for cat in FamilyWalletCategory.objects.filter(group=primary).order_by(
        "sort_order", "name"
    ):
        w = family_portal_wallet_service.ensure_category_shared_wallet(
            primary, cat, leader_user
        )
        wallet_cats.append(
            {
                "id": str(w.pk),
                "category_id": str(cat.pk),
                "name": cat.name,
                "balance": float(w.balance),
                "members": member_qs.count(),
                "allowed_member_roles": list(cat.allowed_member_roles or []),
                "icon": "📁",
                "color": "bg-indigo-500",
                "image_url": (
                    absolute_media_url(request, cat.image)
                    if request is not None and cat.image
                    else ""
                ),
            }
        )

    join_req_qs = (
        FamilyJoinRequest.objects.filter(group=primary)
        .select_related("requested_by")
        .order_by("-created_at")[:50]
    )
    join_requests_out = FamilyJoinRequestReadSerializer(join_req_qs, many=True).data

    viewer_fm = member_qs.filter(user=user).first()
    viewer = {
        "user_id": str(user.pk),
        "family_member_id": str(viewer_fm.pk) if viewer_fm else "",
        "role": viewer_fm.role if viewer_fm else "",
        "is_leader": primary.leader_id == user.pk,
    }

    perm_row = FamilyGroupPermission.objects.filter(group=primary).first()
    batch_invite_defaults = {
        "spending_limit": (
            float(perm_row.default_invite_spending_limit)
            if perm_row and perm_row.default_invite_spending_limit is not None
            else None
        ),
    }

    return {
        "group": {
            "id": str(primary.pk),
            "name": primary.name,
            "leader_id": str(primary.leader_id),
        },
        "members": members_out,
        "pending": pending_out,
        "join_requests": join_requests_out,
        "wallet_categories": wallet_cats,
        "master_wallet_balance": master_balance,
        "add_member_roles": _add_member_roles_payload(),
        "viewer": viewer,
        "batch_invite_defaults": batch_invite_defaults,
    }


def _portal_password_login(request, portal_key: str):
    phone = request.data.get("phone", "").strip()
    password = request.data.get("password", "")
    user = authenticate_user_by_phone(request, phone, password)
    if not user:
        return Response({"detail": "Invalid credentials."}, status=400)
    denied = assert_portal_login_allowed(user, portal_key)
    if denied:
        return denied
    token, _ = Token.objects.get_or_create(user=user)
    return Response(
        {
            "token": token.key,
            "user": {
                "id": user.id,
                "name": user.name,
                "phone": user.phone,
                "role": user.role,
                "kyc_status": user.kyc_status,
            },
        }
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def portal_login(request):
    return _portal_password_login(request, PORTAL_MAIN)


@api_view(["POST"])
@permission_classes([AllowAny])
def family_portal_login(request):
    return _portal_password_login(request, PORTAL_FAMILY)


@api_view(["POST"])
@permission_classes([AllowAny])
def child_portal_login(request):
    return _portal_password_login(request, PORTAL_CHILD)


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_me(request):
    u = request.user
    from core.services.kyc_service import sync_user_kyc_status
    from core.services.kyc_withdraw import latest_kyc_rejection_reason

    sync_user_kyc_status(u)
    u.refresh_from_db()
    w = _wallet_for_user(u)

    return Response(
        {
            "id": u.id,
            "name": u.name,
            "phone": u.phone,
            "email": u.email or "",
            "role": u.role,
            "kyc_status": u.kyc_status,
            "kyc_required": site_kyc_required_flag(),
            "kyc_rejection_reason": latest_kyc_rejection_reason(u),
            "wallet_id": str(w.pk) if w else None,
            "wallet_balance": float(w.balance) if w else 0.0,
        }
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_summary(request):
    u = request.user
    balance = _wallet_balance(u)
    orders_qs = Order.objects.filter(customer=u)
    total_orders = orders_qs.count()
    pending_deliveries = orders_qs.filter(
        status__in=[
            Order.Status.PENDING,
            Order.Status.PROCESSING,
            Order.Status.SHIPPED,
        ]
    ).count()
    spent = (
        orders_qs.filter(status=Order.Status.DELIVERED).aggregate(t=Sum("total"))["t"]
        or Decimal("0")
    )
    unread = Notification.objects.filter(recipient=u, is_read=False).count()
    return Response(
        {
            "wallet_balance": balance,
            "total_orders": total_orders,
            "pending_deliveries": pending_deliveries,
            "total_spent": float(spent),
            "notifications_count": unread,
        }
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_orders_list(request, list_placed_portal: str | None = None):
    u = request.user
    surface = list_placed_portal or Order.PlacedPortal.PORTAL_MAIN
    if not _user_may_use_placed_portal(u, surface):
        return Response(
            {"detail": "You cannot list orders for this portal surface."},
            status=403,
        )
    qs = (
        Order.objects.filter(customer=u)
        .filter(_orders_surface_q(surface))
        .select_related("seller")
        .prefetch_related("items__product", "refunds")
        .annotate(item_count=Count("items"))
        .order_by("-created_at")
    )
    paginator, page = _paginate(request, qs)
    order_settings = OrderSettings.load()
    rows = [_serialize_portal_order_row(o, order_settings) for o in page]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_order_detail(request, pk: int, list_placed_portal: str | None = None):
    u = request.user
    surface = list_placed_portal or Order.PlacedPortal.PORTAL_MAIN
    if not _user_may_use_placed_portal(u, surface):
        return Response(
            {"detail": "You cannot view orders for this portal surface."},
            status=403,
        )
    o = (
        Order.objects.filter(pk=pk, customer=u)
        .filter(_orders_surface_q(surface))
        .select_related("seller")
        .prefetch_related("items__product", "refunds")
        .annotate(item_count=Count("items"))
        .first()
    )
    if not o:
        return Response({"detail": "Order not found."}, status=404)
    order_settings = OrderSettings.load()
    return Response(_serialize_portal_order_row(o, order_settings))


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_order_refund_request(request, pk: int, refund_surface: str):
    """Create a pending refund request; super admin approves via admin API."""
    u = request.user
    if not _user_may_use_placed_portal(u, refund_surface):
        return Response(
            {"detail": "You cannot request refunds for this portal surface."},
            status=403,
        )
    o = Order.objects.filter(pk=pk, customer=u).first()
    if not o:
        return Response({"detail": "Order not found."}, status=404)
    if not _order_matches_list_surface(o, refund_surface):
        return Response({"detail": "Order not found."}, status=404)
    if o.payment_method != Order.PaymentMethod.WALLET:
        return validation_error(
            "Refunds are only supported for wallet-paid orders.",
            field="order",
        )
    if o.payment_status != Order.PaymentStatus.PAID:
        return validation_error(
            "Order must be paid before requesting a refund.",
            field="order",
        )
    if o.status in (Order.Status.CANCELLED, Order.Status.REFUNDED):
        return validation_error("This order cannot be refunded.", field="order")

    order_settings = OrderSettings.load()
    if not _order_within_refund_validity(o, order_settings):
        return validation_error("Refund period has expired.", field="order")

    already = (
        Refund.objects.filter(order=o, status=Refund.Status.APPROVED).aggregate(
            s=Sum("amount")
        )["s"]
        or Decimal("0")
    )
    remaining = Decimal(o.total) - already
    if remaining <= Decimal("0"):
        return validation_error("Nothing left to refund for this order.", field="amount")

    if Refund.objects.filter(order=o, status=Refund.Status.PENDING).exists():
        return validation_error(
            "A refund request is already pending for this order.",
            field="order",
        )

    data = request.data if isinstance(request.data, Mapping) else {}
    amount = remaining

    reason = (data.get("reason") or "").strip()
    notes = (data.get("notes") or "").strip()
    if notes:
        reason = f"{reason}\n\nNotes: {notes}" if reason else f"Notes: {notes}"
    if not reason:
        return validation_error("reason is required", field="reason")

    fin = refund_service.refund_financials(o, amount, persist_settlement=True)
    refund_no = f"RF-{timezone.now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
    rf = Refund.objects.create(
        refund_number=refund_no,
        order=o,
        customer=u,
        amount=amount,
        platform_fee_amount=fin.fee_retained,
        net_credit_amount=fin.customer_credit,
        reason=reason[:4000],
        status=Refund.Status.PENDING,
    )
    refund_notification_service.notify_admins_new_refund_request(rf)
    return Response(
        {
            "ok": True,
            "refund_number": refund_no,
            "gross_amount": float(amount),
            "platform_fee": float(fin.fee_retained),
            "net_credit": float(fin.customer_credit),
            "platform_retention_label": refund_service.commission_slice_retention_short_label(),
            "amount": float(amount),
            "status": rf.status,
        },
        status=201,
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_wallet_transactions(request):
    u = request.user
    w = _resolve_default_checkout_wallet(u)
    if not w:
        return Response({"count": 0, "next": None, "previous": None, "results": []})
    qs = (
        WalletTransaction.objects.filter(wallet=w)
        .select_related("wallet", "wallet__family_group", "wallet__family_category")
        .order_by("-created_at")
    )
    paginator, page = _paginate(request, qs)
    rows = []
    for t in page:
        typ = t.type
        amt = float(t.amount)
        if typ in (WalletTransaction.Type.DEBIT, WalletTransaction.Type.PURCHASE, WalletTransaction.Type.WITHDRAWAL):
            ui_type = "debit"
            display_amt = abs(amt)
        elif typ in (
            WalletTransaction.Type.CREDIT,
            WalletTransaction.Type.TOPUP,
            WalletTransaction.Type.BONUS,
            WalletTransaction.Type.REFUND_CREDIT,
        ):
            ui_type = "credit"
            display_amt = abs(amt)
        else:
            ui_type = "transfer"
            display_amt = abs(amt)
        st = t.status
        status_ui = "completed" if st == WalletTransaction.Status.COMPLETED else (
            "failed" if st == WalletTransaction.Status.FAILED else "pending"
        )
        src = (t.fund_source or "").strip()
        if not src and t.wallet_id:
            src = _fund_source_label_for_wallet(t.wallet)
        rows.append(
            {
                "id": t.txn_id,
                "date": t.created_at.date().isoformat(),
                "type": ui_type,
                "description": t.description or typ,
                "amount": display_amt,
                "status": status_ui,
                "to": "",
                "fund_source": src,
            }
        )
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_notifications_list(request):
    u = request.user
    qs = Notification.objects.filter(
        Q(recipient=u) | Q(recipient__isnull=True, target=Notification.Target.CUSTOMERS)
    ).order_by("-created_at")[:50]
    rows = []
    for n in qs:
        body = (n.message or "")[:500]
        rows.append(
            {
                "id": str(n.pk),
                "type": n.type,
                "title": n.title,
                "message": body,
                "time": n.created_at.isoformat(),
                "created_at": n.created_at.isoformat(),
                "is_read": bool(n.is_read),
                "action_url": n.action_url or "",
                "urgent": n.type == Notification.Type.SECURITY,
                "preview": f"{n.title}: {body}"[:200],
            }
        )
    return Response(rows)


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_notifications_mark_read(request):
    u = request.user
    data = request.data if isinstance(request.data, Mapping) else {}
    mark_all = data.get("all")
    if mark_all in (True, "true", "1", 1):
        updated = Notification.objects.filter(recipient=u, is_read=False).update(is_read=True)
        return Response({"ok": True, "updated": updated})
    raw_ids = data.get("ids")
    if raw_ids is None:
        return Response({"detail": "Provide all: true or ids: [...]"}, status=400)
    if not isinstance(raw_ids, list):
        return Response({"detail": "ids must be a list"}, status=400)
    pks = []
    for x in raw_ids:
        try:
            pks.append(int(x))
        except (TypeError, ValueError):
            continue
    if not pks:
        return Response({"ok": True, "updated": 0})
    updated = Notification.objects.filter(recipient=u, pk__in=pks, is_read=False).update(
        is_read=True
    )
    return Response({"ok": True, "updated": updated})


@api_view(["DELETE"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_notification_detail_write(request, pk):
    row = Notification.objects.filter(pk=pk, recipient=request.user).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    row.delete()
    return Response({"ok": True})


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_children(request):
    """Child wallet rows for parent users (same family group)."""
    u = request.user
    memberships = FamilyMember.objects.filter(
        user=u,
        role__in=[FamilyMember.Role.PARENT, FamilyMember.Role.SPOUSE, FamilyMember.Role.MANAGER],
        status=FamilyMember.Status.ACTIVE,
    ).select_related("group")
    group_ids = [m.group_id for m in memberships]
    if not group_ids:
        return Response([])
    children = (
        FamilyMember.objects.filter(group_id__in=group_ids, role=FamilyMember.Role.CHILD)
        .select_related("user", "group")
        .distinct()
    )
    rows = []
    for m in children:
        mw = family_portal_wallet_service.get_member_family_wallet(m.group, m.user)
        bal = float(mw.balance) if mw else _wallet_balance(m.user, family_group=m.group)
        rows.append(
            {
                "id": str(m.user_id),
                "name": m.user.name,
                "avatar": (m.user.name[:1] or "?").upper(),
                "balance": bal,
                "spendingLimit": float(m.spending_limit_monthly or 0),
                "spent": 0.0,
                "lastActivity": m.joined_at.date().isoformat(),
            }
        )
    return Response(rows)


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_members(request):
    if request.method == "GET":
        return Response(_family_portal_overview_payload(request.user, request))

    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    ser = PortalFamilyAddMemberSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    v = ser.validated_data
    try:
        fm = family_member_provision_service.provision_family_member(
            acting_user=request.user,
            group=primary,
            name=v["name"],
            email=v.get("email") or "",
            phone=v["phone"],
            role=v["role"],
            spending_limit=v["spending_limit"],
            initial_balance=v["initial_balance"],
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    fm = FamilyMember.objects.filter(pk=fm.pk).select_related("user").first()
    return Response(
        {
            "ok": True,
            "member": _family_member_portal_row(
                fm,
                primary,
                online_user_ids=_online_user_ids_for_users([fm.user_id]),
            ),
        },
        status=201,
    )


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_join_requests(request):
    primary = _primary_family_group(request.user)
    if not primary:
        if request.method == "GET":
            return Response({"results": []})
        return Response({"detail": "No family group found."}, status=400)
    if request.method == "GET":
        qs = (
            FamilyJoinRequest.objects.filter(group=primary)
            .select_related("requested_by")
            .order_by("-created_at")[:100]
        )
        return Response({"results": FamilyJoinRequestReadSerializer(qs, many=True).data})
    ser = PortalFamilyAddMemberSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    v = ser.validated_data
    im = (
        FamilyInvite.InviteMethod.LINK
        if v["invite_method"] == "link"
        else FamilyInvite.InviteMethod.PHONE
    )
    try:
        jr, inv = family_join_request_service.create_join_request_with_invite(
            parent=request.user,
            group=primary,
            name=v["name"],
            email=v.get("email") or "",
            phone=v["phone"],
            role=v["role"],
            age=v.get("age"),
            spending_limit=v["spending_limit"],
            initial_balance=v["initial_balance"],
            invite_method=im,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(
        {
            "join_request": FamilyJoinRequestReadSerializer(jr).data,
            "invite": {
                "id": str(inv.pk),
                "token": inv.token,
                "phone": inv.phone,
                "role": inv.role,
                "expires_at": inv.expires_at.isoformat(),
            },
        },
        status=201,
    )


def _family_join_share_url(token: str) -> str:
    base = getattr(settings, "FRONTEND_URL", "http://localhost:8080").rstrip("/")
    return f"{base}/join-family/{token}"


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_join_share_link(request):
    primary = _primary_family_group(request.user)
    if not primary:
        if request.method == "GET":
            return Response(
                {
                    "active": False,
                    "token": None,
                    "join_url": None,
                    "expires_at": None,
                    "title": "",
                    "welcome_message": "",
                    "default_role": "child",
                }
            )
        return Response({"detail": "No family group found."}, status=400)
    if not family_service.user_can_manage_family_invites(request.user, primary):
        return Response({"detail": "You cannot manage join links for this family."}, status=403)

    if request.method == "GET":
        link = family_portal_join_link_service.get_active_link_for_group(primary)
        if not link:
            return Response(
                {
                    "active": False,
                    "token": None,
                    "join_url": None,
                    "expires_at": None,
                    "title": "",
                    "welcome_message": "",
                    "default_role": "child",
                }
            )
        return Response(
            {
                "active": True,
                "token": link.token,
                "join_url": _family_join_share_url(link.token),
                "expires_at": link.expires_at.isoformat() if link.expires_at else None,
                "title": link.title or "",
                "welcome_message": link.welcome_message or "",
                "default_role": link.default_role,
                "created_at": link.created_at.isoformat(),
            }
        )

    ser = PortalFamilyJoinShareLinkCreateSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    v = ser.validated_data
    try:
        link = family_portal_join_link_service.create_or_rotate_link(
            creator=request.user,
            group=primary,
            default_role=v.get("default_role") or "child",
            title=v.get("title") or "",
            welcome_message=v.get("welcome_message") or "",
            expires_in_days=v.get("expires_in_days"),
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(
        {
            "ok": True,
            "token": link.token,
            "join_url": _family_join_share_url(link.token),
            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
            "title": link.title or "",
            "welcome_message": link.welcome_message or "",
            "default_role": link.default_role,
        },
        status=201,
    )


@api_view(["PATCH"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_join_request_detail(request, pk: int):
    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    jr = FamilyJoinRequest.objects.filter(pk=pk, group=primary).first()
    if not jr:
        return Response({"detail": "Not found."}, status=404)
    ser = PortalFamilyJoinRequestPatchSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    action = ser.validated_data["action"]
    try:
        if action == "approve":
            family_join_request_service.approve_join_request(
                reviewer=request.user, jr=jr
            )
        else:
            family_join_request_service.reject_join_request(
                reviewer=request.user, jr=jr
            )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    jr.refresh_from_db()
    return Response(
        {
            "ok": True,
            "status": jr.status,
            "join_request": FamilyJoinRequestReadSerializer(jr).data,
        }
    )


@api_view(["GET", "PUT", "PATCH"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_product_restrictions(request):
    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    if request.method == "GET":
        rows = family_product_restriction_service.list_group_level_restrictions(
            group=primary
        )
        return Response(
            {"results": PortalProductRestrictionReadSerializer(rows, many=True).data}
        )
    if request.method == "PUT":
        ser = PortalProductRestrictionsReplaceSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=400)
        try:
            family_product_restriction_service.replace_group_level_restrictions(
                group=primary,
                rules=ser.validated_data["rules"],
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        rows = family_product_restriction_service.list_group_level_restrictions(
            group=primary
        )
        return Response(
            {"results": PortalProductRestrictionReadSerializer(rows, many=True).data}
        )
    ser = PortalProductRestrictionUpsertSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    v = ser.validated_data
    try:
        pr = family_product_restriction_service.upsert_group_level_restriction(
            group=primary,
            category_id=v["category_id"],
            is_blocked=v.get("is_blocked", False),
            requires_approval=v.get("requires_approval", False),
            max_price=v.get("max_price"),
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    if pr is None:
        return Response({"ok": True, "removed": True})
    return Response(PortalProductRestrictionReadSerializer(pr).data)


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_auto_approval_rules(request):
    primary = _primary_family_group(request.user)
    if not primary:
        if request.method == "GET":
            return Response({"results": []})
        return Response({"detail": "No family group found."}, status=400)
    if request.method == "GET":
        qs = (
            AutoApprovalRule.objects.filter(group=primary)
            .select_related("category")
            .order_by("name", "id")
        )
        return Response(
            {"results": PortalAutoApprovalRuleReadSerializer(qs, many=True).data}
        )
    if not family_service.user_can_manage_family_invites(request.user, primary):
        return Response(
            {"detail": "You do not have permission to manage auto-approval rules."},
            status=403,
        )
    ser = PortalAutoApprovalRuleCreateSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    rule = ser.save(group=primary)
    return Response(
        PortalAutoApprovalRuleReadSerializer(rule).data,
        status=201,
    )


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_auto_approval_rule_detail(request, pk: int):
    primary = _primary_family_group(request.user)
    if not primary:
        if request.method == "GET":
            return Response({"detail": "Not found."}, status=404)
        return Response({"detail": "No family group found."}, status=400)
    rule = (
        AutoApprovalRule.objects.filter(pk=pk, group=primary)
        .select_related("category")
        .first()
    )
    if not rule:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        return Response(PortalAutoApprovalRuleReadSerializer(rule).data)
    if not family_service.user_can_manage_family_invites(request.user, primary):
        return Response(
            {"detail": "You do not have permission to manage auto-approval rules."},
            status=403,
        )
    if request.method == "DELETE":
        rule.delete()
        return Response({"ok": True})
    pser = PortalAutoApprovalRulePatchSerializer(data=request.data)
    if not pser.is_valid():
        return Response(pser.errors, status=400)
    v = pser.validated_data
    if "name" in v:
        rule.name = v["name"]
    if "description" in v:
        rule.description = v["description"]
    if "category_id" in v:
        rule.category = v["category_id"]
    if "max_amount" in v:
        rule.max_amount = v["max_amount"]
    if "is_enabled" in v:
        rule.is_enabled = v["is_enabled"]
    rule.save()
    rule.refresh_from_db()
    return Response(PortalAutoApprovalRuleReadSerializer(rule).data)


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_members_batch(request):
    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    ser = PortalFamilyAddMembersBatchSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    sl = ser.validated_data["spending_limit"]
    ib = ser.validated_data["initial_balance"]
    out = []
    try:
        with transaction.atomic():
            for v in ser.validated_data["members"]:
                fm = family_member_provision_service.provision_family_member(
                    acting_user=request.user,
                    group=primary,
                    name=v["name"],
                    email=v.get("email") or "",
                    phone=v["phone"],
                    role=v["role"],
                    spending_limit=sl,
                    initial_balance=ib,
                )
                fm = FamilyMember.objects.filter(pk=fm.pk).select_related("user").first()
                out.append(
                    {
                        "ok": True,
                        "member": _family_member_portal_row(
                            fm,
                            primary,
                            online_user_ids=_online_user_ids_for_users([fm.user_id]),
                        ),
                    }
                )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    perm, _ = FamilyGroupPermission.objects.get_or_create(group=primary)
    perm.default_invite_spending_limit = sl
    perm.save(update_fields=["default_invite_spending_limit"])
    return Response({"results": out}, status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_member_detail(request, pk: int):
    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    if not family_service.user_can_manage_family_invites(request.user, primary):
        return Response(
            {"detail": "You do not have permission to manage family members."},
            status=403,
        )
    fm = (
        FamilyMember.objects.filter(pk=pk, group=primary)
        .select_related("user")
        .first()
    )
    if not fm:
        return Response({"detail": "Not found."}, status=404)
    if primary.leader_id == fm.user_id:
        return Response(
            {"detail": "Cannot change or remove the group leader from this endpoint."},
            status=400,
        )

    if request.method == "DELETE":
        if fm.user_id == request.user.pk:
            return Response(
                {"detail": "You cannot remove yourself from the family."},
                status=400,
            )
        fm.delete()
        return Response({"ok": True})

    ser = PortalFamilyMemberPatchSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    v = ser.validated_data
    update_fields: list[str] = []

    with transaction.atomic():
        fm_locked = (
            FamilyMember.objects.select_for_update()
            .select_related("user")
            .get(pk=fm.pk)
        )
        if "role" in v:
            new_role = v["role"]
            old_role = fm_locked.role
            if new_role != old_role:
                fm_locked.role = new_role
                update_fields.append("role")
                u = fm_locked.user
                if new_role == FamilyMember.Role.CHILD:
                    User.objects.filter(pk=u.pk).update(role=User.Role.CHILD)
                elif old_role == FamilyMember.Role.CHILD and new_role != FamilyMember.Role.CHILD:
                    if u.role == User.Role.CHILD:
                        User.objects.filter(pk=u.pk).update(role=User.Role.NORMAL)
                family_service.ensure_family_wallets_for_member(
                    primary, u, new_role
                )
        for fld in (
            "spending_limit_daily",
            "spending_limit_weekly",
            "spending_limit_monthly",
        ):
            if fld in v:
                setattr(fm_locked, fld, v[fld])
                update_fields.append(fld)
        if "status" in v:
            new_ms = (
                FamilyMember.Status.ACTIVE
                if v["status"] == "active"
                else FamilyMember.Status.FROZEN
            )
            if fm_locked.status != new_ms:
                fm_locked.status = new_ms
                update_fields.append("status")
                _sync_member_family_wallets_status(
                    primary, fm_locked.user, frozen=new_ms == FamilyMember.Status.FROZEN
                )
        if update_fields:
            fm_locked.save(update_fields=list(dict.fromkeys(update_fields)))
        fm = fm_locked

    fm.refresh_from_db()
    return Response(
        _family_member_portal_row(
            fm,
            primary,
            online_user_ids=_online_user_ids_for_users([fm.user_id]),
        )
    )


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_wallet_load(request):
    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    method = (request.data.get("method") or "esewa").strip()[:50]
    method_norm = method.lower()
    if request.data.get("category_id") not in (None, ""):
        return validation_error(
            "Top-ups credit only the main family wallet; use Transfer to move funds into a bucket.",
            field="category_id",
        )
    if method_norm not in ("esewa", "khalti"):
        return validation_error(
            "Only eSewa and Khalti are supported for adding money to the family wallet.",
            field="method",
        )
    w = family_portal_wallet_service.ensure_default_shared_wallet(primary, primary.leader)
    raw_return = (request.data.get("return_path") or "/family-portal/wallets-overview").strip()
    return_path = raw_return[:500] if raw_return.startswith("/") else f"/{raw_return[:499].lstrip('/')}"
    try:
        wgt.assert_can_topup_wallet(payer=request.user, wallet=w, target=wgt.TOPUP_TARGET_FAMILY_MASTER)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    if method_norm == "esewa":
        return Response(
            wgt.build_esewa_initiate_response(
                request=request,
                payer=request.user,
                wallet=w,
                amount=amount,
                method=method,
                topup_target=wgt.TOPUP_TARGET_FAMILY_MASTER,
                return_path=return_path,
                return_query_esewa=None,
                success_reverse_name="portal-wallet-topup-esewa-success",
                failure_reverse_name="portal-wallet-topup-esewa-failure",
            )
        )
    try:
        return Response(
            wgt.build_khalti_initiate_response(
                payer=request.user,
                wallet=w,
                amount=amount,
                method=method,
                topup_target=wgt.TOPUP_TARGET_FAMILY_MASTER,
                return_path=return_path,
                return_query_esewa=None,
                purchase_order_id=f"KP-F-{uuid4().hex[:24]}",
                purchase_order_name="Family wallet load",
            )
        )
    except ValueError as e:
        return validation_error(str(e), field="amount")
    except KhaltiConfigError as e:
        return Response({"detail": str(e)}, status=503)
    except KhaltiApiError as e:
        return Response(
            {"detail": str(e), "khalti_error": (e.body or "")[:2000]},
            status=502,
        )


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_wallet_distribute(request):
    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    category_id = request.data.get("category_id")
    category = None
    if category_id not in (None, ""):
        category = FamilyWalletCategory.objects.filter(
            pk=category_id, group=primary
        ).first()
        if not category:
            return validation_error("invalid category_id", field="category_id")

    allocations = request.data.get("allocations")
    if allocations is not None:
        if not isinstance(allocations, list) or not allocations:
            return validation_error(
                "allocations must be a non-empty list", field="allocations"
            )
        payout_notice: list[tuple[User, Decimal]] = []
        try:
            with transaction.atomic():
                for row in allocations:
                    mid = row.get("member_id")
                    amt = _to_decimal(row.get("amount"), "0")
                    if amt <= 0:
                        raise ValueError("Each amount must be positive.")
                    fm = FamilyMember.objects.filter(
                        pk=mid, group=primary
                    ).select_related("user").first()
                    if not fm:
                        raise ValueError("Invalid member_id in allocations.")
                    dr_err = _distribution_recipient_error(fm, category)
                    if dr_err:
                        raise ValueError(dr_err)
                    family_portal_wallet_service.family_wallet_distribute(
                        group=primary,
                        to_user=fm.user,
                        amount=amt,
                        performed_by=request.user,
                        category=category,
                    )
                    payout_notice.append((fm.user, amt))
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        for recv, amt in payout_notice:
            _notify_wallet_recipient(
                recv,
                "Family wallet",
                f"You received Rs. {amt} from the family wallet.",
                "/child-portal",
            )
        master = family_portal_wallet_service.get_category_shared_wallet(
            primary, category
        )
        return Response(
            {
                "ok": True,
                "shared_balance": float(master.balance) if master else 0.0,
            }
        )

    member_id = request.data.get("member_id")
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    fm = (
        FamilyMember.objects.filter(pk=member_id, group=primary)
        .select_related("user")
        .first()
    )
    if not fm:
        return validation_error("invalid member_id", field="member_id")
    dr_err = _distribution_recipient_error(fm, category)
    if dr_err:
        return Response({"detail": dr_err}, status=400)
    try:
        from_w, to_w, _o, _i = family_portal_wallet_service.family_wallet_distribute(
            group=primary,
            to_user=fm.user,
            amount=amount,
            performed_by=request.user,
            category=category,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    _notify_wallet_recipient(
        fm.user,
        "Family wallet",
        f"You received Rs. {amount} from the family wallet.",
        "/child-portal",
    )
    return Response(
        {
            "ok": True,
            "shared_balance": float(from_w.balance),
            "member_balance": float(to_w.balance),
        }
    )


def _resolve_family_wallet_pk_for_transfer(group: FamilyGroup, raw):
    """Map a wallet id or legacy ``cat-<category_pk>`` placeholder to a Wallet primary key."""
    if raw in (None, ""):
        return None
    s = str(raw).strip()
    if s.startswith("cat-") and len(s) > 4:
        tail = s[4:]
        if tail.isdigit():
            cat = FamilyWalletCategory.objects.filter(
                pk=int(tail), group=group
            ).first()
            if not cat or not group.leader_id:
                return None
            w = family_portal_wallet_service.ensure_category_shared_wallet(
                group, cat, group.leader
            )
            return w.pk
        return None
    try:
        return int(s)
    except ValueError:
        return None


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_wallet_transfer(request):
    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    txn_status = (
        WalletTransaction.Status.FLAGGED
        if wallet_policy.transfer_should_auto_flag(amount)
        else WalletTransaction.Status.COMPLETED
    )
    category_id = request.data.get("category_id")
    category = None
    if category_id not in (None, ""):
        category = FamilyWalletCategory.objects.filter(
            pk=category_id, group=primary
        ).first()
        if not category:
            return validation_error("invalid category_id", field="category_id")

    from_wid_raw = request.data.get("from_wallet_id")
    to_wid_raw = request.data.get("to_wallet_id")
    if from_wid_raw not in (None, "") and to_wid_raw not in (None, ""):
        from_pk = _resolve_family_wallet_pk_for_transfer(primary, from_wid_raw)
        to_pk = _resolve_family_wallet_pk_for_transfer(primary, to_wid_raw)
        if not from_pk or not to_pk:
            return validation_error("invalid wallet id(s)", field="from_wallet_id")
        fw = Wallet.objects.filter(pk=from_pk, family_group=primary).first()
        tw = Wallet.objects.filter(pk=to_pk, family_group=primary).first()
        if not fw or not tw:
            return validation_error("invalid wallet id(s)", field="from_wallet_id")
        if fw.pk == tw.pk:
            return validation_error("cannot transfer to the same wallet", field="to_wallet_id")
        # Tagging: full role checks only when both ends are member wallets (not shared buckets).
        if category and fw.type != Wallet.Type.SHARED and tw.type != Wallet.Type.SHARED:
            fm_from = FamilyMember.objects.filter(
                group=primary, user_id=fw.owner_id
            ).first()
            fm_to = FamilyMember.objects.filter(
                group=primary, user_id=tw.owner_id
            ).first()
            if not fm_from or not fm_to:
                return Response(
                    {"detail": "Could not resolve members for category rules."},
                    status=400,
                )
            if not family_portal_wallet_service.category_allows_member_role(
                category, fm_from.role
            ):
                return Response(
                    {"detail": "This category cannot be used for the sender role."},
                    status=400,
                )
            if not family_portal_wallet_service.category_allows_member_role(
                category, fm_to.role
            ):
                return Response(
                    {"detail": "This category cannot be used for the recipient role."},
                    status=400,
                )
        wallet_policy.assert_family_transfer_wallets_allowed(fw, tw)
        wallet_policy.assert_daily_transfer_for_wallet(fw, amount)
        if wallet_policy.transfer_requires_otp(amount):
            otp_resp = _portal_consume_otp_or_error(
                request, OTPVerification.Purpose.TRANSFER
            )
            if otp_resp is not None:
                return otp_resp
        try:
            fw_o, tw_o, _o, _i = (
                family_portal_wallet_service.family_wallet_transfer_group_wallets(
                    group=primary,
                    from_wallet_id=from_pk,
                    to_wallet_id=to_pk,
                    amount=amount,
                    performed_by=request.user,
                    category=category,
                    txn_status=txn_status,
                )
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        recv_user = tw_o.owner
        if recv_user and recv_user.pk != request.user.pk:
            _notify_wallet_recipient(
                recv_user,
                "Wallet transfer",
                f"You received Rs. {amount} from {request.user.name}.",
                "/child-portal",
            )
        return Response(
            {
                "ok": True,
                "from_balance": float(fw_o.balance),
                "to_balance": float(tw_o.balance),
            }
        )

    from_mid = request.data.get("from_member_id")
    to_mid = request.data.get("to_member_id")
    fm_from = FamilyMember.objects.filter(pk=from_mid, group=primary).first()
    fm_to = FamilyMember.objects.filter(pk=to_mid, group=primary).first()
    if not fm_from or not fm_to:
        return validation_error("invalid member id(s)", field="from_member_id")
    if fm_from.pk == fm_to.pk:
        return validation_error("cannot transfer to self", field="to_member_id")
    if category:
        if not family_portal_wallet_service.category_allows_member_role(
            category, fm_from.role
        ):
            return Response(
                {"detail": "This category cannot be used for the sender role."},
                status=400,
            )
        if not family_portal_wallet_service.category_allows_member_role(
            category, fm_to.role
        ):
            return Response(
                {"detail": "This category cannot be used for the recipient role."},
                status=400,
            )
    fw0 = family_portal_wallet_service.get_member_family_wallet(
        primary, fm_from.user
    )
    tw0 = family_portal_wallet_service.get_member_family_wallet(primary, fm_to.user)
    if not fw0 or not tw0:
        return validation_error("invalid member id(s)", field="from_member_id")
    wallet_policy.assert_family_transfer_wallets_allowed(fw0, tw0)
    wallet_policy.assert_daily_transfer_for_wallet(fw0, amount)
    if wallet_policy.transfer_requires_otp(amount):
        otp_resp = _portal_consume_otp_or_error(
            request, OTPVerification.Purpose.TRANSFER
        )
        if otp_resp is not None:
            return otp_resp
    try:
        fw, tw, _o, _i = family_portal_wallet_service.family_wallet_transfer_members(
            group=primary,
            from_user=fm_from.user,
            to_user=fm_to.user,
            amount=amount,
            performed_by=request.user,
            category=category,
            txn_status=txn_status,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    _notify_wallet_recipient(
        fm_to.user,
        "Wallet transfer",
        f"You received Rs. {amount} from {fm_from.user.name}.",
        "/child-portal",
    )
    return Response(
        {
            "ok": True,
            "from_balance": float(fw.balance),
            "to_balance": float(tw.balance),
        }
    )


def _family_parent_may_withdraw_wallet(
    user: User, primary: FamilyGroup, w: Wallet
) -> bool:
    if w.family_group_id != primary.pk:
        return False
    if w.status != Wallet.Status.ACTIVE:
        return False
    if w.type == Wallet.Type.SHARED:
        return True
    if w.type == Wallet.Type.PARENT and w.owner_id == user.pk:
        return True
    return False


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def portal_family_wallet_withdrawals(request):
    from core.services.kyc_service import sync_user_kyc_status
    from core.services.kyc_withdraw import kyc_withdraw_block_payload

    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)

    if request.method == "GET":
        w_filter = request.query_params.get("wallet_id")
        qs = WalletWithdrawal.objects.filter(
            wallet__family_group=primary,
        ).select_related("wallet")
        if w_filter and str(w_filter).strip().isdigit():
            qs = qs.filter(wallet_id=int(w_filter))
        else:
            allowed_ids = list(
                Wallet.objects.filter(family_group=primary)
                .filter(
                    Q(type=Wallet.Type.SHARED)
                    | Q(type=Wallet.Type.PARENT, owner=request.user)
                )
                .values_list("pk", flat=True)
            )
            qs = qs.filter(wallet_id__in=allowed_ids)
        qs = qs.order_by("-created_at")[:200]
        return Response(
            {"results": [_serialize_withdrawal_row(x, request) for x in qs]}
        )

    sync_user_kyc_status(request.user)
    request.user.refresh_from_db()
    block = kyc_withdraw_block_payload(request.user)
    if block:
        return Response(block, status=403)
    pay_block = payout_required_block_payload(request.user)
    if pay_block:
        return Response(pay_block, status=403)

    raw_wid = request.data.get("wallet_id")
    try:
        wid = int(raw_wid)
    except (TypeError, ValueError):
        return validation_error("wallet_id required", field="wallet_id")
    w = Wallet.objects.filter(pk=wid, family_group=primary).first()
    if not w or not _family_parent_may_withdraw_wallet(request.user, primary, w):
        return validation_error("Invalid wallet for withdrawal.", field="wallet_id")
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    raw_pid = request.data.get("payout_account_id") or request.data.get("payout_account")
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return validation_error("payout_account_id required", field="payout_account_id")
    acct = PayoutAccount.objects.filter(pk=pid, user=request.user).first()
    if not acct:
        return validation_error("Invalid payout account.", field="payout_account_id")
    if wallet_policy.withdrawal_requires_otp():
        otp_resp = _portal_consume_otp_or_error(
            request, OTPVerification.Purpose.WITHDRAW
        )
        if otp_resp is not None:
            return otp_resp
    proof = request.FILES.get("proof_image") or request.FILES.get("proof")
    if proof:
        ct = (getattr(proof, "content_type", "") or "").lower()
        if not ct.startswith("image/"):
            return validation_error("Proof must be an image file.", field="proof_image")
    try:
        wd = create_pending_withdrawal(
            wallet=w,
            payout_user=request.user,
            payout_account=acct,
            amount=amount,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    if proof:
        wd.proof_image = proof
        wd.save(update_fields=["proof_image"])
    notify_family_withdrawal_submitted(wd, request.user)
    return Response(
        {
            "id": str(wd.pk),
            "withdrawal_number": wd.withdrawal_number,
            "status": wd.status,
        },
        status=201,
    )


def _family_wallet_category_create_meta_fields():
    from django.db import models as dj_models

    out = []
    for field in FamilyWalletCategory._meta.fields:
        if field.name in ("id", "group", "created_at"):
            continue
        if isinstance(field, dj_models.CharField):
            out.append(
                {
                    "name": field.name,
                    "type": "string",
                    "required": field.name == "name",
                    "max_length": field.max_length,
                }
            )
        elif isinstance(field, dj_models.PositiveIntegerField):
            dv = 0
            if field.default is not dj_models.NOT_PROVIDED:
                d = field.default
                dv = int(d() if callable(d) else d)
            out.append(
                {
                    "name": field.name,
                    "type": "integer",
                    "required": False,
                    "default": dv,
                }
            )
        elif isinstance(field, dj_models.JSONField):
            out.append(
                {
                    "name": field.name,
                    "type": "multiselect",
                    "required": False,
                    "choices": [
                        {"value": c[0], "label": c[1]} for c in FamilyMember.Role.choices
                    ],
                    "default": ["child"],
                }
            )
        elif isinstance(field, dj_models.ImageField):
            out.append(
                {
                    "name": field.name,
                    "type": "file",
                    "required": not field.blank,
                }
            )
    return out


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_wallet_categories_meta(request):
    return Response({"fields": _family_wallet_category_create_meta_fields()})


@api_view(["GET", "POST"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_wallet_categories(request):
    primary = _primary_family_group(request.user)
    if not primary:
        if request.method == "GET":
            return Response({"results": []})
        return Response({"detail": "No family group found."}, status=400)
    if request.method == "GET":
        qs = FamilyWalletCategory.objects.filter(group=primary).order_by(
            "sort_order", "name"
        )
        out = []
        ser_ctx = {"request": request}
        for cat in qs:
            w = family_portal_wallet_service.get_category_shared_wallet(primary, cat)
            row = FamilyWalletCategorySerializer(cat, context=ser_ctx).data
            row["balance"] = float(w.balance) if w else 0.0
            row["wallet_id"] = str(w.pk) if w else None
            out.append(row)
        return Response({"results": out})
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required", field="name")
    sort_order = int(request.data.get("sort_order") or 0)
    try:
        allowed_roles = _parse_allowed_member_roles(request.data)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    if FamilyWalletCategory.objects.filter(group=primary, name__iexact=name).exists():
        return Response({"detail": "A category with this name already exists."}, status=400)
    create_kw: dict = {
        "group": primary,
        "name": name[:100],
        "sort_order": sort_order,
        "allowed_member_roles": allowed_roles,
    }
    img = request.data.get("image")
    if img is not None and hasattr(img, "read"):
        create_kw["image"] = img
    cat = FamilyWalletCategory.objects.create(**create_kw)
    try:
        w = family_portal_wallet_service.create_category_wallet(
            group=primary, category=cat, leader=primary.leader
        )
    except ValueError as e:
        cat.delete()
        return Response({"detail": str(e)}, status=400)
    row = FamilyWalletCategorySerializer(cat, context={"request": request}).data
    row["balance"] = float(w.balance)
    row["wallet_id"] = str(w.pk)
    return Response(row, status=201)


def _family_wallet_txn_flow_signed(t: WalletTransaction) -> tuple[str, float]:
    w_id = t.wallet_id
    typ = t.type
    raw = float(t.amount)
    amt = abs(raw)
    if typ in (
        WalletTransaction.Type.CREDIT,
        WalletTransaction.Type.TOPUP,
        WalletTransaction.Type.BONUS,
        WalletTransaction.Type.REFUND_CREDIT,
    ):
        return "in", amt
    if typ in (
        WalletTransaction.Type.DEBIT,
        WalletTransaction.Type.PURCHASE,
        WalletTransaction.Type.WITHDRAWAL,
    ):
        return "out", -amt
    if typ == WalletTransaction.Type.TRANSFER:
        if t.from_wallet_id == w_id:
            return "out", -amt
        if t.to_wallet_id == w_id:
            return "in", amt
    return "out", -amt


def _family_wallet_display_label(wallet: Wallet) -> str:
    if wallet.type == Wallet.Type.SHARED:
        if wallet.family_category_id and getattr(wallet, "family_category", None):
            return wallet.family_category.name
        return (wallet.label or "").strip() or "Family wallet"
    if wallet.owner_id:
        return (wallet.label or "").strip() or "Member wallet"
    return (wallet.label or "").strip() or "Wallet"


def _wallet_txn_status_ui(st: str) -> str:
    if st == WalletTransaction.Status.COMPLETED:
        return "completed"
    if st == WalletTransaction.Status.FAILED:
        return "failed"
    return "pending"


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_transactions(request):
    u = request.user
    group = _primary_family_group(u)
    if not group:
        return Response({"count": 0, "next": None, "previous": None, "results": []})
    wallet_ids = list(
        Wallet.objects.filter(family_group=group).values_list("id", flat=True)
    )
    qs = WalletTransaction.objects.filter(wallet_id__in=wallet_ids).select_related(
        "wallet",
        "wallet__owner",
        "wallet__family_category",
        "family_wallet_category",
    )
    mid = request.query_params.get("member_id")
    if mid is not None and str(mid).strip() != "":
        try:
            mid_int = int(mid)
        except ValueError:
            mid_int = None
        if mid_int is not None:
            fm = (
                FamilyMember.objects.filter(pk=mid_int, group=group)
                .select_related("user")
                .first()
            )
            if fm:
                mw = family_portal_wallet_service.get_member_family_wallet(
                    group, fm.user
                )
                if mw:
                    qs = qs.filter(wallet_id=mw.pk)
                else:
                    qs = qs.none()
            else:
                qs = qs.none()
    qs = qs.order_by("-created_at")
    paginator, page = _paginate(request, qs)
    rows = []
    for t in page:
        cat = t.family_wallet_category
        w = t.wallet
        if cat is None and w and getattr(w, "family_category_id", None):
            cat = w.family_category
        flow, signed_amount = _family_wallet_txn_flow_signed(t)
        member_label = "—"
        if w and w.owner_id:
            member_label = w.owner.name or "—"
        rows.append(
            {
                "id": t.txn_id,
                "member": member_label,
                "type": t.type,
                "item": t.description or "",
                "amount": abs(float(t.amount)),
                "signed_amount": signed_amount,
                "flow": flow,
                "date": t.created_at.date().isoformat(),
                "time": t.created_at.strftime("%H:%M"),
                "status": _wallet_txn_status_ui(t.status),
                "wallet": _family_wallet_display_label(w) if w else "Wallet",
                "reference_type": t.reference_type or "",
                "category_id": str(cat.pk) if cat else None,
                "category_name": cat.name if cat else None,
                "fund_source": (t.fund_source or "").strip()
                or (_fund_source_label_for_wallet(w) if w else ""),
            }
        )
    return paginator.get_paginated_response(rows)


def _child_transaction_row(t: WalletTransaction, w: Wallet) -> dict:
    ref = (t.reference_type or "").strip()
    signed = signed_amount_for_wallet_transaction(t, w)
    if ref == "family_distribute" and signed > 0:
        typ = "parent"
        wallet_lbl = "Parent"
        item = "From family wallet"
    elif ref == "child_peer_transfer":
        typ = "peer"
        wallet_lbl = "Sibling"
        item = "Received from sibling" if signed > 0 else "Sent to sibling"
    elif t.type == WalletTransaction.Type.TOPUP:
        typ = "self"
        wallet_lbl = "Self"
        item = t.description or "Wallet top-up"
    elif t.type == WalletTransaction.Type.WITHDRAWAL:
        typ = "self"
        wallet_lbl = "Self"
        item = t.description or "Withdrawal"
    elif t.type == WalletTransaction.Type.PURCHASE:
        typ = "self"
        wallet_lbl = "Self"
        item = t.description or "Purchase"
    else:
        typ = "self"
        wallet_lbl = "Self"
        item = t.description or t.type
    fs = (t.fund_source or "").strip() or _fund_source_label_for_wallet(w)
    return {
        "id": t.txn_id,
        "item": item,
        "amount": signed,
        "status": t.status,
        "time": t.created_at.strftime("%H:%M"),
        "created_at": t.created_at.isoformat(),
        "date": t.created_at.date().isoformat(),
        "type": typ,
        "wallet": wallet_lbl,
        "reference_type": ref,
        "fund_source": fs,
    }


def _child_portal_wallet(user):
    fm = (
        FamilyMember.objects.filter(user=user, role=FamilyMember.Role.CHILD)
        .select_related("group")
        .first()
    )
    if not fm and user.role != User.Role.CHILD:
        return None, None, None
    group = fm.group if fm else None
    w = _wallet_for_user(user, wallet_type=Wallet.Type.CHILD, family_group=group) or _wallet_for_user(
        user, family_group=group
    )
    return fm, group, w


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_summary(request):
    u = request.user
    fm, group, w = _child_portal_wallet(u)
    if not fm and u.role != User.Role.CHILD:
        return Response({"detail": "Child profile not found."}, status=404)
    bal = _wallet_balance(u, family_group=group)
    parent_loaded = 0.0
    if group:
        sw = family_portal_wallet_service.get_default_shared_wallet(group)
        if sw:
            parent_loaded = float(sw.balance)
    limit_m = float(fm.spending_limit_monthly) if fm else 0.0
    spent_windows = (
        child_non_personal_spent_windows(u)
        if (fm or u.role == User.Role.CHILD)
        else None
    )
    spent_this_month = (
        float(spent_windows["monthly"]) if spent_windows is not None else 0.0
    )
    pw = personal_wallet_qs(u).first()
    personal_bal = float(pw.balance) if pw else 0.0
    return Response(
        {
            "parentLoaded": parent_loaded,
            "selfLoaded": bal,
            "personalBalance": personal_bal,
            "totalBalance": bal + parent_loaded + personal_bal,
            "spendingLimit": limit_m,
            "spentThisMonth": spent_this_month,
            "spentThisWeek": (
                float(spent_windows["weekly"]) if spent_windows is not None else 0.0
            ),
            "spentToday": (
                float(spent_windows["daily"]) if spent_windows is not None else 0.0
            ),
            "group_name": group.name if group else "",
        }
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_transactions(request):
    u = request.user
    fm, group, w = _child_portal_wallet(u)
    if not fm and u.role != User.Role.CHILD:
        return Response({"detail": "Child profile not found."}, status=404)
    if not w:
        return Response({"count": 0, "next": None, "previous": None, "results": []})
    qs = WalletTransaction.objects.filter(wallet=w).order_by("-created_at")
    paginator, page = _paginate(request, qs)
    rows = [_child_transaction_row(t, w) for t in page]
    return paginator.get_paginated_response(rows)


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_peer_members(request):
    u = request.user
    fm = (
        FamilyMember.objects.filter(
            user=u,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .select_related("group")
        .first()
    )
    if not fm:
        return Response([])
    qs = (
        FamilyMember.objects.filter(
            group=fm.group,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .exclude(pk=fm.pk)
        .select_related("user")
    )
    return Response(
        [
            {
                "id": str(m.pk),
                "name": m.user.name,
                "phone": m.user.phone,
            }
            for m in qs
        ]
    )


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_wallet_peer_transfer(request):
    u = request.user
    fm = (
        FamilyMember.objects.filter(
            user=u,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .select_related("group")
        .first()
    )
    if not fm:
        return Response({"detail": "Child membership not found."}, status=400)
    group = fm.group
    perm, _ = FamilyGroupPermission.objects.get_or_create(group=group)
    if not perm.allow_peer_transfers:
        return Response(
            {"detail": "Peer transfers are not enabled for your group."},
            status=403,
        )
    to_mid = request.data.get("to_member_id")
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    category_id = request.data.get("category_id")
    category = None
    if category_id not in (None, ""):
        category = FamilyWalletCategory.objects.filter(
            pk=category_id, group=group
        ).first()
        if not category:
            return validation_error("invalid category_id", field="category_id")
    fm_to = FamilyMember.objects.filter(
        pk=to_mid,
        group=group,
        role=FamilyMember.Role.CHILD,
        status=FamilyMember.Status.ACTIVE,
    ).select_related("user").first()
    if not fm_to:
        return validation_error("invalid to_member_id", field="to_member_id")
    if fm_to.pk == fm.pk:
        return validation_error("cannot transfer to self", field="to_member_id")
    if category:
        if not family_portal_wallet_service.category_allows_member_role(
            category, fm.role
        ) or not family_portal_wallet_service.category_allows_member_role(
            category, fm_to.role
        ):
            return Response(
                {"detail": "This category cannot be used for this transfer."},
                status=400,
            )
    try:
        fw, tw, _o, _i = family_portal_wallet_service.family_wallet_transfer_members(
            group=group,
            from_user=u,
            to_user=fm_to.user,
            amount=amount,
            performed_by=u,
            category=category,
            reference_type="child_peer_transfer",
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    _notify_wallet_recipient(
        fm_to.user,
        "Wallet transfer",
        f"You received Rs. {amount} from {u.name}.",
        "/child-portal",
    )
    return Response(
        {
            "ok": True,
            "from_balance": float(fw.balance),
            "to_balance": float(tw.balance),
        }
    )


def _resolve_active_child_wallet_for_mutation(user: User):
    fm = (
        FamilyMember.objects.filter(
            user=user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .select_related("group")
        .first()
    )
    if not fm:
        return None, Response({"detail": "Child membership not found."}, status=400)
    w = family_portal_wallet_service.get_member_family_wallet(fm.group, user)
    if not w:
        return None, Response({"detail": "Child wallet not found."}, status=400)
    if w.status != Wallet.Status.ACTIVE:
        return None, Response({"detail": "Wallet is frozen."}, status=400)
    return (fm, w), None


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_wallet_topup(request):
    pair, err = _resolve_active_child_wallet_for_mutation(request.user)
    if err:
        return err
    _, w = pair
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    method = (request.data.get("method") or "esewa").strip()[:50]
    method_norm = method.lower()
    if method_norm not in ("esewa", "khalti"):
        return validation_error(
            "Only eSewa and Khalti are supported for adding money to your wallet.",
            field="method",
        )
    raw_return = (request.data.get("return_path") or "/child-portal/topup").strip()
    return_path = raw_return[:500] if raw_return.startswith("/") else f"/{raw_return[:499].lstrip('/')}"
    try:
        wgt.assert_can_topup_wallet(payer=request.user, wallet=w, target=wgt.TOPUP_TARGET_CHILD)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    if method_norm == "esewa":
        return Response(
            wgt.build_esewa_initiate_response(
                request=request,
                payer=request.user,
                wallet=w,
                amount=amount,
                method=method,
                topup_target=wgt.TOPUP_TARGET_CHILD,
                return_path=return_path,
                return_query_esewa=None,
                success_reverse_name="portal-wallet-topup-esewa-success",
                failure_reverse_name="portal-wallet-topup-esewa-failure",
            )
        )
    try:
        return Response(
            wgt.build_khalti_initiate_response(
                payer=request.user,
                wallet=w,
                amount=amount,
                method=method,
                topup_target=wgt.TOPUP_TARGET_CHILD,
                return_path=return_path,
                return_query_esewa=None,
                purchase_order_id=f"KP-C-{uuid4().hex[:24]}",
                purchase_order_name="Child wallet top-up",
            )
        )
    except ValueError as e:
        return validation_error(str(e), field="amount")
    except KhaltiConfigError as e:
        return Response({"detail": str(e)}, status=503)
    except KhaltiApiError as e:
        return Response(
            {"detail": str(e), "khalti_error": (e.body or "")[:2000]},
            status=502,
        )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_wallet_withdrawals_list(request):
    pair, err = _resolve_active_child_wallet_for_mutation(request.user)
    if err:
        return err
    _, w = pair
    qs = WalletWithdrawal.objects.filter(wallet=w).order_by("-created_at")[:200]
    return Response({"results": [_serialize_withdrawal_row(x, request) for x in qs]})


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_wallet_withdraw(request):
    from core.services.kyc_service import sync_user_kyc_status
    from core.services.kyc_withdraw import kyc_withdraw_block_payload

    sync_user_kyc_status(request.user)
    request.user.refresh_from_db()
    block = kyc_withdraw_block_payload(request.user)
    if block:
        return Response(block, status=403)
    pay_block = payout_required_block_payload(request.user)
    if pay_block:
        return Response(pay_block, status=403)

    pair, err = _resolve_active_child_wallet_for_mutation(request.user)
    if err:
        return err
    fm, w = pair
    perm, _ = FamilyGroupPermission.objects.get_or_create(group=fm.group)
    if not perm.allow_cash_withdrawal:
        return Response(
            {"detail": "Cash withdrawal is not enabled for your family group."},
            status=403,
        )
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    if wallet_policy.withdrawal_requires_otp():
        otp_resp = _portal_consume_otp_or_error(
            request, OTPVerification.Purpose.WITHDRAW
        )
        if otp_resp is not None:
            return otp_resp
    raw_pid = request.data.get("payout_account_id") or request.data.get("payout_account")
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return validation_error("payout_account_id required", field="payout_account_id")
    acct = PayoutAccount.objects.filter(pk=pid, user=request.user).first()
    if not acct:
        return validation_error("Invalid payout account.", field="payout_account_id")
    try:
        wd = create_pending_withdrawal(
            wallet=w,
            payout_user=request.user,
            payout_account=acct,
            amount=amount,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(
        {
            "id": str(wd.pk),
            "withdrawal_number": wd.withdrawal_number,
            "status": wd.status,
        },
        status=201,
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalChild])
def portal_child_rules(request):
    u = request.user
    fm = (
        FamilyMember.objects.filter(
            user=u,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        .select_related("group")
        .first()
    )
    if not fm and u.role != User.Role.CHILD:
        return Response({"detail": "Child profile not found."}, status=404)
    if not fm:
        return Response(
            {
                "group_permissions": None,
                "member_limits": None,
                "member_spent": None,
                "product_restrictions": [],
                "auto_approval_rules": [],
                "approved_purchase_product_ids": [],
            }
        )
    group = fm.group
    perm, _ = FamilyGroupPermission.objects.get_or_create(group=group)
    restrictions = family_product_restriction_service.list_group_level_restrictions(
        group=group
    )
    rules_qs = (
        AutoApprovalRule.objects.filter(group=group)
        .select_related("category")
        .order_by("name", "id")
    )
    approved_product_ids = sorted(
        set(
            PurchaseApprovalRequest.objects.filter(
                child=u,
                status=PurchaseApprovalRequest.Status.APPROVED,
                consumed_at__isnull=True,
            ).values_list("product_id", flat=True)
        )
    )
    spent_track = child_non_personal_spent_windows(u)
    return Response(
        {
            "group_permissions": {
                "allow_online_purchases": perm.allow_online_purchases,
                "allow_peer_transfers": perm.allow_peer_transfers,
                "allow_cash_withdrawal": perm.allow_cash_withdrawal,
                "category_restrictions": perm.category_restrictions,
                "time_based_restrictions": perm.time_based_restrictions,
                "daily_spending_limit": float(perm.daily_spending_limit or 0),
            },
            "member_limits": {
                "spending_limit_daily": float(fm.spending_limit_daily or 0),
                "spending_limit_weekly": float(fm.spending_limit_weekly or 0),
                "spending_limit_monthly": float(fm.spending_limit_monthly or 0),
            },
            "member_spent": {
                "daily": float(spent_track["daily"]),
                "weekly": float(spent_track["weekly"]),
                "monthly": float(spent_track["monthly"]),
            },
            "product_restrictions": PortalProductRestrictionReadSerializer(
                restrictions, many=True
            ).data,
            "auto_approval_rules": PortalAutoApprovalRuleReadSerializer(
                rules_qs, many=True
            ).data,
            "approved_purchase_product_ids": approved_product_ids,
        }
    )


def _primary_family_membership(user: User):
    """
    Active FamilyMember row for self-profile family_group_* fields.

    For non-child users, prefer the same group as family portal APIs
    (_primary_family_group) so overview matches profile.
    """
    if user.role == User.Role.CHILD:
        return (
            FamilyMember.objects.filter(user=user, status=FamilyMember.Status.ACTIVE)
            .select_related("group")
            .order_by("id")
            .first()
        )
    primary = _primary_family_group(user)
    if primary:
        fm = (
            FamilyMember.objects.filter(
                user=user,
                group=primary,
                status=FamilyMember.Status.ACTIVE,
            )
            .select_related("group")
            .first()
        )
        if fm:
            return fm
    return (
        FamilyMember.objects.filter(user=user, status=FamilyMember.Status.ACTIVE)
        .select_related("group")
        .order_by("id")
        .first()
    )


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_change_password(request):
    old_p = request.data.get("old_password") or ""
    new_p = request.data.get("new_password") or ""
    if not new_p or len(new_p) < 6:
        return validation_error("new_password must be at least 6 characters", field="new_password")
    u = request.user
    if not u.check_password(old_p):
        return Response({"detail": "Current password is incorrect."}, status=400)
    u.set_password(new_p)
    u.save(update_fields=["password"])
    return Response({"ok": True})


def _portal_self_profile_get(request, u: User) -> dict:
    role = u.role
    base = {
        "portal_role": role,
        "id": str(u.pk),
        "name": u.name,
        "phone": u.phone,
        "email": u.email or "",
        "address": u.address or "",
        "avatar_url": user_public_avatar_url(request, u),
        "username": u.username,
        "kid": u.KID,
        "kyc_status": u.kyc_status,
    }
    if role == User.Role.NORMAL:
        orders_count = Order.objects.filter(customer=u).count()
        fav_count = ReelInteraction.objects.filter(
            user=u, type=ReelInteraction.Type.BOOKMARK
        ).count()
        review_count = ProductReview.objects.filter(
            customer=u,
            status=ProductReview.Status.APPROVED,
        ).count()
        base.update(
            {
                "store_name": u.name,
                "description": u.profile_description or "",
                "contact_email": u.email or "",
                "logo_url": user_public_avatar_url(request, u),
                "banner_url": absolute_media_url(request, u.profile_cover),
                "rating": 0.0,
                "is_verified": u.kyc_status == User.KYCStatus.VERIFIED,
                "product_count": orders_count,
                "review_count": review_count,
                "favourite_reels_count": fav_count,
            }
        )
    fm = _primary_family_membership(u)
    if fm:
        base["family_group_id"] = str(fm.group_id)
        base["family_group_name"] = fm.group.name if fm.group_id else ""
        base["family_member_role"] = fm.role
        base["spending_limit_monthly"] = float(fm.spending_limit_monthly or 0)
    else:
        base["family_group_id"] = ""
        base["family_group_name"] = ""
        base["family_member_role"] = ""
        base["spending_limit_monthly"] = 0.0
    return base


def _portal_self_profile_patch(request, u: User) -> Response | None:
    """Apply PATCH fields; return Response on error, else None."""
    role = u.role
    if "name" in request.data or (role == User.Role.NORMAL and (
        "store_name" in request.data
    )):
        name = (request.data.get("store_name") or request.data.get("name") or "").strip()[:150]
        if name:
            u.name = name
    if role == User.Role.NORMAL and "description" in request.data:
        u.profile_description = request.data.get("description") or ""
    if "contact_email" in request.data or "email" in request.data:
        u.email = (
            (request.data.get("contact_email") or request.data.get("email") or "").strip()[:254]
        )
    if "phone" in request.data:
        phone = (request.data.get("phone") or "").strip()[:15]
        if phone and phone != u.phone:
            if User.objects.filter(phone=phone).exclude(pk=u.pk).exists():
                return validation_error("Phone already in use.", field="phone")
            u.phone = phone
    if "address" in request.data:
        u.address = request.data.get("address") or ""
    if request.FILES.get("logo") or request.FILES.get("avatar"):
        f = request.FILES.get("logo") or request.FILES.get("avatar")
        u.avatar = f
    if role == User.Role.NORMAL and (
        request.FILES.get("banner") or request.FILES.get("profile_cover")
    ):
        f = request.FILES.get("banner") or request.FILES.get("profile_cover")
        u.profile_cover = f
    u.save()
    return None


@api_view(["GET", "PATCH"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_self_profile(request):
    u = request.user
    if request.method == "GET":
        return Response(_portal_self_profile_get(request, u))
    err = _portal_self_profile_patch(request, u)
    if err is not None:
        return err
    u.refresh_from_db()
    out = _portal_self_profile_get(request, u)
    if u.role == User.Role.NORMAL:
        out.update(
            {
                "store_name": u.name,
                "logo_url": user_public_avatar_url(request, u),
                "banner_url": absolute_media_url(request, u.profile_cover),
            }
        )
    return Response(out)


# --- Customer profile (store-style) + favourite reels ---


@api_view(["GET", "PATCH"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalCustomer])
def portal_customer_profile(request):
    u = request.user
    if request.method == "GET":
        orders_count = Order.objects.filter(customer=u).count()
        fav_count = ReelInteraction.objects.filter(
            user=u, type=ReelInteraction.Type.BOOKMARK
        ).count()
        review_count = ProductReview.objects.filter(
            customer=u,
            status=ProductReview.Status.APPROVED,
        ).count()
        return Response(
            {
                "id": str(u.pk),
                "store_name": u.name,
                "description": u.profile_description or "",
                "contact_email": u.email or "",
                "phone": u.phone,
                "address": u.address or "",
                "logo_url": user_public_avatar_url(request, u),
                "banner_url": absolute_media_url(request, u.profile_cover),
                "username": u.username,
                "kid": u.KID,
                "kyc_status": u.kyc_status,
                "rating": 0.0,
                "is_verified": u.kyc_status == User.KYCStatus.VERIFIED,
                "product_count": orders_count,
                "review_count": review_count,
                "favourite_reels_count": fav_count,
            }
        )
    if "store_name" in request.data or "name" in request.data:
        name = (request.data.get("store_name") or request.data.get("name") or "").strip()[:150]
        if name:
            u.name = name
    if "description" in request.data:
        u.profile_description = request.data.get("description") or ""
    if "contact_email" in request.data or "email" in request.data:
        u.email = (
            (request.data.get("contact_email") or request.data.get("email") or "").strip()[:254]
        )
    if "phone" in request.data:
        phone = (request.data.get("phone") or "").strip()[:15]
        if phone and phone != u.phone:
            if User.objects.filter(phone=phone).exclude(pk=u.pk).exists():
                return validation_error("Phone already in use.", field="phone")
            u.phone = phone
    if "address" in request.data:
        u.address = request.data.get("address") or ""
    if request.FILES.get("logo") or request.FILES.get("avatar"):
        f = request.FILES.get("logo") or request.FILES.get("avatar")
        u.avatar = f
    if request.FILES.get("banner") or request.FILES.get("profile_cover"):
        f = request.FILES.get("banner") or request.FILES.get("profile_cover")
        u.profile_cover = f
    u.save()
    return Response(
        {
            "id": str(u.pk),
            "store_name": u.name,
            "logo_url": user_public_avatar_url(request, u),
            "banner_url": absolute_media_url(request, u.profile_cover),
        }
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_reels_favourites(request):
    qs = (
        Reel.objects.filter(
            interactions__user=request.user,
            interactions__type=ReelInteraction.Type.BOOKMARK,
            status__in=[Reel.Status.ACTIVE, Reel.Status.APPROVED],
        )
        .select_related("product", "product__category", "vendor")
        .annotate(comments_count=Count("comments", distinct=True))
        .order_by("-created_at")
        .distinct()
    )
    qs = annotate_reels_comments(qs)
    paginator, page = _paginate(request, qs)
    data = ReelPublicSerializer(page, many=True, context={"request": request}).data
    return paginator.get_paginated_response(data)


# --- Wallet (personal) ---


def _portal_wallet_otp_error_response(message: str):
    return Response({"code": "otp_required", "detail": message}, status=400)


def _portal_consume_otp_or_error(request, purpose: str) -> Response | None:
    code = (request.data.get("otp") or "").strip()
    if not code:
        return _portal_wallet_otp_error_response("OTP is required for this action.")
    phone = (request.user.phone or "").strip()
    if not phone:
        return Response(
            {"detail": "Your account has no phone number on file for OTP verification."},
            status=400,
        )
    try:
        otp_service.consume(phone, purpose, code)
    except otp_service.OTPError as e:
        return Response({"detail": str(e)}, status=400)
    return None


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalWalletOtpUser])
def portal_wallet_otp_for_transfer(request):
    phone = (request.user.phone or "").strip()
    if not phone:
        return Response({"detail": "No phone number on file."}, status=400)
    otp_service.create_otp(phone, OTPVerification.Purpose.TRANSFER)
    return Response({"ok": True})


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalWalletOtpUser])
def portal_wallet_otp_for_withdraw(request):
    phone = (request.user.phone or "").strip()
    if not phone:
        return Response({"detail": "No phone number on file."}, status=400)
    otp_service.create_otp(phone, OTPVerification.Purpose.WITHDRAW)
    return Response({"ok": True})


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_wallet_public_settings(request):
    return Response(wallet_policy.public_settings_snapshot())


def _ensure_active_personal_wallet(user):
    w = get_or_create_personal_wallet(user)
    if w.status != Wallet.Status.ACTIVE:
        return None, validation_error("Wallet is frozen.", field="wallet")
    return w, None


def _active_family_group_ids_for_user(user):
    led = FamilyGroup.objects.filter(
        leader=user, status=FamilyGroup.Status.ACTIVE
    ).values_list("id", flat=True)
    member = FamilyMember.objects.filter(
        user=user, status=FamilyMember.Status.ACTIVE
    ).values_list("group_id", flat=True)
    return set(led) | set(member)


def _wallet_transfer_allowed_recipient_user_ids(user: User) -> set[int]:
    group_ids = _active_family_group_ids_for_user(user)
    if not group_ids:
        return set()
    return set(
        FamilyMember.objects.filter(
            group_id__in=group_ids, status=FamilyMember.Status.ACTIVE
        )
        .exclude(user=user)
        .values_list("user_id", flat=True)
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalCustomer])
def portal_wallet_transfer_recipients(request):
    """Active family co-members only (same groups as the current user). Optional `q` filters the list."""
    u = request.user
    group_ids = _active_family_group_ids_for_user(u)
    seen: set[int] = set()
    rows: list[dict] = []

    if group_ids:
        mids = (
            FamilyMember.objects.filter(
                group_id__in=group_ids, status=FamilyMember.Status.ACTIVE
            )
            .exclude(user=u)
            .select_related("user")
            .order_by("user__name", "user_id")
        )
        for m in mids:
            if m.user_id in seen:
                continue
            seen.add(m.user_id)
            cu = m.user
            rows.append(
                {
                    "user_id": cu.pk,
                    "name": cu.name,
                    "phone": cu.phone,
                    "username": cu.username,
                    "kid": cu.KID,
                }
            )

    q = (request.query_params.get("q") or "").strip()
    if len(q) >= 1:
        ql = q.lower()
        rows = [
            r
            for r in rows
            if ql in (r["name"] or "").lower()
            or ql in (r["phone"] or "")
            or ql in (r["username"] or "").lower()
            or ql in (r["kid"] or "").lower()
        ]

    return Response({"results": rows})


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalCustomer])
def portal_wallet_topup(request):
    w, err = _ensure_active_personal_wallet(request.user)
    if err:
        return err
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    method = (request.data.get("method") or "topup").strip()[:50]
    method_norm = method.lower()
    raw_return = (request.data.get("return_path") or "/portal/wallet").strip()
    return_path = raw_return[:500] if raw_return.startswith("/") else f"/{raw_return[:499].lstrip('/')}"
    if method_norm == "esewa":
        return Response(
            wgt.build_esewa_initiate_response(
                request=request,
                payer=request.user,
                wallet=w,
                amount=amount,
                method=method,
                topup_target=wgt.TOPUP_TARGET_CUSTOMER,
                return_path=return_path,
                return_query_esewa=None,
                success_reverse_name="portal-wallet-topup-esewa-success",
                failure_reverse_name="portal-wallet-topup-esewa-failure",
            )
        )
    if method_norm == "khalti":
        try:
            return Response(
                wgt.build_khalti_initiate_response(
                    payer=request.user,
                    wallet=w,
                    amount=amount,
                    method=method,
                    topup_target=wgt.TOPUP_TARGET_CUSTOMER,
                    return_path=return_path,
                    return_query_esewa=None,
                    purchase_order_id=f"KP-W-{uuid4().hex[:24]}",
                    purchase_order_name="Wallet top-up",
                )
            )
        except ValueError as e:
            return validation_error(str(e), field="amount")
        except KhaltiConfigError as e:
            return Response({"detail": str(e)}, status=503)
        except KhaltiApiError as e:
            return Response(
                {"detail": str(e), "khalti_error": (e.body or "")[:2000]},
                status=502,
            )
    try:
        with transaction.atomic():
            wt = wallet_service.credit_wallet(
                w,
                amount,
                wtype=WalletTransaction.Type.TOPUP,
                description=f"Wallet top-up ({method})",
                performed_by=request.user,
            )
            wallet_service.apply_topup_bonus_after_credit(
                w,
                amount,
                bonus_reference_id=wt.txn_id,
                performed_by=request.user,
            )
    except Exception as e:
        return Response({"detail": str(e)}, status=400)
    w.refresh_from_db()
    return Response({"ok": True, "balance": float(w.balance)})


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def portal_wallet_topup_esewa_success(request):
    return HttpResponseRedirect(wgt.handle_esewa_wallet_topup_success(request))


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def portal_wallet_topup_esewa_failure(request):
    return HttpResponseRedirect(wgt.handle_esewa_wallet_topup_failure(request))


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated])
def portal_wallet_topup_khalti_verify(request):
    """Confirm Khalti payment via lookup (idempotent). Call from SPA after return_url redirect."""
    pidx = (request.query_params.get("pidx") or "").strip()
    if not pidx:
        return validation_error("pidx is required", field="pidx")
    body, status = wgt.khalti_wallet_topup_verify_payload(user=request.user, pidx=pidx)
    return Response(body, status=status)


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalCustomer])
def portal_wallet_transfer(request):
    from_w, err = _ensure_active_personal_wallet(request.user)
    if err:
        return err
    raw_to = (
        (request.data.get("recipient") or request.data.get("recipient_id") or request.data.get("to") or "")
        .strip()
    )
    if not raw_to:
        return validation_error("recipient required", field="recipient")
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")

    recipient = None
    if raw_to.isdigit():
        recipient = User.objects.filter(pk=int(raw_to)).first()
    if not recipient and raw_to.upper().startswith("KP") and len(raw_to) <= 12:
        recipient = User.objects.filter(KID__iexact=raw_to.upper()).first()
    if not recipient:
        handle = raw_to.lstrip("@").strip()
        recipient = User.objects.filter(username__iexact=handle).first()
    if not recipient or recipient.pk == request.user.pk:
        return validation_error("Recipient not found.", field="recipient")
    if recipient.pk not in _wallet_transfer_allowed_recipient_user_ids(request.user):
        return validation_error(
            "Recipient is not an allowed transfer target.",
            field="recipient",
        )

    to_w = get_or_create_personal_wallet(recipient)
    if to_w.status != Wallet.Status.ACTIVE:
        return validation_error("Recipient wallet is not active.", field="recipient")

    try:
        wallet_policy.assert_wallet_type_enabled_for_wallet(from_w)
        wallet_policy.assert_wallet_type_enabled_for_wallet(to_w)
        wallet_policy.assert_peer_transfer_individual_allowed(from_w, to_w)
        fee = wallet_policy.compute_peer_transfer_fee(amount)
        wallet_policy.assert_may_credit_wallet(to_w, amount)
        wallet_policy.assert_daily_transfer_limit(request.user, amount)
        if wallet_policy.transfer_requires_otp(amount):
            otp_resp = _portal_consume_otp_or_error(
                request, OTPVerification.Purpose.TRANSFER
            )
            if otp_resp is not None:
                return otp_resp
        txn_status = (
            WalletTransaction.Status.FLAGGED
            if wallet_policy.transfer_should_auto_flag(amount)
            else WalletTransaction.Status.COMPLETED
        )
        wallet_service.execute_transfer(
            from_w,
            to_w,
            amount,
            performed_by=request.user,
            platform_fee=fee,
            txn_status=txn_status,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    from_w.refresh_from_db()
    return Response({"ok": True, "balance": float(from_w.balance)})


def _serialize_payout_account(request, row: PayoutAccount) -> dict:
    return {
        "id": str(row.pk),
        "type": row.type,
        "phone": row.phone or "",
        "bank_account_no": row.bank_account_no or "",
        "bank_account_holder": row.bank_account_holder or "",
        "bank_name": row.bank_name or "",
        "qr_image_url": absolute_media_url(request, row.qr_image) if row.qr_image else "",
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _serialize_withdrawal_row(w: WalletWithdrawal, request=None) -> dict:
    proof_url = ""
    if request is not None and getattr(w, "proof_image", None) and w.proof_image:
        proof_url = absolute_media_url(request, w.proof_image)
    return {
        "id": str(w.pk),
        "withdrawal_number": w.withdrawal_number,
        "amount": float(w.amount),
        "method": w.method,
        "method_account": w.method_account,
        "status": w.status,
        "reject_reason": (w.reject_reason or "").strip(),
        "created_at": w.created_at.isoformat(),
        "processed_at": w.processed_at.isoformat() if w.processed_at else "",
        "wallet_id": str(w.wallet_id),
        "proof_image_url": proof_url,
    }


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def portal_payout_accounts_list_create(request):
    u = request.user
    if request.method == "GET":
        qs = PayoutAccount.objects.filter(user=u).order_by("-updated_at", "-id")
        return Response({"results": [_serialize_payout_account(request, x) for x in qs]})
    typ = (request.data.get("type") or "").strip()
    if typ not in dict(PayoutAccount.Type.choices):
        return validation_error("invalid type", field="type")
    row = PayoutAccount(
        user=u,
        type=typ,
        phone=(request.data.get("phone") or "").strip()[:20],
        bank_account_no=(request.data.get("bank_account_no") or "").strip()[:64],
        bank_account_holder=(request.data.get("bank_account_holder") or "").strip()[:150],
        bank_name=(request.data.get("bank_name") or "").strip()[:100],
    )
    qr = request.FILES.get("qr_image")
    if qr:
        row.qr_image = qr
    try:
        row.full_clean()
    except ValidationError as e:
        return Response(e.message_dict if hasattr(e, "message_dict") else {"detail": str(e)}, status=400)
    row.save()
    return Response(_serialize_payout_account(request, row), status=201)


@api_view(["PATCH", "DELETE"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def portal_payout_account_detail(request, pk: int):
    u = request.user
    row = PayoutAccount.objects.filter(pk=pk, user=u).first()
    if not row:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "DELETE":
        try:
            row.delete()
        except Exception:
            return Response(
                {
                    "detail": "Cannot delete this payout account (it may be linked to withdrawals).",
                },
                status=400,
            )
        return Response({"ok": True})
    if "type" in request.data:
        t = (request.data.get("type") or "").strip()
        if t in dict(PayoutAccount.Type.choices):
            row.type = t
    if "phone" in request.data:
        row.phone = (request.data.get("phone") or "").strip()[:20]
    if "bank_account_no" in request.data:
        row.bank_account_no = (request.data.get("bank_account_no") or "").strip()[:64]
    if "bank_account_holder" in request.data:
        row.bank_account_holder = (request.data.get("bank_account_holder") or "").strip()[:150]
    if "bank_name" in request.data:
        row.bank_name = (request.data.get("bank_name") or "").strip()[:100]
    qr = request.FILES.get("qr_image")
    if qr:
        row.qr_image = qr
    try:
        row.full_clean()
    except ValidationError as e:
        return Response(e.message_dict if hasattr(e, "message_dict") else {"detail": str(e)}, status=400)
    row.save()
    return Response(_serialize_payout_account(request, row))


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalCustomer])
def portal_wallet_withdrawals_list(request):
    w, err = _ensure_active_personal_wallet(request.user)
    if err:
        return err
    qs = WalletWithdrawal.objects.filter(wallet=w).order_by("-created_at")[:200]
    return Response({"results": [_serialize_withdrawal_row(x, request) for x in qs]})


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalCustomer])
def portal_wallet_withdraw(request):
    from core.services.kyc_service import sync_user_kyc_status
    from core.services.kyc_withdraw import kyc_withdraw_block_payload

    sync_user_kyc_status(request.user)
    request.user.refresh_from_db()
    block = kyc_withdraw_block_payload(request.user)
    if block:
        return Response(block, status=403)
    pay_block = payout_required_block_payload(request.user)
    if pay_block:
        return Response(pay_block, status=403)

    w, err = _ensure_active_personal_wallet(request.user)
    if err:
        return err
    amount = _to_decimal(request.data.get("amount"), "0")
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    if wallet_policy.withdrawal_requires_otp():
        otp_resp = _portal_consume_otp_or_error(
            request, OTPVerification.Purpose.WITHDRAW
        )
        if otp_resp is not None:
            return otp_resp
    raw_pid = request.data.get("payout_account_id") or request.data.get("payout_account")
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return validation_error("payout_account_id required", field="payout_account_id")
    acct = PayoutAccount.objects.filter(pk=pid, user=request.user).first()
    if not acct:
        return validation_error("Invalid payout account.", field="payout_account_id")
    try:
        wd = create_pending_withdrawal(
            wallet=w,
            payout_user=request.user,
            payout_account=acct,
            amount=amount,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(
        {
            "id": str(wd.pk),
            "withdrawal_number": wd.withdrawal_number,
            "status": wd.status,
        },
        status=201,
    )


# --- Support ---


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_support_super_admin_contact(request):
    u = support_ticket_service.primary_super_admin_user()
    if not u:
        return Response(
            {
                "name": "",
                "phone": "",
                "avatar_url": "",
                "is_online": False,
            }
        )
    sa_ids = support_ticket_service.super_admin_user_ids()
    online = online_user_ids_for(sa_ids)
    return Response(
        {
            "name": u.name or u.phone or "",
            "phone": u.phone or "",
            "avatar_url": user_public_avatar_url(request, u) or "",
            "is_online": u.pk in online,
        }
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_support_faqs(request):
    qs = FAQ.objects.filter(
        is_published=True,
        surface__in=[FAQ.Surface.CUSTOMER, FAQ.Surface.GENERAL],
    ).order_by("sort_order", "id")
    return Response(
        {
            "results": [
                {
                    "id": str(f.pk),
                    "question": f.question,
                    "answer": f.answer,
                    "surface": f.surface,
                }
                for f in qs
            ]
        }
    )


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_support_tickets(request):
    if request.method == "GET":
        qs = SupportTicket.objects.filter(submitter=request.user).order_by(
            "-last_activity_at", "-created_at"
        )
        paginator, page = _paginate(request, qs)
        ticket_ids = [t.pk for t in page]
        states = {
            rs.ticket_id: rs
            for rs in SupportTicketReaderState.objects.filter(
                reader=request.user, ticket_id__in=ticket_ids
            )
        }
        rows = []
        for t in page:
            st = states.get(t.pk)
            rows.append(
                {
                    "id": t.ticket_number,
                    "subject": t.subject,
                    "status": t.status,
                    "priority": t.priority,
                    "category": t.category,
                    "source_panel": t.source_panel,
                    "created": t.created_at.date().isoformat(),
                    "last_activity": (t.last_activity_at or t.created_at).date().isoformat(),
                    "has_unread": support_ticket_service.ticket_has_unread_for_submitter(
                        t, state=st
                    ),
                }
            )
        return paginator.get_paginated_response(rows)
    subj = (request.data.get("subject") or "").strip()
    desc = (request.data.get("description") or "").strip()
    if not subj or not desc:
        return validation_error("subject and description required")
    pr = request.data.get("priority") or SupportTicket.Priority.MEDIUM
    if pr not in dict(SupportTicket.Priority.choices):
        pr = SupportTicket.Priority.MEDIUM
    cat = (request.data.get("category") or "").strip() or SupportTicket.Category.OTHER
    if cat not in dict(SupportTicket.Category.choices):
        cat = SupportTicket.Category.OTHER
    source_panel = support_ticket_service.resolve_portal_source_panel(request.user)
    try:
        with transaction.atomic():
            t = SupportTicket.objects.create(
                ticket_number=_gen_ticket_number(),
                submitter=request.user,
                subject=subj[:255],
                description=desc,
                priority=pr,
                source_panel=source_panel,
                category=cat,
                last_activity_at=timezone.now(),
            )
            support_ticket_service.append_message(t, request.user, desc)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    support_notification_service.notify_admins_new_ticket(t)
    return Response({"id": t.ticket_number}, status=201)


def _portal_support_attachment_url(att_id: int) -> str:
    return f"/portal/support/attachments/{att_id}/"


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_support_ticket_detail(request, ticket_number):
    t = (
        SupportTicket.objects.filter(ticket_number=ticket_number, submitter=request.user)
        .prefetch_related("messages__sender", "messages__attachments")
        .first()
    )
    if not t:
        return Response({"detail": "Not found."}, status=404)
    support_ticket_service.ensure_initial_message(t)
    support_ticket_service.mark_ticket_read(t, request.user)
    _av = lambda u: user_public_avatar_url(request, u)
    sa_ids = support_ticket_service.super_admin_user_ids()
    counterpart_online = bool(online_user_ids_for(sa_ids))
    msgs = support_ticket_service.serialize_ticket_messages(
        list(t.messages.all()),
        _portal_support_attachment_url,
        sender_avatar_url_fn=_av,
        viewer_user_id=request.user.pk,
        viewer_is_staff=False,
        counterpart_online=counterpart_online,
    )
    return Response(
        {
            "id": t.ticket_number,
            "subject": t.subject,
            "description": t.description,
            "status": t.status,
            "priority": t.priority,
            "category": t.category,
            "source_panel": t.source_panel,
            "created": t.created_at.isoformat(),
            "last_activity_at": (t.last_activity_at or t.created_at).isoformat(),
            "counterpart_online": counterpart_online,
            "messages": msgs,
        }
    )


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def portal_support_ticket_messages(request, ticket_number):
    t = SupportTicket.objects.filter(
        ticket_number=ticket_number, submitter=request.user
    ).first()
    if not t:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        before = request.query_params.get("before")
        if not before:
            return Response({"detail": "Query parameter 'before' (message id) is required."}, status=400)
        try:
            before_id = int(before)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid 'before'."}, status=400)
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        sa_ids = support_ticket_service.super_admin_user_ids()
        counterpart_online = bool(online_user_ids_for(sa_ids))
        results, has_more = support_ticket_service.messages_page_before(
            t,
            before_id,
            limit,
            _portal_support_attachment_url,
            sender_avatar_url_fn=lambda u: user_public_avatar_url(request, u),
            viewer_user_id=request.user.pk,
            viewer_is_staff=False,
            counterpart_online=counterpart_online,
        )
        return Response({"results": results, "has_more": has_more})

    body, files = support_ticket_service.extract_message_body_and_files_from_request(request)
    try:
        msg = support_ticket_service.append_message(t, request.user, body, files)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    preview = body or ("sent an attachment" if files else "")
    support_notification_service.notify_admins_ticket_activity(t, preview)
    msg = (
        SupportTicketMessage.objects.filter(pk=msg.pk)
        .select_related("sender")
        .prefetch_related("attachments")
        .first()
    )
    sa_ids = support_ticket_service.super_admin_user_ids()
    counterpart_online = bool(online_user_ids_for(sa_ids))
    tick = support_ticket_service.delivery_tick_for_message(
        msg,
        viewer_user_id=request.user.pk,
        viewer_is_staff=False,
        counterpart_online=counterpart_online,
    )
    return Response(
        {
            "ok": True,
            "message": support_ticket_service.message_to_row(
                msg,
                _portal_support_attachment_url,
                sender_avatar_url_fn=lambda u: user_public_avatar_url(request, u),
                delivery_ticks=tick,
            ),
        },
        status=201,
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalSelf])
def portal_support_attachment(request, attachment_id: int):
    att = support_ticket_service.get_attachment_or_none(attachment_id)
    if not att or not support_ticket_service.user_may_access_attachment_for_submitter(
        request.user, att
    ):
        return Response({"detail": "Not found."}, status=404)
    return support_ticket_service.attachment_file_response(att)


# --- Checkout ---


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_delivery_default(request):
    """Saved default delivery location (GET) or reverse-geocode + save (POST)."""
    u = request.user
    if request.method == "GET":
        return Response(
            {
                "area_location": u.default_area_location or "",
                "landmark": u.default_landmark or "",
                "google_map_link": u.default_google_map_link or "",
                "latitude": float(u.default_latitude)
                if u.default_latitude is not None
                else None,
                "longitude": float(u.default_longitude)
                if u.default_longitude is not None
                else None,
            }
        )

    lat = request.data.get("latitude")
    lon = request.data.get("longitude")
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return Response(
            {"detail": "latitude and longitude are required as numbers."},
            status=400,
        )
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return Response({"detail": "invalid coordinates"}, status=400)

    try:
        raw = reverse_geocode(lat_f, lon_f)
        area, landmark = area_and_landmark_from_nominatim(raw)
    except NominatimError:
        return Response(
            {
                "detail": "Could not resolve this location. Try again or enter manually.",
            },
            status=502,
        )

    if not area:
        area = f"{lat_f:.5f}, {lon_f:.5f}"[:255]

    map_link = f"https://maps.google.com/?q={lat_f},{lon_f}"
    lat_dec = Decimal(str(round(lat_f, 6))).quantize(Decimal("0.000001"))
    lon_dec = Decimal(str(round(lon_f, 6))).quantize(Decimal("0.000001"))

    u.default_area_location = area[:255]
    u.default_landmark = landmark[:255]
    u.default_google_map_link = map_link[:500]
    u.default_latitude = lat_dec
    u.default_longitude = lon_dec
    u.save(
        update_fields=[
            "default_area_location",
            "default_landmark",
            "default_google_map_link",
            "default_latitude",
            "default_longitude",
            "updated_at",
        ]
    )
    return Response(
        {
            "area_location": u.default_area_location,
            "landmark": u.default_landmark,
            "google_map_link": u.default_google_map_link,
            "latitude": float(u.default_latitude),
            "longitude": float(u.default_longitude),
        }
    )


PORTAL_GATEWAY_PAYMENT_METHODS = frozenset(
    {
        Order.PaymentMethod.ESEWA,
        Order.PaymentMethod.KHALTI,
        Order.PaymentMethod.IME_PAY,
    }
)


def _fund_source_label_for_wallet(w: Wallet) -> str:
    if w.type == Wallet.Type.PERSONAL:
        return "Personal wallet"
    if w.type == Wallet.Type.CHILD:
        gname = w.family_group.name if w.family_group_id else "Family"
        return f"Child wallet — {gname}"
    if w.type == Wallet.Type.PARENT:
        gname = w.family_group.name if w.family_group_id else "Family"
        return f"Family member (parent) wallet — {gname}"
    if w.type == Wallet.Type.SHARED:
        cat = getattr(w, "family_category", None)
        if w.family_category_id and cat:
            return f"Family shared bucket — {cat.name}"
        return "Family shared wallet"
    return (w.label or "").strip() or w.get_type_display()


def _wallet_child_spending_limits_apply(w: Wallet) -> bool:
    """True when validate_child_spending_limits runs for this wallet (non-personal)."""
    return w.type != Wallet.Type.PERSONAL


def _checkout_fund_source_label_for_wallet(viewer: User, w: Wallet) -> str:
    """Checkout-only label customization; keep shared fund_source rules elsewhere."""
    base = _fund_source_label_for_wallet(w)
    if w.type != Wallet.Type.CHILD:
        return base
    if not w.family_group_id or not w.owner_id:
        return base
    child_is_active_in_group = FamilyMember.objects.filter(
        user_id=w.owner_id,
        group_id=w.family_group_id,
        role=FamilyMember.Role.CHILD,
        status=FamilyMember.Status.ACTIVE,
    ).exists()
    viewer_is_in_group = (
        w.owner_id == viewer.pk
        or FamilyMember.objects.filter(
            user=viewer,
            group_id=w.family_group_id,
            status=FamilyMember.Status.ACTIVE,
        ).exists()
    )
    if not (child_is_active_in_group and viewer_is_in_group):
        return base
    child_name = (getattr(w.owner, "name", "") or "").strip() or "Child"
    group_name = (getattr(w.family_group, "name", "") or "").strip() or "Family"
    return f"Child wallet — {child_name} ({group_name})"


def _wallet_user_may_pay_from(user: User, w: Wallet) -> bool:
    if w.status != Wallet.Status.ACTIVE:
        return False
    if w.type in (Wallet.Type.VENDOR, Wallet.Type.PLATFORM):
        return False
    if w.owner_id == user.pk:
        return True
    if w.family_group_id and FamilyMember.objects.filter(
        user=user,
        group_id=w.family_group_id,
        status=FamilyMember.Status.ACTIVE,
    ).exists():
        return True
    return False


def _payable_checkout_wallets_for_user(user: User) -> list[Wallet]:
    """Active wallets the user may charge for portal checkout (owner or family member)."""
    member_group_ids = list(
        FamilyMember.objects.filter(
            user=user, status=FamilyMember.Status.ACTIVE
        ).values_list("group_id", flat=True)
    )
    q = Q(owner=user)
    if member_group_ids:
        q |= Q(family_group_id__in=member_group_ids)
    qs = (
        Wallet.objects.filter(q)
        .filter(status=Wallet.Status.ACTIVE)
        .exclude(type__in=(Wallet.Type.VENDOR, Wallet.Type.PLATFORM))
        .select_related("owner", "family_group", "family_category")
        .distinct()
        .order_by("type", "id")
    )
    return [
        w
        for w in qs
        if _wallet_user_may_pay_from(user, w)
        and wallet_policy.wallet_payable_under_settings(w)
    ]


def _resolve_default_checkout_wallet(user: User) -> Wallet | None:
    def _first_payable(candidates: list[Wallet]) -> Wallet | None:
        for c in candidates:
            if c and wallet_policy.wallet_payable_under_settings(c):
                return c
        return None

    if user.role == User.Role.NORMAL:
        w = get_or_create_personal_wallet(user)
        if wallet_policy.wallet_payable_under_settings(w):
            return w
        return _first_payable(_payable_checkout_wallets_for_user(user))

    if user.role == User.Role.CHILD:
        fm = (
            FamilyMember.objects.filter(
                user=user,
                role=FamilyMember.Role.CHILD,
                status=FamilyMember.Status.ACTIVE,
            )
            .select_related("group")
            .first()
        )
        if fm and fm.group_id:
            mw = family_portal_wallet_service.get_member_family_wallet(fm.group, user)
            if mw and wallet_policy.wallet_payable_under_settings(mw):
                return mw
        w = get_or_create_personal_wallet(user)
        if wallet_policy.wallet_payable_under_settings(w):
            return w
        return _first_payable(_payable_checkout_wallets_for_user(user))

    if user.role == User.Role.PARENT:
        if user_has_family_portal_access(user):
            primary = _primary_family_group(user)
            if primary:
                mw = family_portal_wallet_service.get_member_family_wallet(
                    primary, user
                )
                if mw and wallet_policy.wallet_payable_under_settings(mw):
                    return mw
        w = get_or_create_personal_wallet(user)
        if wallet_policy.wallet_payable_under_settings(w):
            return w
        return _first_payable(_payable_checkout_wallets_for_user(user))
    return None


def _resolve_checkout_wallet(request) -> tuple[Wallet, str]:
    u = request.user
    raw_id = request.data.get("pay_wallet_id") or request.data.get("wallet_id")
    if raw_id is not None and str(raw_id).strip():
        try:
            wid = int(str(raw_id).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid pay_wallet_id.") from exc
        w = Wallet.objects.filter(pk=wid).first()
        if not w:
            raise ValueError("Wallet not found.")
        if not _wallet_user_may_pay_from(u, w):
            raise ValueError("This wallet cannot be used for checkout.")
        if not wallet_policy.wallet_payable_under_settings(w):
            raise ValueError("This wallet type is disabled for payments.")
        return w, _fund_source_label_for_wallet(w)
    w = _resolve_default_checkout_wallet(u)
    if not w:
        raise ValueError("No wallet found for checkout.")
    return w, _fund_source_label_for_wallet(w)


def _payment_method_from_client(raw: str) -> str | None:
    if not raw:
        return Order.PaymentMethod.COD
    s = str(raw).strip().lower().replace(" ", "_")
    mapping = {
        "cod": Order.PaymentMethod.COD,
        "cash_on_delivery": Order.PaymentMethod.COD,
        "esewa": Order.PaymentMethod.ESEWA,
        "khalti": Order.PaymentMethod.KHALTI,
        "ime_pay": Order.PaymentMethod.IME_PAY,
        "wallet": Order.PaymentMethod.WALLET,
    }
    if s in mapping:
        return mapping[s]
    if s in dict(Order.PaymentMethod.choices):
        return s
    return None


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_orders_checkout_wallet(request):
    """Default paying wallet and all payable buckets (same rules as POST checkout)."""
    gate = storefront_orders_gate_response()
    if gate:
        return gate
    u = request.user
    default_w = _resolve_default_checkout_wallet(u)
    default_payload = None
    if default_w:
        default_w = Wallet.objects.filter(pk=default_w.pk).select_related(
            "owner", "family_group", "family_category"
        ).first()
    if default_w:
        default_payload = {
            "id": default_w.pk,
            "fund_source": _checkout_fund_source_label_for_wallet(u, default_w),
            "balance": float(default_w.balance),
            "child_spending_limits_apply": _wallet_child_spending_limits_apply(
                default_w
            ),
        }
    payable = []
    for w in _payable_checkout_wallets_for_user(u):
        payable.append(
            {
                "id": w.pk,
                "fund_source": _checkout_fund_source_label_for_wallet(u, w),
                "balance": float(w.balance),
                "is_default": bool(default_w and w.pk == default_w.pk),
                "child_spending_limits_apply": _wallet_child_spending_limits_apply(w),
            }
        )
    return Response({"default": default_payload, "payable_wallets": payable})


def _portal_checkout_group_seller_sort_key(sid: int | None) -> tuple[int, int]:
    """Sort checkout seller group keys: vendor PKs ascending, in-house (None) last."""
    if sid is None:
        return (1, 0)
    return (0, sid)


def _request_data_dict(request) -> dict:
    if isinstance(request.data, dict):
        return request.data
    return dict(request.data.items())


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_orders_checkout_quote(request):
    """Read-only totals for checkout UI (pricing, flash, coupon, delivery) — no orders or stock changes."""
    gate = storefront_orders_gate_response()
    if gate:
        return gate
    u = request.user
    items = request.data.get("items")
    try:
        parsed = parse_checkout_items(items)
    except ValueError as e:
        return validation_error(str(e), field="items")

    want_delivery = request.data.get("want_delivery", True)
    if isinstance(want_delivery, str):
        want_delivery = want_delivery.lower() in ("true", "1", "yes")

    try:
        resolved = resolve_checkout_lines(
            parsed, u, select_for_update=False, strict_stock=False
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)

    groups = resolved.groups
    req_dict = _request_data_dict(request)
    raw_coupon = request.data.get("coupon_code")
    if raw_coupon is None:
        raw_coupon = request.data.get("coupon")
    coupon_obj, discount_total, coupon_err, seller_discounts, eligible_subtotal = (
        apply_coupon_split(
            groups,
            raw_coupon,
            strict_coupon=False,
        )
    )

    delivery_fee_total, delivery_alloc, _checkout_zone, d_err, delivery_weight_kg, shipping_method_id_echo = (
        compute_delivery_allocation(
            req_dict, want_delivery, resolved.cart_subtotal, groups
        )
    )
    if d_err:
        delivery_fee_total = Decimal("0")
        delivery_alloc = {sid: Decimal("0") for sid in groups}

    seller_subtotals = {
        sid: sum(lt for *_rest, lt in lines) for sid, lines in groups.items()
    }
    _orders_plan, grand_total = build_orders_plan(
        groups,
        seller_subtotals,
        delivery_alloc,
        seller_discounts,
        _portal_checkout_group_seller_sort_key,
    )
    savings = resolved.list_subtotal - resolved.cart_subtotal
    if savings < 0:
        savings = Decimal("0")
    flash_save = savings_from_flash_vs_product_sale(groups)
    line_rows = checkout_quote_line_rows(
        groups,
        seller_discounts,
        resolved.flash_deal_by_product_id,
        _portal_checkout_group_seller_sort_key,
    )

    coupon_applied = None
    if coupon_obj is not None and coupon_err is None:
        coupon_applied = {
            "type": coupon_obj.type,
            "value": float(coupon_obj.value),
        }

    sh_quote = ShippingSettings.load()
    return Response(
        {
            "subtotal": float(resolved.cart_subtotal),
            "list_subtotal": float(resolved.list_subtotal),
            "savings_vs_list": float(savings),
            "savings_flash": float(flash_save),
            "delivery_fee": float(delivery_fee_total),
            "coupon_discount": float(discount_total),
            "eligible_subtotal": float(eligible_subtotal),
            "total": float(grand_total),
            "coupon_error": coupon_err,
            "coupon_applied": coupon_applied,
            "flash_product_ids": resolved.flash_product_ids,
            "lines": line_rows,
            "stock_warnings": resolved.stock_warnings,
            "delivery_error": d_err,
            "delivery_weight_kg": float(delivery_weight_kg),
            "shipping_method_id": shipping_method_id_echo,
            "seller_pays_shipping": sh_quote.seller_pays_shipping,
        }
    )


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_orders_checkout(request):
    gate = storefront_orders_gate_response()
    if gate:
        return gate
    u = request.user
    items = request.data.get("items")
    if not isinstance(items, list) or not items:
        return validation_error("items must be a non-empty list", field="items")

    placed = _parse_placed_portal_body(request.data.get("placed_portal"))
    if not placed:
        return validation_error(
            "placed_portal is required (portal_main, portal_family, or portal_child).",
            field="placed_portal",
        )
    if not _user_may_use_placed_portal(u, placed):
        return validation_error(
            "You cannot place orders for this portal surface.",
            field="placed_portal",
        )

    want_delivery = request.data.get("want_delivery", True)
    if isinstance(want_delivery, str):
        want_delivery = want_delivery.lower() in ("true", "1", "yes")

    try:
        parsed = parse_checkout_items(items)
    except ValueError as e:
        return validation_error(str(e), field="items")

    try:
        with transaction.atomic():
            try:
                resolved = resolve_checkout_lines(
                    parsed, u, select_for_update=True, strict_stock=True
                )
            except ValueError as e:
                return Response({"detail": str(e)}, status=400)

            groups = resolved.groups
            cart_subtotal = resolved.cart_subtotal
            checkout_zone: ShippingZone | None = None

            raw_coupon = request.data.get("coupon_code")
            if raw_coupon is None:
                raw_coupon = request.data.get("coupon")
            try:
                coupon_obj, _discount_total, _coupon_err, seller_discounts, _eligible = (
                    apply_coupon_split(
                        groups,
                        raw_coupon,
                        strict_coupon=True,
                    )
                )
            except ValueError as e:
                return Response({"detail": str(e)}, status=400)

            delivery_fee_total, delivery_alloc, checkout_zone, d_err, _dw_kg, _sm_echo = (
                compute_delivery_allocation(
                    _request_data_dict(request),
                    want_delivery,
                    cart_subtotal,
                    groups,
                )
            )
            if d_err:
                return Response({"detail": d_err}, status=400)

            seller_subtotals = {
                sid: sum(lt for *_rest, lt in lines)
                for sid, lines in groups.items()
            }
            orders_plan, grand_total_plan = build_orders_plan(
                groups,
                seller_subtotals,
                delivery_alloc,
                seller_discounts,
                _portal_checkout_group_seller_sort_key,
            )

            raw_pay = request.data.get("payment_method")
            if raw_pay is None or not str(raw_pay).strip():
                return validation_error(
                    "payment_method is required; use wallet.",
                    field="payment_method",
                )
            payment_method = _payment_method_from_client(str(raw_pay))
            if not payment_method:
                return validation_error("invalid payment_method", field="payment_method")
            if payment_method != Order.PaymentMethod.WALLET:
                return validation_error(
                    "Only KhudraPasal Wallet is supported for checkout.",
                    field="payment_method",
                )

            notes = (request.data.get("notes") or "")[:500]
            pay_wallet: Wallet | None = None
            fund_source_label = ""
            if payment_method == Order.PaymentMethod.WALLET:
                pay_wallet, fund_source_label = _resolve_checkout_wallet(request)
                pay_wallet = Wallet.objects.select_for_update().get(pk=pay_wallet.pk)
                if pay_wallet.status != Wallet.Status.ACTIVE:
                    raise ValueError("Wallet is frozen.")
                if pay_wallet.balance < grand_total_plan:
                    raise ValueError("Insufficient balance")
                validate_child_spending_limits(u, pay_wallet, grand_total_plan)

            orders_created: list[Order] = []
            for vendor, lines, v_sub, v_delivery, d_amt, v_total in orders_plan:
                order = Order.objects.create(
                    order_number=_gen_order_number(),
                    customer=u,
                    seller=vendor,
                    status=Order.Status.PENDING,
                    payment_method=payment_method,
                    payment_status=Order.PaymentStatus.PENDING,
                    subtotal=v_sub,
                    delivery_fee=v_delivery,
                    discount_amount=d_amt,
                    total=v_total,
                    want_delivery=bool(want_delivery),
                    notes=notes,
                    is_pos_order=False,
                    placed_portal=placed,
                    payment_wallet=(
                        pay_wallet
                        if payment_method == Order.PaymentMethod.WALLET
                        and pay_wallet is not None
                        else None
                    ),
                )

                line_coupons = split_seller_discount_across_lines(lines, d_amt)
                for j, (p, qty, unit_price, line_total) in enumerate(lines):
                    coup_line = line_coupons[j]
                    fd_pk = resolved.flash_deal_by_product_id.get(p.pk)
                    OrderItem.objects.create(
                        order=order,
                        product=p,
                        quantity=qty,
                        list_unit_price=p.price,
                        flash_deal_id=fd_pk,
                        unit_price=unit_price,
                        coupon_discount_amount=coup_line,
                        total_price=(line_total - coup_line).quantize(Decimal("0.01")),
                    )
                    product_service.sync_stock_status(p)

                if want_delivery:
                    d = request.data.get("delivery") or request.data
                    full_name = (d.get("full_name") or "").strip()
                    mobile = (d.get("mobile") or "").strip()
                    area = (d.get("area_location") or "").strip()
                    if not full_name or not mobile or not area:
                        raise ValueError(
                            "Delivery requires full_name, mobile, and area_location."
                        )
                    lat_raw = d.get("latitude")
                    lng_raw = d.get("longitude")
                    lat_dec = None
                    lng_dec = None
                    if lat_raw is not None and lng_raw is not None:
                        try:
                            lat_dec = Decimal(str(float(lat_raw))).quantize(
                                Decimal("0.000001")
                            )
                            lng_dec = Decimal(str(float(lng_raw))).quantize(
                                Decimal("0.000001")
                            )
                        except (ValueError, TypeError):
                            lat_dec = None
                            lng_dec = None
                    DeliveryAddress.objects.create(
                        order=order,
                        shipping_zone=checkout_zone,
                        full_name=full_name[:150],
                        mobile=mobile[:15],
                        secondary_contact=(d.get("secondary_contact") or "")[:15],
                        area_location=area[:255],
                        landmark=(d.get("landmark") or "")[:255],
                        google_map_link=(d.get("google_map_link") or "")[:200],
                        latitude=lat_dec,
                        longitude=lng_dec,
                        delivery_notes=(d.get("delivery_notes") or d.get("notes") or "")[
                            :500
                        ],
                    )

                orders_created.append(order)

            if coupon_obj is not None and orders_created:
                canonical = min(orders_created, key=lambda o: o.pk)
                Order.objects.filter(pk=canonical.pk).update(coupon_id=coupon_obj.pk)

            if payment_method in PORTAL_GATEWAY_PAYMENT_METHODS:
                for order in orders_created:
                    PaymentTransaction.objects.create(
                        txn_ref=f"{order.order_number}-{uuid4().hex[:16]}",
                        order=order,
                        customer=u,
                        amount=order.total,
                        method=payment_method,
                        status=PaymentTransaction.Status.PENDING,
                    )

            if payment_method == Order.PaymentMethod.WALLET and pay_wallet is not None:
                for order in orders_created:
                    pay_with_wallet(
                        order, pay_wallet, fund_source=fund_source_label
                    )

            if (
                u.role == User.Role.CHILD
                and orders_created
                and payment_method == Order.PaymentMethod.WALLET
                and pay_wallet is not None
            ):
                checkout_pairs: list[tuple[Product, int]] = []
                for order in orders_created:
                    for oi in OrderItem.objects.filter(order=order).select_related(
                        "product"
                    ):
                        checkout_pairs.append((oi.product, oi.quantity))
                if checkout_pairs:
                    consume_purchase_approvals_after_checkout(u, checkout_pairs)

    except ValueError as e:
        return Response({"detail": str(e)}, status=400)

    grand_total = sum((o.total for o in orders_created), Decimal("0"))
    status_set = {o.payment_status for o in orders_created}
    payment_status_out = (
        next(iter(status_set)) if len(status_set) == 1 else "mixed"
    )

    for order in orders_created:
        order.refresh_from_db()
        seller = order.seller
        if order.seller_id and seller:
            vu = getattr(seller, "user", None)
            if vu is not None:
                Notification.objects.create(
                    title="New order",
                    message=f"Order {order.order_number} — Rs. {float(order.total):,.2f}",
                    type=Notification.Type.ORDER,
                    target=Notification.Target.VENDORS,
                    recipient=vu,
                    action_url="/vendor/all-orders",
                )

    first = orders_created[0]
    requires_payment_confirmation = payment_method in PORTAL_GATEWAY_PAYMENT_METHODS
    return Response(
        {
            "orders": [
                {
                    "order_number": o.order_number,
                    "total": float(o.total),
                    "payment_status": o.payment_status,
                }
                for o in orders_created
            ],
            "order_number": first.order_number,
            "total": float(grand_total),
            "payment_status": payment_status_out,
            "requires_payment_confirmation": requires_payment_confirmation,
        },
        status=201,
    )


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalShopper])
def portal_orders_payment_complete(request):
    """Mark pending gateway payment transactions successful; orders become paid and commissions settle."""
    u = request.user
    raw = request.data.get("order_numbers")
    if not isinstance(raw, list) or not raw:
        return validation_error(
            "order_numbers must be a non-empty list", field="order_numbers"
        )
    nums = [str(x).strip() for x in raw if str(x).strip()]
    if not nums:
        return validation_error(
            "order_numbers must be a non-empty list", field="order_numbers"
        )

    payload = request.data.get("gateway_payload")
    gw_update: dict | None = None
    if isinstance(payload, dict):
        gw_update = payload

    already_paid: list[str] = []
    completed: list[str] = []

    with transaction.atomic():
        for num in nums:
            o = (
                Order.objects.select_for_update()
                .filter(order_number=num, customer_id=u.pk)
                .first()
            )
            if not o:
                return Response({"detail": f"Order {num} not found."}, status=404)
            if o.payment_status == Order.PaymentStatus.PAID:
                already_paid.append(num)
                continue
            if o.payment_status != Order.PaymentStatus.PENDING:
                return Response(
                    {
                        "detail": (
                            f"Order {num} cannot be confirmed "
                            f"(payment_status={o.payment_status})."
                        )
                    },
                    status=400,
                )
            pt = (
                PaymentTransaction.objects.select_for_update()
                .filter(
                    order_id=o.pk,
                    customer_id=u.pk,
                    status=PaymentTransaction.Status.PENDING,
                )
                .first()
            )
            if not pt:
                return Response(
                    {
                        "detail": (
                            f"No pending gateway payment for order {num}. "
                            "COD orders must be marked paid by admin."
                        )
                    },
                    status=400,
                )
            if pt.amount != o.total:
                return Response(
                    {"detail": f"Payment amount mismatch for order {num}."},
                    status=400,
                )
            pt.status = PaymentTransaction.Status.SUCCESS
            if gw_update is not None:
                pt.gateway_response = gw_update
                pt.save(update_fields=["status", "gateway_response"])
            else:
                pt.save(update_fields=["status"])
            completed.append(num)

    out_orders: list[dict] = []
    for num in nums:
        ox = Order.objects.filter(order_number=num, customer_id=u.pk).first()
        if ox:
            out_orders.append(
                {
                    "order_number": ox.order_number,
                    "payment_status": ox.payment_status,
                    "total": float(ox.total),
                }
            )

    return Response(
        {
            "orders": out_orders,
            "completed": completed,
            "already_paid": already_paid,
        }
    )


@api_view(["GET"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalCustomer])
def portal_switch_portal_context(request):
    u = request.user
    has_family = user_has_family_portal_access(u)
    has_child = bool(
        u.role == User.Role.CHILD
        or FamilyMember.objects.filter(
            user=u,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        ).exists()
    )
    can_create_family = bool(
        u.role == User.Role.NORMAL
        and not FamilyGroup.objects.filter(
            leader=u, status=FamilyGroup.Status.ACTIVE
        ).exists()
        and not FamilyMember.objects.filter(
            user=u, status=FamilyMember.Status.ACTIVE
        ).exists()
    )
    group_types = [
        {"value": value, "label": label} for value, label in FamilyGroup.Type.choices
    ]
    return Response(
        {
            "has_family_portal_access": has_family,
            "has_child_portal_access": has_child,
            "can_create_family_group": can_create_family,
            "family_group_types": group_types,
            "create_family_defaults": {"status": FamilyGroup.Status.ACTIVE},
        }
    )


_INVITE_ROLE_SET = frozenset(
    {
        FamilyInvite.Role.CHILD,
        FamilyInvite.Role.SPOUSE,
        FamilyInvite.Role.GUEST,
        FamilyInvite.Role.MANAGER,
    }
)


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalCustomer])
def portal_family_group_create(request):
    """NORMAL customer creates a family and becomes PARENT (use family portal after)."""
    u = request.user
    if not isinstance(request.data, Mapping):
        return validation_error(
            'Expected a JSON object with a "name" field (not a bare JSON string).',
        )
    name = (request.data.get("name") or "").strip()
    if not name:
        return validation_error("name is required", field="name")
    gtype = request.data.get("type") or FamilyGroup.Type.FAMILY
    try:
        group = family_service.create_family_group_for_user(u, name, gtype)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(
        {"id": str(group.pk), "name": group.name, "type": group.type},
        status=201,
    )


@api_view(["GET", "POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated, IsPortalParent])
def portal_family_invites(request):
    if request.method == "GET":
        primary = _primary_family_group(request.user)
        if not primary:
            return Response({"results": []})
        qs = FamilyInvite.objects.filter(
            group=primary, status=FamilyInvite.Status.PENDING
        ).order_by("-created_at")[:50]
        rows = [
            {
                "id": str(inv.pk),
                "phone": inv.phone,
                "role": inv.role,
                "token": inv.token,
                "invite_method": inv.invite_method,
                "spending_limit": float(inv.spending_limit),
                "initial_balance": float(inv.initial_balance),
                "expires_at": inv.expires_at.isoformat(),
                "created_at": inv.created_at.isoformat(),
            }
            for inv in qs
        ]
        return Response({"results": rows})

    primary = _primary_family_group(request.user)
    if not primary:
        return Response({"detail": "No family group found."}, status=400)
    phone = (request.data.get("phone") or "").strip()
    role = (request.data.get("role") or FamilyInvite.Role.CHILD).strip()
    if role not in _INVITE_ROLE_SET:
        return validation_error("invalid role", field="role")
    spending_limit = _to_decimal(request.data.get("spending_limit"), "0")
    initial_balance = _to_decimal(request.data.get("initial_balance"), "0")
    raw_im = (request.data.get("invite_method") or "phone").strip().lower()
    im = (
        FamilyInvite.InviteMethod.LINK
        if raw_im == "link"
        else FamilyInvite.InviteMethod.PHONE
    )
    try:
        inv = family_service.create_invite(
            request.user,
            primary,
            phone,
            role,
            spending_limit,
            initial_balance,
            invite_method=im,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(
        {
            "id": str(inv.pk),
            "token": inv.token,
            "expires_at": inv.expires_at.isoformat(),
            "phone": inv.phone,
            "role": inv.role,
        },
        status=201,
    )


@api_view(["POST"])
@authentication_classes(PORTAL_API_AUTHENTICATION)
@permission_classes([IsAuthenticated])
def portal_family_invites_accept(request):
    """Invitee verifies OTP and creates a pending join request; parent approves via portal."""
    token = (request.data.get("token") or "").strip()
    code = (request.data.get("otp") or "").strip()
    raw_phone = (request.data.get("phone") or "").strip()
    if not token or len(code) != 6 or not code.isdigit():
        return validation_error("token and a 6-digit otp are required", field="otp")
    phone = normalize_nepal_phone(raw_phone)
    if not phone:
        return validation_error("Enter a valid Nepal mobile number.", field="phone")

    u = request.user
    if u.phone != phone:
        return Response(
            {"detail": "Sign in with the invited phone number."},
            status=400,
        )

    with transaction.atomic():
        invite = FamilyInvite.objects.select_for_update().filter(token=token).first()
        if not invite:
            return Response({"detail": "Invalid invite."}, status=400)
        if invite.status != FamilyInvite.Status.PENDING:
            return Response({"detail": "Invite is no longer pending."}, status=400)
        if invite.expires_at < timezone.now():
            return Response({"detail": "Invite has expired."}, status=400)
        if invite.phone != phone:
            return Response({"detail": "Phone does not match this invite."}, status=400)
        try:
            otp_service.consume(phone, OTPVerification.Purpose.FAMILY_INVITE, code)
        except otp_service.OTPError as e:
            return Response({"detail": str(e)}, status=400)
        jr = family_join_request_service.ensure_pending_join_request_after_invite_otp(
            user=u, invite=invite
        )

    return Response(
        {
            "ok": True,
            "pending_approval": True,
            "group_id": str(invite.group_id),
            "join_request": FamilyJoinRequestReadSerializer(jr).data,
        }
    )
