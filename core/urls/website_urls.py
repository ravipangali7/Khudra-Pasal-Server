from django.urls import path

from core.views.website import family_portal_join_views, home_views

urlpatterns = [
    path(
        "family-invite/<str:token>/",
        home_views.website_family_invite_meta,
        name="website-family-invite-meta",
    ),
    path(
        "family-portal-join/<str:token>/",
        family_portal_join_views.website_family_portal_join,
        name="website-family-portal-join",
    ),
    path("search-placeholders/", home_views.search_placeholders_list, name="website-search-placeholders"),
    path("store-info/", home_views.store_info, name="website-store-info"),
    path("brands/", home_views.brands_list, name="website-brands"),
    path("categories/", home_views.categories_list, name="website-categories"),
    path("catalog/", home_views.catalog_list, name="website-catalog"),
    path("products/", home_views.products_list, name="website-products"),
    path("products/<str:identifier>/", home_views.product_detail, name="website-product-detail"),
    path("products/<str:identifier>/reviews/", home_views.product_reviews_list, name="website-product-reviews"),
    path("cms-pages/<slug:slug>/", home_views.cms_page_public, name="website-cms-page"),
    path("blog-posts/", home_views.blog_posts_list, name="website-blog-posts"),
    path("blog-posts/<slug:slug>/", home_views.blog_post_public, name="website-blog-post-detail"),
    path("banners/", home_views.banners_list, name="website-banners"),
    path("deals/", home_views.deals_list, name="website-deals"),
    path("reels/", home_views.reels_list, name="website-reels"),
    path("reels/<int:pk>/interactions/", home_views.reel_interaction_create, name="website-reel-interaction-create"),
    path(
        "reels/<int:pk>/interactions/<str:interaction_type>/",
        home_views.reel_interaction_delete,
        name="website-reel-interaction-delete",
    ),
    path("reels/<int:pk>/comments/", home_views.reel_comments, name="website-reel-comments"),
    path("reels/<int:pk>/views/", home_views.reel_view_record, name="website-reel-view-record"),
    path("cart/", home_views.cart_detail, name="website-cart"),
    path("cart/items/", home_views.cart_item_add, name="website-cart-add-item"),
    path("cart/items/<int:pk>/", home_views.cart_item_detail, name="website-cart-item-detail"),
    path("wishlist/", home_views.wishlist_list, name="website-wishlist"),
    path("wishlist/items/", home_views.wishlist_item_add, name="website-wishlist-add-item"),
    path("wishlist/items/<int:product_id>/", home_views.wishlist_item_remove, name="website-wishlist-remove-item"),
]

