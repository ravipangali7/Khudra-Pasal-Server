from django.core.management.base import BaseCommand

from core.services import marketing_service


class Command(BaseCommand):
    help = "Update FlashDeal and Banner status from schedule (run via cron/Celery)."

    def handle(self, *args, **options):
        n_deals = marketing_service.refresh_flash_deal_statuses()
        n_banners = marketing_service.refresh_banner_statuses()
        self.stdout.write(
            self.style.SUCCESS(
                f"Updated flash deals: {n_deals}, banners: {n_banners} rows."
            )
        )
