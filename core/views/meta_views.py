"""Public SEO endpoints: settings, sitemap, share landings."""

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.models import BlogPost, Brand, Category, CMSPage, SiteSettings, Vendor
from core.seo.og_image import public_media_url, resolve_share_og_image
from core.seo.share import render_share_html, share_context_from_entity
from core.seo.catalog_export import build_catalog_csv, build_catalog_json, build_catalog_xml
from core.seo.sitemap import build_sitemap_xml
from core.views.admin.admin_write_utils import product_primary_image_url
from core.views.website.home_views import _active_products_queryset


def _site_seo_assets(request):
    site = SiteSettings.load()
    logo = public_media_url(request, file_field=site.site_logo) if site.site_logo else ""
    favicon = (
        public_media_url(request, file_field=site.site_favicon)
        if getattr(site, "site_favicon", None) and site.site_favicon
        else ""
    )
    cover = (
        public_media_url(request, file_field=site.cover_image)
        if getattr(site, "cover_image", None) and site.cover_image
        else ""
    )
    return site, logo, favicon, cover or logo


def _share_response(request, ctx: dict) -> HttpResponse:
    html = render_share_html(
        title=ctx["title"],
        description=ctx["description"],
        canonical_spa_url=ctx["canonical_spa_url"],
        og_type=ctx["og_type"],
        og_image=ctx.get("og_image") or "",
        site_name=ctx.get("site_name") or "",
    )
    return HttpResponse(html, content_type="text/html; charset=utf-8")


@api_view(["GET"])
@permission_classes([AllowAny])
def public_settings(request):
    site, logo, favicon, cover = _site_seo_assets(request)
    return Response(
        {
            "siteName": site.site_name,
            "siteMetaDescription": site.site_description or "",
            "siteLogo": logo,
            "siteFavicon": favicon,
            "coverImage": cover,
        }
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def sitemap_xml(request):
    xml = build_sitemap_xml()
    return HttpResponse(xml, content_type="application/xml; charset=utf-8")


@api_view(["GET"])
@permission_classes([AllowAny])
def catalog_export_json(request):
    return HttpResponse(build_catalog_json(), content_type="application/json; charset=utf-8")


@api_view(["GET"])
@permission_classes([AllowAny])
def catalog_export_csv(request):
    return HttpResponse(build_catalog_csv(), content_type="text/csv; charset=utf-8")


@api_view(["GET"])
@permission_classes([AllowAny])
def catalog_export_xml(request):
    return HttpResponse(build_catalog_xml(), content_type="application/xml; charset=utf-8")


@api_view(["GET"])
@permission_classes([AllowAny])
def blog_post_share(request, slug):
    post = get_object_or_404(
        BlogPost.objects.select_related("author"),
        slug=slug,
        status=BlogPost.Status.PUBLISHED,
    )
    site, logo, _, cover = _site_seo_assets(request)
    image = public_media_url(request, file_field=post.cover_image) if post.cover_image else ""
    ctx = share_context_from_entity(
        request=request,
        meta_title=post.seo_title or "",
        display_title=post.title,
        meta_description=post.seo_description or "",
        excerpt=post.excerpt or "",
        body=post.content or "",
        spa_path=f"/blog/{post.slug}",
        og_type="article",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
    ctx["site_name"] = site.site_name
    return _share_response(request, ctx)


@api_view(["GET"])
@permission_classes([AllowAny])
def cms_page_share(request, slug):
    row = get_object_or_404(CMSPage, slug=slug, status=CMSPage.Status.PUBLISHED)
    site, logo, _, cover = _site_seo_assets(request)
    image = public_media_url(request, file_field=row.featured_image) if row.featured_image else ""
    ctx = share_context_from_entity(
        request=request,
        meta_title=row.seo_title or "",
        display_title=row.title,
        meta_description=row.seo_description or "",
        excerpt="",
        body=row.content or "",
        spa_path=f"/page/{row.slug}",
        og_type="website",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
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
    image = resolve_share_og_image(
        request,
        entity_image=product_primary_image_url(request, row),
        cover_image=cover,
        site_logo=logo,
    )
    slug = row.slug or str(row.pk)
    ctx = share_context_from_entity(
        request=request,
        meta_title=row.seo_title or "",
        display_title=row.name,
        meta_description=row.seo_description or "",
        excerpt=row.short_description or "",
        body=row.description or "",
        spa_path=f"/product/{slug}",
        og_type="product",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
    ctx["site_name"] = site.site_name
    return _share_response(request, ctx)


@api_view(["GET"])
@permission_classes([AllowAny])
def category_share(request, slug):
    row = get_object_or_404(Category, slug=slug, status=Category.Status.ACTIVE)
    site, logo, _, cover = _site_seo_assets(request)
    image = public_media_url(request, file_field=row.image) if row.image else ""
    desc = row.seo_description or f"Shop {row.name} on {site.site_name}."
    ctx = share_context_from_entity(
        request=request,
        meta_title=row.seo_title or "",
        display_title=row.name,
        meta_description=desc,
        excerpt=desc,
        body="",
        spa_path=f"/category/{row.slug}",
        og_type="website",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
    ctx["site_name"] = site.site_name
    return _share_response(request, ctx)


@api_view(["GET"])
@permission_classes([AllowAny])
def brand_share(request, brand_id):
    row = get_object_or_404(Brand, pk=brand_id, status=Brand.Status.ACTIVE)
    site, logo, _, cover = _site_seo_assets(request)
    image = public_media_url(request, file_field=row.logo) if row.logo else ""
    desc = f"Shop {row.name} on {site.site_name}."
    ctx = share_context_from_entity(
        request=request,
        meta_title="",
        display_title=row.name,
        meta_description=desc,
        excerpt=desc,
        body="",
        spa_path=f"/brands/{row.pk}",
        og_type="website",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
    ctx["site_name"] = site.site_name
    return _share_response(request, ctx)


@api_view(["GET"])
@permission_classes([AllowAny])
def vendor_store_share(request, slug):
    vendor = get_object_or_404(
        Vendor,
        store_slug=slug,
        status=Vendor.Status.APPROVED,
    )
    site, logo, _, cover = _site_seo_assets(request)
    image = ""
    if vendor.logo:
        image = public_media_url(request, file_field=vendor.logo)
    elif vendor.banner:
        image = public_media_url(request, file_field=vendor.banner)
    desc = (vendor.description or "").strip() or f"Shop at {vendor.store_name} on {site.site_name}."
    ctx = share_context_from_entity(
        request=request,
        meta_title="",
        display_title=vendor.store_name,
        meta_description=desc,
        excerpt=desc[:160],
        body=vendor.description or "",
        spa_path=f"/store/{vendor.store_slug}",
        og_type="website",
        entity_image=image,
        site_name=site.site_name,
        site_description=site.site_description or "",
        site_logo=logo,
        cover_image=cover,
    )
    ctx["site_name"] = site.site_name
    return _share_response(request, ctx)
