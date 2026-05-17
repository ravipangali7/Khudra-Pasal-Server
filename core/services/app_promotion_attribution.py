"""App promotion banner clicks, install claims, and first-order discount."""

from __future__ import annotations

import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import AppPromotionAttribution, Order, User
from core.services.app_promotion_banner import (
    banner_discount_percent,
    public_app_promotion_banner_from_site,
)
from core.services.portal_checkout_pricing import storefront_merchandise_subtotal
from core.services.coupon_validation import split_discount_across_sellers

VISIT_TOKEN_COOKIE = "kp_app_promo"
VISIT_TOKEN_HEADER = "HTTP_X_APP_PROMO_TOKEN"


def _new_visit_token() -> str:
    return secrets.token_urlsafe(24)[:48]


def _percent_from_banner() -> Decimal:
    return banner_discount_percent()


def _client_meta(request) -> tuple[str | None, str]:
    ip = None
    if request is not None:
        xff = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
        ip = xff or request.META.get("REMOTE_ADDR")
    ua = (request.META.get("HTTP_USER_AGENT") or "")[:512] if request else ""
    return ip, ua


def visit_token_from_request(request) -> str:
    if request is None:
        return ""
    hdr = (request.META.get(VISIT_TOKEN_HEADER) or "").strip()
    if hdr:
        return hdr
    return (request.COOKIES.get(VISIT_TOKEN_COOKIE) or "").strip()


def append_play_store_referrer(store_url: str, visit_token: str) -> str:
    if not store_url or store_url == "#" or not visit_token:
        return store_url
    from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse

    parsed = urlparse(store_url)
    q = parse_qs(parsed.query, keep_blank_values=True)
    q["referrer"] = [quote(f"kp_token={visit_token}", safe="")]
    new_query = urlencode(q, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def record_banner_click(
    request,
    *,
    user: User | None = None,
    visit_token: str | None = None,
) -> dict[str, Any]:
    """Record a banner CTA click; returns visit_token for client storage."""
    from core.models import SiteSettings

    banner = public_app_promotion_banner_from_site(SiteSettings.load())
    if not banner:
        return {"ok": False, "detail": "Banner is not configured."}

    pct = _percent_from_banner()
    headline = banner.get("headline") or ""
    ip, ua = _client_meta(request)
    token = (visit_token or "").strip() or _new_visit_token()

    with transaction.atomic():
        attr: AppPromotionAttribution | None = None
        if user is not None:
            attr, _ = AppPromotionAttribution.objects.select_for_update().get_or_create(
                user=user,
                defaults={
                    "visit_token": token,
                    "discount_percent": pct,
                    "banner_headline": headline,
                    "ip_address": ip,
                    "user_agent": ua,
                },
            )
            if attr.visit_token != token:
                attr.visit_token = token
            attr.discount_percent = pct
            attr.banner_headline = headline
            if attr.status == AppPromotionAttribution.Status.REDEEMED:
                pass
            else:
                attr.status = AppPromotionAttribution.Status.CLICKED
                attr.clicked_at = timezone.now()
            attr.save()
        else:
            attr = (
                AppPromotionAttribution.objects.select_for_update()
                .filter(visit_token=token)
                .first()
            )
            if attr is None:
                attr = AppPromotionAttribution.objects.create(
                    visit_token=token,
                    discount_percent=pct,
                    banner_headline=headline,
                    ip_address=ip,
                    user_agent=ua,
                )
            else:
                attr.discount_percent = pct
                attr.banner_headline = headline
                if attr.status != AppPromotionAttribution.Status.REDEEMED:
                    attr.status = AppPromotionAttribution.Status.CLICKED
                    attr.clicked_at = timezone.now()
                attr.save()

    return {
        "ok": True,
        "visit_token": attr.visit_token,
        "discount_percent": float(attr.discount_percent),
    }


def merge_visit_token_to_user(user: User, visit_token: str) -> None:
    token = (visit_token or "").strip()
    if not token or not user.pk:
        return
    with transaction.atomic():
        anon = (
            AppPromotionAttribution.objects.select_for_update()
            .filter(visit_token=token, user__isnull=True)
            .first()
        )
        existing = (
            AppPromotionAttribution.objects.select_for_update()
            .filter(user=user)
            .first()
        )
        if anon and existing and anon.pk != existing.pk:
            if existing.status == AppPromotionAttribution.Status.REDEEMED:
                anon.delete()
                return
            for field in (
                "status",
                "installed_at",
                "discount_percent",
                "banner_headline",
            ):
                av = getattr(anon, field)
                if av and not getattr(existing, field):
                    setattr(existing, field, av)
            if anon.status == AppPromotionAttribution.Status.INSTALLED and (
                existing.status == AppPromotionAttribution.Status.CLICKED
            ):
                existing.status = AppPromotionAttribution.Status.INSTALLED
                existing.installed_at = anon.installed_at
            existing.save()
            anon.delete()
            return
        if anon and not existing:
            anon.user = user
            anon.save(update_fields=["user"])
            return
        if existing and not anon:
            return
        if anon and existing and anon.pk == existing.pk:
            if existing.user_id is None:
                existing.user = user
                existing.save(update_fields=["user"])


def claim_app_install(user: User, visit_token: str | None = None) -> dict[str, Any]:
    token = (visit_token or "").strip()
    with transaction.atomic():
        attr: AppPromotionAttribution | None = None
        if token:
            merge_visit_token_to_user(user, token)
            attr = (
                AppPromotionAttribution.objects.select_for_update()
                .filter(Q(user=user) | Q(visit_token=token))
                .order_by("-clicked_at")
                .first()
            )
        else:
            attr = (
                AppPromotionAttribution.objects.select_for_update()
                .filter(user=user)
                .first()
            )
        if attr is None:
            return {"ok": False, "detail": "No banner attribution for this account."}
        if attr.user_id is None:
            attr.user = user
        if attr.status == AppPromotionAttribution.Status.REDEEMED:
            return {"ok": True, "status": attr.status, "already_redeemed": True}
        attr.status = AppPromotionAttribution.Status.INSTALLED
        if not attr.installed_at:
            attr.installed_at = timezone.now()
        attr.save()
    return {
        "ok": True,
        "status": attr.status,
        "discount_percent": float(attr.discount_percent),
    }


def merge_attribution_from_request(user: User, request) -> None:
    if not user or not user.pk or request is None:
        return
    token = visit_token_from_request(request)
    if token:
        merge_visit_token_to_user(user, token)


def user_has_prior_orders(user: User) -> bool:
    return Order.objects.filter(customer=user).exclude(
        status=Order.Status.CANCELLED
    ).exists()


def get_redeemable_attribution(user: User) -> AppPromotionAttribution | None:
    if not user or not user.pk:
        return None
    attr = (
        AppPromotionAttribution.objects.filter(user=user)
        .exclude(status=AppPromotionAttribution.Status.REDEEMED)
        .first()
    )
    if attr is None:
        return None
    if attr.status != AppPromotionAttribution.Status.INSTALLED:
        return None
    if user_has_prior_orders(user):
        return None
    pct = attr.discount_percent or Decimal("0")
    if pct <= 0:
        return None
    return attr


def compute_app_promo_discount_split(
    user: User,
    groups: dict,
    coupon_discount_total: Decimal,
    seller_discounts: dict[int | None, Decimal],
) -> tuple[Decimal, dict[int | None, Decimal]]:
    attr = get_redeemable_attribution(user)
    if attr is None:
        return Decimal("0"), {sid: Decimal("0") for sid in groups}

    merch = storefront_merchandise_subtotal(groups)
    after_coupon = (merch - coupon_discount_total).quantize(Decimal("0.01"))
    if after_coupon < 0:
        after_coupon = Decimal("0")
    if after_coupon <= 0:
        return Decimal("0"), {sid: Decimal("0") for sid in groups}

    total_disc = (after_coupon * attr.discount_percent / Decimal("100")).quantize(
        Decimal("0.01")
    )
    if total_disc <= 0:
        return Decimal("0"), {sid: Decimal("0") for sid in groups}

    seller_eligible: dict[int | None, Decimal] = {}
    for sid, glines in groups.items():
        line_tot = sum(lt for *_rest, lt in glines)
        coup = seller_discounts.get(sid, Decimal("0"))
        rem = (line_tot - coup).quantize(Decimal("0.01"))
        if rem > 0:
            seller_eligible[sid] = rem

    split = split_discount_across_sellers(total_disc, seller_eligible)
    return total_disc, split


def mark_attribution_redeemed(user: User, first_order: Order) -> None:
    attr = (
        AppPromotionAttribution.objects.filter(user=user)
        .exclude(status=AppPromotionAttribution.Status.REDEEMED)
        .first()
    )
    if attr is None:
        return
    now = timezone.now()
    attr.status = AppPromotionAttribution.Status.REDEEMED
    attr.redeemed_at = now
    attr.first_order = first_order
    attr.save(update_fields=["status", "redeemed_at", "first_order"])


def attribution_admin_row(attr: AppPromotionAttribution) -> dict[str, Any]:
    u = attr.user
    return {
        "id": attr.pk,
        "visit_token": attr.visit_token,
        "user_id": u.pk if u else None,
        "user_name": u.name if u else "",
        "user_phone": u.phone if u else "",
        "status": attr.status,
        "clicked_at": attr.clicked_at.isoformat() if attr.clicked_at else None,
        "installed_at": attr.installed_at.isoformat() if attr.installed_at else None,
        "redeemed_at": attr.redeemed_at.isoformat() if attr.redeemed_at else None,
        "discount_percent": float(attr.discount_percent or 0),
        "banner_headline": attr.banner_headline or "",
        "first_order_id": attr.first_order_id,
        "first_order_number": attr.first_order.order_number if attr.first_order_id else "",
    }
