from django.urls import path

from core.views import meta_views

urlpatterns = [
    path("settings/public/", meta_views.public_settings, name="settings-public"),
    path("meta/sitemap.xml", meta_views.sitemap_xml, name="meta-sitemap"),
    path(
        "website/blog-posts/<slug:slug>/share/",
        meta_views.blog_post_share,
        name="website-blog-post-share",
    ),
    path(
        "website/cms-pages/<slug:slug>/share/",
        meta_views.cms_page_share,
        name="website-cms-page-share",
    ),
    path(
        "website/products/<str:identifier>/share/",
        meta_views.product_share,
        name="website-product-share",
    ),
]
