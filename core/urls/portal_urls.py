from django.urls import path

from core.models import Order
from core.views import navigation_views
from core.views.portal import portal_kyc, portal_views

urlpatterns = [
    path("navigation/", navigation_views.portal_navigation, name="portal-navigation"),
    path("auth/login/", portal_views.portal_login, name="portal-login"),
    path("auth/change-password/", portal_views.portal_change_password, name="portal-change-password"),
    path("me/", portal_views.portal_me, name="portal-me"),
    path("kyc/schema/", portal_kyc.portal_kyc_schema, name="portal-kyc-schema"),
    path("kyc/status/", portal_kyc.portal_kyc_status, name="portal-kyc-status"),
    path("kyc/submit/", portal_kyc.portal_kyc_submit, name="portal-kyc-submit"),
    path("self-profile/", portal_views.portal_self_profile, name="portal-self-profile"),
    path("profile/", portal_views.portal_customer_profile, name="portal-customer-profile"),
    path("reels/favourites/", portal_views.portal_reels_favourites, name="portal-reels-favourites"),
    path("wallet/topup/", portal_views.portal_wallet_topup, name="portal-wallet-topup"),
    path(
        "wallet/transfer-recipients/",
        portal_views.portal_wallet_transfer_recipients,
        name="portal-wallet-transfer-recipients",
    ),
    path("wallet/transfer/", portal_views.portal_wallet_transfer, name="portal-wallet-transfer"),
    path("wallet/withdraw/", portal_views.portal_wallet_withdraw, name="portal-wallet-withdraw"),
    path("support/faqs/", portal_views.portal_support_faqs, name="portal-support-faqs"),
    path(
        "support/attachments/<int:attachment_id>/",
        portal_views.portal_support_attachment,
        name="portal-support-attachment",
    ),
    path("support/tickets/", portal_views.portal_support_tickets, name="portal-support-tickets"),
    path(
        "support/tickets/<str:ticket_number>/messages/",
        portal_views.portal_support_ticket_messages,
        name="portal-support-ticket-messages",
    ),
    path(
        "support/tickets/<str:ticket_number>/",
        portal_views.portal_support_ticket_detail,
        name="portal-support-ticket-detail",
    ),
    path(
        "delivery-default/",
        portal_views.portal_delivery_default,
        name="portal-delivery-default",
    ),
    path(
        "orders/checkout-wallet/",
        portal_views.portal_orders_checkout_wallet,
        name="portal-orders-checkout-wallet",
    ),
    path("orders/checkout/", portal_views.portal_orders_checkout, name="portal-orders-checkout"),
    path(
        "orders/payment/complete/",
        portal_views.portal_orders_payment_complete,
        name="portal-orders-payment-complete",
    ),
    path("summary/", portal_views.portal_summary, name="portal-summary"),
    path(
        "switch-portal/context/",
        portal_views.portal_switch_portal_context,
        name="portal-switch-portal-context",
    ),
    path(
        "orders/<int:pk>/refund-request/",
        portal_views.portal_order_refund_request,
        {"refund_surface": Order.PlacedPortal.PORTAL_MAIN},
        name="portal-order-refund-request",
    ),
    path(
        "orders/",
        portal_views.portal_orders_list,
        {"list_placed_portal": Order.PlacedPortal.PORTAL_MAIN},
        name="portal-orders",
    ),
    path(
        "wallet-transactions/",
        portal_views.portal_wallet_transactions,
        name="portal-wallet-txns",
    ),
    path(
        "notifications/mark-read/",
        portal_views.portal_notifications_mark_read,
        name="portal-notifications-mark-read",
    ),
    path(
        "notifications/",
        portal_views.portal_notifications_list,
        name="portal-notifications",
    ),
    path(
        "notifications/<int:pk>/",
        portal_views.portal_notification_detail_write,
        name="portal-notifications-write",
    ),
    path(
        "family/children/",
        portal_views.portal_family_children,
        name="portal-family-children",
    ),
    path(
        "family/group/",
        portal_views.portal_family_group_create,
        name="portal-family-group-create",
    ),
    path(
        "family/invites/",
        portal_views.portal_family_invites,
        name="portal-family-invites",
    ),
    path(
        "family/invites/accept/",
        portal_views.portal_family_invites_accept,
        name="portal-family-invites-accept",
    ),
    path(
        "family/members/batch/",
        portal_views.portal_family_members_batch,
        name="portal-family-members-batch",
    ),
    path(
        "family/members/<int:pk>/",
        portal_views.portal_family_member_detail,
        name="portal-family-member-detail",
    ),
    path(
        "family/members/",
        portal_views.portal_family_members,
        name="portal-family-members",
    ),
    path(
        "family/product-restrictions/",
        portal_views.portal_family_product_restrictions,
        name="portal-family-product-restrictions",
    ),
    path(
        "family/auto-approval-rules/<int:pk>/",
        portal_views.portal_family_auto_approval_rule_detail,
        name="portal-family-auto-approval-detail",
    ),
    path(
        "family/auto-approval-rules/",
        portal_views.portal_family_auto_approval_rules,
        name="portal-family-auto-approval-rules",
    ),
    path(
        "family/wallet-transactions/",
        portal_views.portal_family_transactions,
        name="portal-family-wallet-txns",
    ),
    path(
        "family/join-request/",
        portal_views.portal_family_join_requests,
        name="portal-family-join-requests",
    ),
    path(
        "family/join-request/<int:pk>/",
        portal_views.portal_family_join_request_detail,
        name="portal-family-join-request-detail",
    ),
    path(
        "family/join-share-link/",
        portal_views.portal_family_join_share_link,
        name="portal-family-join-share-link",
    ),
    path(
        "family/wallet/load/",
        portal_views.portal_family_wallet_load,
        name="portal-family-wallet-load",
    ),
    path(
        "family/wallet/distribute/",
        portal_views.portal_family_wallet_distribute,
        name="portal-family-wallet-distribute",
    ),
    path(
        "family/wallet/transfer/",
        portal_views.portal_family_wallet_transfer,
        name="portal-family-wallet-transfer",
    ),
    path(
        "family/wallet/categories/meta/",
        portal_views.portal_family_wallet_categories_meta,
        name="portal-family-wallet-categories-meta",
    ),
    path(
        "family/wallet/categories/",
        portal_views.portal_family_wallet_categories,
        name="portal-family-wallet-categories",
    ),
    path("child/summary/", portal_views.portal_child_summary, name="portal-child-summary"),
    path(
        "child/wallet-transactions/",
        portal_views.portal_child_transactions,
        name="portal-child-wallet-txns",
    ),
    path(
        "child/peer-members/",
        portal_views.portal_child_peer_members,
        name="portal-child-peer-members",
    ),
    path(
        "child/wallet/peer-transfer/",
        portal_views.portal_child_wallet_peer_transfer,
        name="portal-child-wallet-peer-transfer",
    ),
    path(
        "child/wallet/topup/",
        portal_views.portal_child_wallet_topup,
        name="portal-child-wallet-topup",
    ),
    path(
        "child/wallet/withdraw/",
        portal_views.portal_child_wallet_withdraw,
        name="portal-child-wallet-withdraw",
    ),
    path("child/rules/", portal_views.portal_child_rules, name="portal-child-rules"),
]
