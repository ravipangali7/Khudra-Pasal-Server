from django.urls import include, path

from core.models import Order
from core.views.portal import portal_views
from core.views.website import ai_pitch_views, home_views

urlpatterns = [
    path("", include("core.urls.meta_urls")),
    path("ai-pitch/", ai_pitch_views.ai_pitch, name="ai-pitch"),
    path("wallet-hub/", include("core.urls.wallet_hub_urls")),
    path("auth/", include("core.urls.auth_urls")),
    path("reels/dashboard/", home_views.reels_dashboard, name="reels-dashboard"),
    path("reels/vendors/", home_views.reels_vendors_directory, name="reels-vendors-directory"),
    path("reels/vendor/<int:vendor_id>/", home_views.reels_by_vendor_list, name="reels-by-vendor"),
    path("reels/trending/all-vendors/", home_views.reels_trending_list, name="reels-trending-all-vendors"),
    path("reels/trending/", home_views.reels_trending_list, name="reels-trending"),
    path("products/all-vendors/", home_views.products_all_vendors_list, name="products-all-vendors"),
    path("website/", include("core.urls.website_urls")),
    path("admin/", include("core.urls.admin_urls")),
    path("vendor/", include("core.urls.vendor_urls")),
    path("portal/", include("core.urls.portal_urls")),
    path("family-portal/", include("core.urls.family_portal_urls")),
    path("child-portal/", include("core.urls.child_portal_urls")),
    path(
        "orders/all-vendors/",
        portal_views.portal_orders_list,
        {"list_placed_portal": Order.PlacedPortal.PORTAL_MAIN},
        name="orders-all-vendors",
    ),
    path("transactions/", portal_views.portal_wallet_transactions, name="transactions"),
]

