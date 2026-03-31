from __future__ import annotations

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
        ReelInteraction.Type.CART_ADD: "cart_adds",
    }
    field = field_map.get(interaction_type)
    if not field:
        return
    Reel.objects.filter(pk=reel.pk, **{f"{field}__gt": 0}).update(**{field: F(field) - 1})


@transaction.atomic
def record_unique_view(reel: Reel, user) -> tuple[bool, int]:
    _, created = ReelView.objects.get_or_create(reel=reel, user=user)
    if created:
        Reel.objects.filter(pk=reel.pk).update(views=F("views") + 1)
    latest_views = Reel.objects.filter(pk=reel.pk).values_list("views", flat=True).first() or 0
    return created, int(latest_views)
