from django.urls import path

from core.views import navigation_views
from core.views.portal import portal_views
from core.views.vendor import vendor_inventory_views, vendor_resources, vendor_views

urlpatterns = [
    path("navigation/", navigation_views.vendor_navigation, name="vendor-navigation"),
    path("auth/login/", vendor_views.vendor_login, name="vendor-login"),
    path("auth/logout/", vendor_views.vendor_logout, name="vendor-logout"),
    path("auth/change-password/", vendor_resources.vendor_change_password, name="vendor-change-password"),
    path("me/", vendor_views.vendor_me, name="vendor-me"),
    path("summary/", vendor_views.vendor_summary, name="vendor-summary"),
    path(
        "notifications/mark-read/",
        vendor_views.vendor_notifications_mark_read,
        name="vendor-notifications-mark-read",
    ),
    path("notifications/", vendor_views.vendor_notifications_list, name="vendor-notifications"),
    path(
        "notifications/<int:pk>/",
        vendor_views.vendor_notification_detail_write,
        name="vendor-notifications-write",
    ),
    path("profile/", vendor_resources.vendor_profile, name="vendor-profile"),
    path("settings/", vendor_resources.vendor_settings, name="vendor-settings"),
    path("bank-detail/", vendor_resources.vendor_bank_detail, name="vendor-bank"),
    path("catalog/categories/", vendor_resources.vendor_catalog_categories, name="vendor-cat-categories"),
    path("catalog/brands/", vendor_resources.vendor_catalog_brands, name="vendor-cat-brands"),
    path("catalog/units/", vendor_resources.vendor_catalog_units, name="vendor-cat-units"),
    path("catalog/attributes/", vendor_resources.vendor_catalog_attributes, name="vendor-cat-attributes"),
    path("products/slug-preview/", vendor_resources.vendor_product_slug_preview, name="vendor-product-slug"),
    path("products/create/", vendor_resources.vendor_product_create, name="vendor-products-create"),
    path("products/<int:pk>/", vendor_resources.vendor_product_detail, name="vendor-product-detail"),
    path("orders/", vendor_views.vendor_orders_list, name="vendor-orders"),
    path("orders/<str:order_number>/", vendor_resources.vendor_order_detail, name="vendor-order-detail"),
    path("refunds/", vendor_resources.vendor_refunds_list, name="vendor-refunds"),
    path("pos/checkout/", vendor_resources.vendor_pos_checkout, name="vendor-pos-checkout"),
    path("reviews/", vendor_views.vendor_reviews_list, name="vendor-reviews"),
    path("reviews/<int:pk>/", vendor_resources.vendor_review_update, name="vendor-review-update"),
    path("wallet-transactions/", vendor_views.vendor_wallet_transactions, name="vendor-wallet-txns"),
    path(
        "commission-settlements/",
        vendor_views.vendor_commission_settlements,
        name="vendor-commission-settlements",
    ),
    path("withdrawals/", vendor_resources.vendor_withdrawals, name="vendor-withdrawals"),
    path(
        "payout-accounts/",
        portal_views.portal_payout_accounts_list_create,
        name="vendor-payout-accounts",
    ),
    path(
        "payout-accounts/<int:pk>/",
        portal_views.portal_payout_account_detail,
        name="vendor-payout-account-detail",
    ),
    path("customers/", vendor_resources.vendor_customers_list, name="vendor-customers"),
    path("reports/summary/", vendor_resources.vendor_reports_summary, name="vendor-reports-summary"),
    path("reports/export.csv", vendor_resources.vendor_reports_export_csv, name="vendor-reports-csv"),
    path(
        "support/super-admin-contact/",
        vendor_resources.vendor_support_super_admin_contact,
        name="vendor-support-super-admin-contact",
    ),
    path("support/tickets/", vendor_resources.vendor_support_tickets, name="vendor-support-tickets"),
    path(
        "support/tickets/<str:ticket_number>/messages/",
        vendor_resources.vendor_support_ticket_messages,
        name="vendor-support-ticket-messages",
    ),
    path(
        "support/tickets/<str:ticket_number>/",
        vendor_resources.vendor_support_ticket_detail,
        name="vendor-support-ticket-detail",
    ),
    path(
        "support/attachments/<int:attachment_id>/",
        vendor_resources.vendor_support_attachment,
        name="vendor-support-attachment",
    ),
    path("faqs/", vendor_resources.vendor_faqs_list, name="vendor-faqs"),
    path("reels/", vendor_resources.vendor_reels_list_create, name="vendor-reels"),
    path("reels/favourites/", vendor_resources.vendor_reels_favourites, name="vendor-reels-favourites"),
    path("reels/<int:pk>/", vendor_resources.vendor_reel_detail, name="vendor-reel-detail"),
    path("products/", vendor_views.vendor_products_list, name="vendor-products"),
    path("suppliers/", vendor_inventory_views.vendor_suppliers, name="vendor-suppliers"),
    path(
        "suppliers/<int:pk>/ledger/",
        vendor_inventory_views.vendor_supplier_ledger,
        name="vendor-supplier-ledger",
    ),
    path(
        "suppliers/<int:pk>/",
        vendor_inventory_views.vendor_supplier_detail,
        name="vendor-supplier-detail",
    ),
    path(
        "stock-purchases/",
        vendor_inventory_views.vendor_stock_purchases,
        name="vendor-stock-purchases",
    ),
    path(
        "stock-purchases/<int:pk>/",
        vendor_inventory_views.vendor_stock_purchase_detail,
        name="vendor-stock-purchase-detail",
    ),
    path(
        "stock-purchases/<int:pk>/post/",
        vendor_inventory_views.vendor_stock_purchase_post,
        name="vendor-stock-purchase-post",
    ),
    path("ledger/", vendor_inventory_views.vendor_ledger, name="vendor-ledger"),
]
