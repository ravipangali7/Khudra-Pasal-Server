from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from django.db.models import F

from core.models import Product, Reel, ReelInteraction, ReelView


@transaction.atomic
def on_reel_approved(reel: Reel) -> None:
    if reel.status != Reel.Status.APPROVED:
        return
    if reel.product_id:
        p = Product.objects.filter(pk=reel.product_id).only("enable_reels").first()
        if p and p.enable_reels:
            Reel.objects.filter(pk=reel.pk).update(status=Reel.Status.ACTIVE)
    else:
        Reel.objects.filter(pk=reel.pk).update(status=Reel.Status.ACTIVE)


@transaction.atomic
def record_interaction(interaction: ReelInteraction) -> None:
    field_map = {
        ReelInteraction.Type.LIKE: "likes",
        ReelInteraction.Type.SHARE: "shares",
        ReelInteraction.Type.BOOKMARK: "bookmarks",
        ReelInteraction.Type.CART_ADD: "cart_adds",
    }
    field = field_map.get(interaction.type)
    if not field:
        return
    Reel.objects.filter(pk=interaction.reel_id).update(**{field: F(field) + 1})


@transaction.atomic
def remove_interaction_counter(reel: Reel, interaction_type: str) -> None:
    field_map = {
        ReelInteraction.Type.LIKE: "likes",
        ReelInteraction.Type.SHARE: "shares",
        ReelInteraction.Type.BOOKMARK: "bookmarks",
        ReelInteraction.Type.CART_ADD: "cart_adds",
    }
    field = field_map.get(interaction_type)
    if not field:
        return
    Reel.objects.filter(pk=reel.pk, **{f"{field}__gt": 0}).update(**{field: F(field) - 1})


@transaction.atomic
def record_unique_view(
    reel: Reel,
    user,
    request=None,
    *,
    watch_seconds: int | None = None,
    quick_skip: bool = False,
    watch_completed: bool = False,
) -> tuple[bool, int]:
    """Authenticated users: one counted view per user per reel. Anonymous: one per client IP per 24h (cache)."""
    if getattr(user, "is_authenticated", False):
        view, created = ReelView.objects.get_or_create(reel=reel, user=user)
        update_fields: list[str] = []
        if watch_seconds is not None:
            prev = view.watch_seconds or 0
            view.watch_seconds = max(prev, int(watch_seconds))
            update_fields.append("watch_seconds")
        if quick_skip:
            view.quick_skip = True
            update_fields.append("quick_skip")
        if watch_completed:
            view.watch_completed = True
            update_fields.append("watch_completed")
        if update_fields:
            view.save(update_fields=update_fields)
        if created:
            Reel.objects.filter(pk=reel.pk).update(views=F("views") + 1)
        latest_views = Reel.objects.filter(pk=reel.pk).values_list("views", flat=True).first() or 0
        return created, int(latest_views)

    latest_views = int(Reel.objects.filter(pk=reel.pk).values_list("views", flat=True).first() or 0)
    if request is None:
        return False, latest_views

    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    ip = forwarded or request.META.get("REMOTE_ADDR") or "unknown"
    cache_key = f"reel_view_anon:{reel.pk}:{ip}"
    if cache.get(cache_key):
        return False, latest_views
    cache.set(cache_key, 1, timeout=86400)
    Reel.objects.filter(pk=reel.pk).update(views=F("views") + 1)
    latest_views = int(Reel.objects.filter(pk=reel.pk).values_list("views", flat=True).first() or 0)
    return True, latest_views
