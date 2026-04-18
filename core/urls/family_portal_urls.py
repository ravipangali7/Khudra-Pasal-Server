from django.urls import path

from core.models import Order
from core.views import navigation_views
from core.views.portal import portal_views, purchase_approval_views

urlpatterns = [
    path("auth/login/", portal_views.family_portal_login, name="family-portal-login"),
    path("navigation/", navigation_views.portal_navigation, name="family-portal-navigation"),
    path("family/group/", portal_views.portal_family_group_create, name="family-portal-group-create"),
    path("family/invites/", portal_views.portal_family_invites, name="family-portal-invites"),
    path(
        "family/invites/accept/",
        portal_views.portal_family_invites_accept,
        name="family-portal-invites-accept",
    ),
    path("family/children/", portal_views.portal_family_children, name="family-portal-children"),
    path(
        "family/members/batch/",
        portal_views.portal_family_members_batch,
        name="family-portal-members-batch",
    ),
    path(
        "family/members/<int:pk>/",
        portal_views.portal_family_member_detail,
        name="family-portal-member-detail",
    ),
    path("family/members/", portal_views.portal_family_members, name="family-portal-members"),
    path(
        "family/product-restrictions/",
        portal_views.portal_family_product_restrictions,
        name="family-portal-product-restrictions",
    ),
    path(
        "family/auto-approval-rules/<int:pk>/",
        portal_views.portal_family_auto_approval_rule_detail,
        name="family-portal-auto-approval-detail",
    ),
    path(
        "family/auto-approval-rules/",
        portal_views.portal_family_auto_approval_rules,
        name="family-portal-auto-approval-rules",
    ),
    path(
        "family/wallet-transactions/",
        portal_views.portal_family_transactions,
        name="family-portal-wallet-txns",
    ),
    path(
        "family/wallet/withdrawals/",
        portal_views.portal_family_wallet_withdrawals,
        name="family-portal-wallet-withdrawals",
    ),
    path(
        "wallet/otp/transfer/",
        portal_views.portal_wallet_otp_for_transfer,
        name="family-portal-wallet-otp-transfer",
    ),
    path(
        "wallet/otp/withdraw/",
        portal_views.portal_wallet_otp_for_withdraw,
        name="family-portal-wallet-otp-withdraw",
    ),
    path(
        "wallet/settings-public/",
        portal_views.portal_wallet_public_settings,
        name="family-portal-wallet-settings-public",
    ),
    path(
        "payout-accounts/",
        portal_views.portal_payout_accounts_list_create,
        name="family-portal-payout-accounts",
    ),
    path(
        "payout-accounts/<int:pk>/",
        portal_views.portal_payout_account_detail,
        name="family-portal-payout-account-detail",
    ),
    path(
        "orders/<int:pk>/refund-request/",
        portal_views.portal_order_refund_request,
        {"refund_surface": Order.PlacedPortal.PORTAL_FAMILY},
        name="family-portal-order-refund-request",
    ),
    path(
        "orders/<int:pk>/",
        portal_views.portal_order_detail,
        {"list_placed_portal": Order.PlacedPortal.PORTAL_FAMILY},
        name="family-portal-order-detail",
    ),
    path(
        "orders/",
        portal_views.portal_orders_list,
        {"list_placed_portal": Order.PlacedPortal.PORTAL_FAMILY},
        name="family-portal-orders",
    ),
    path(
        "family/purchase-approval-requests/",
        purchase_approval_views.portal_family_purchase_approval_requests,
        name="family-portal-purchase-approval-requests",
    ),
    path(
        "family/purchase-approval-requests/<int:pk>/",
        purchase_approval_views.portal_family_purchase_approval_request_detail,
        name="family-portal-purchase-approval-request-detail",
    ),
]
