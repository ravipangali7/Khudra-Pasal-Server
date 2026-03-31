from django.urls import path

from core.models import Order
from core.views import navigation_views
from core.views.portal import portal_kyc, portal_views

urlpatterns = [
    path("auth/login/", portal_views.child_portal_login, name="child-portal-login"),
    path("navigation/", navigation_views.portal_navigation, name="child-portal-navigation"),
    path("child/summary/", portal_views.portal_child_summary, name="child-portal-summary"),
    path(
        "child/wallet-transactions/",
        portal_views.portal_child_transactions,
        name="child-portal-wallet-txns",
    ),
    path(
        "child/peer-members/",
        portal_views.portal_child_peer_members,
        name="child-portal-peer-members",
    ),
    path(
        "child/wallet/peer-transfer/",
        portal_views.portal_child_wallet_peer_transfer,
        name="child-portal-wallet-peer-transfer",
    ),
    path(
        "child/wallet/topup/",
        portal_views.portal_child_wallet_topup,
        name="child-portal-wallet-topup",
    ),
    path(
        "child/wallet/withdraw/",
        portal_views.portal_child_wallet_withdraw,
        name="child-portal-wallet-withdraw",
    ),
    path("kyc/schema/", portal_kyc.portal_kyc_schema, name="child-portal-kyc-schema"),
    path("kyc/status/", portal_kyc.portal_kyc_status, name="child-portal-kyc-status"),
    path("kyc/submit/", portal_kyc.portal_kyc_submit, name="child-portal-kyc-submit"),
    path("child/rules/", portal_views.portal_child_rules, name="child-portal-rules"),
    path(
        "orders/<int:pk>/refund-request/",
        portal_views.portal_order_refund_request,
        {"refund_surface": Order.PlacedPortal.PORTAL_CHILD},
        name="child-portal-order-refund-request",
    ),
    path(
        "orders/",
        portal_views.portal_orders_list,
        {"list_placed_portal": Order.PlacedPortal.PORTAL_CHILD},
        name="child-portal-orders",
    ),
]
