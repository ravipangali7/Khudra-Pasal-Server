"""Send a test message using the same SMTP config as transactional mail."""

from django.core.management.base import BaseCommand

from core.models import SiteSettings
from core.services.mail_service import send_html_email, smtp_is_configured


class Command(BaseCommand):
    help = (
        "Verify SMTP (SiteSettings and optional KP_SMTP_* env overrides). "
        "For Gmail use an App Password with 2-Step Verification enabled."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            type=str,
            default="",
            help="Recipient (defaults to SiteSettings.site_email)",
        )

    def handle(self, *args, **options):
        site = SiteSettings.load()
        if not smtp_is_configured(site):
            self.stderr.write(
                self.style.ERROR(
                    "SMTP host not configured. Set SiteSettings SMTP fields and/or "
                    "KP_SMTP_HOST (and credentials) in the environment."
                )
            )
            return
        to = (options["to"] or "").strip() or (site.site_email or "").strip()
        if not to:
            self.stderr.write(
                self.style.ERROR("No recipient: pass --to=email or set site_email in SiteSettings.")
            )
            return
        send_html_email(
            "KhudraPasal SMTP test",
            "<p>If you received this, SMTP authentication and delivery are working.</p>",
            [to],
            site=site,
            raise_exceptions=True,
        )
        self.stdout.write(self.style.SUCCESS(f"Sent test email to {to}"))
