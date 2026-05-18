"""Public SEO endpoints: settings, sitemap, share landings."""

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.models import BlogPost, CMSPage, SiteSettings
from core.seo.share import render_share_html, share_context_from_entity
from core.seo.sitemap import build_sitemap_xml
from core.seo.resolvers import spa_url
from core.seo.media_urls import ensure_https_og_image
from core.views.admin.admin_write_utils import absolute_media_url, product_primary_image_url
from core.views.website.home_views import _active_products_queryset


def _site_seo_assets(request):
    site = SiteSettings.load()
    logo = absolute_media_url(request, site.site_logo) if site.site_logo else ""
    favicon = absolute_media_url(request, site.site_favicon) if getattr(site, "site_favicon", None) and site.site_favicon else ""
    cover = absolute_media_url(request, site.cover_image) if getattr(site, "cover_image", None) and site.cover_image else ""
    return site, logo, favicon, cover


@api_view(["GET"])
@permission_classes([AllowAny])
def public_settings(request):
    """GET /api/settings/public/ — SPA site SEO defaults (camelCase)."""
    site, logo, favicon, cover = _site_seo_assets(request)
    return Response(
        {
            "siteName": site.site_name,
            "siteMetaDescription": site.site_description or "",
            "siteLogo": logo,
            "siteFavicon": favicon,
            "coverImage": cover or logo,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def sitemap_xml(request):
    """GET /api/meta/sitemap.xml"""
    xml = build_sitemap_xml()
    return HttpResponse(xml, content_type="application/xml; charset=utf-8")


def _share_response(request, ctx: dict) -> HttpResponse:
    html = render_share_html(
        title=ctx["title"],
        description=ctx["description"],
        canonical_spa_url=ctx["canonical_spa_url"],
        og_type=ctx["og_type"],
        og_image=ensure_https_og_image(ctx.get("og_image") or ""),
        share_url=ctx["share_url"],
        site_name=ctx.get("site_name") or "",
    )
    return HttpResponse(html, content_type="text/html; charset=utf-8")


@api_view(["GET"])
@permission_classes([AllowAny])
def blog_post_share(request, slug):
    post = get_object_or_404(
        BlogPost.objects.select_related("author"),
        slug=slug,
        status=BlogPost.Status.PUBLISHED,
    )
    site, logo, _, cover = _site_seo_assets(request)
    image = ""
    if post.cover_image:
        image = absolute_media_url(request, post.cover_image)
    ctx = share_context_from_entity(
        meta_title=post.seo_title or "",
        display_title=post.title,
        meta_description=post.seo_description or "",
        excerpt=post.excerpt or "",
        body=post.content or "",
        spa_path=f"/blog/{post.slug}",
        share_api_path=request.build_absolute_uri(f"/api/website/blog-posts/{post.slug}/share/"),
        og_type="article",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
    ctx["canonical_spa_url"] = spa_url(f"/blog/{post.slug}")
    ctx["site_name"] = site.site_name
    return _share_response(request, ctx)


@api_view(["GET"])
@permission_classes([AllowAny])
def cms_page_share(request, slug):
    row = get_object_or_404(CMSPage, slug=slug, status=CMSPage.Status.PUBLISHED)
    site, logo, _, cover = _site_seo_assets(request)
    image = absolute_media_url(request, row.featured_image) if row.featured_image else ""
    ctx = share_context_from_entity(
        meta_title=row.seo_title or "",
        display_title=row.title,
        meta_description=row.seo_description or "",
        excerpt="",
        body=row.content or "",
        spa_path=f"/page/{row.slug}",
        share_api_path=request.build_absolute_uri(f"/api/website/cms-pages/{slug}/share/"),
        og_type="website",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
    ctx["canonical_spa_url"] = spa_url(f"/page/{row.slug}")
    ctx["site_name"] = site.site_name
    return _share_response(request, ctx)


@api_view(["GET"])
@permission_classes([AllowAny])
def product_share(request, identifier):
    qs = _active_products_queryset().prefetch_related("images")
    row = qs.filter(slug=identifier).first()
    if not row:
        row = get_object_or_404(qs, pk=identifier)
    site, logo, _, cover = _site_seo_assets(request)
    image = ensure_https_og_image(product_primary_image_url(request, row))
    slug = row.slug or str(row.pk)
    ctx = share_context_from_entity(
        meta_title=row.seo_title or "",
        display_title=row.name,
        meta_description=row.seo_description or "",
        excerpt=row.short_description or "",
        body=row.description or "",
        spa_path=f"/product/{slug}",
        share_api_path=request.build_absolute_uri(f"/api/website/products/{slug}/share/"),
        og_type="product",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
    ctx["canonical_spa_url"] = spa_url(f"/product/{slug}")
    ctx["site_name"] = site.site_name
    return _share_response(request, ctx)
