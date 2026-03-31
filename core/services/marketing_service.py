from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import Banner, FlashDeal


@transaction.atomic
def refresh_flash_deal_statuses() -> int:
    now = timezone.now()
    n = 0
    n += FlashDeal.objects.filter(
        status=FlashDeal.Status.SCHEDULED,
        start_at__lte=now,
        end_at__gt=now,
    ).update(status=FlashDeal.Status.ACTIVE)
    n += FlashDeal.objects.filter(
        status__in=[FlashDeal.Status.SCHEDULED, FlashDeal.Status.ACTIVE],
        end_at__lte=now,
    ).update(status=FlashDeal.Status.EXPIRED)
    return n


@transaction.atomic
def refresh_banner_statuses() -> int:
    today = timezone.now().date()
    n = 0
    n += Banner.objects.filter(
        status=Banner.Status.SCHEDULED,
        start_date__isnull=False,
        start_date__lte=today,
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).update(
        status=Banner.Status.ACTIVE
    )
    n += Banner.objects.filter(
        status=Banner.Status.ACTIVE,
        end_date__isnull=False,
        end_date__lt=today,
    ).update(status=Banner.Status.EXPIRED)
    return n
