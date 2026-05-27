"""Seed singleton site / wallet settings and default payment gateway rows."""
from django.core.management.base import BaseCommand

from core.models import OrderSettings, PaymentGatewaySettings, SiteSettings, WalletSettings


class Command(BaseCommand):
    help = "Create SiteSettings, WalletSettings, and default PaymentGatewaySettings rows."

    def handle(self, *args, **options):
        site, _ = SiteSettings.objects.get_or_create(
            pk=1,
            defaults={
                "site_name": "Khudra Pasal",
                "site_email": "hello@khudrapasal.com",
                "phone": "+977-1-4444444",
                "address": "Kathmandu, Nepal",
                "currency": "NPR",
                "timezone": "Asia/Kathmandu",
                "site_description": "Your neighbourhood digital store.",
                "footer_text": "© KhudraPasal. All rights reserved.",
            },
        )
        default_placeholders = [
            "dal",
            "rice",
            "cafe items",
            "amul butter",
            "banana",
            "shampoo",
            "mobile charger",
            "bedsheet",
        ]
        if not site.search_placeholders:
            site.search_placeholders = default_placeholders
            site.save(update_fields=["search_placeholders"])
        self.stdout.write(self.style.SUCCESS(f"Site settings: pk={site.pk}"))

        wallet, _ = WalletSettings.objects.get_or_create(pk=1)
        self.stdout.write(self.style.SUCCESS(f"Wallet settings: pk={wallet.pk}"))

        order_s, _ = OrderSettings.objects.get_or_create(pk=1)
        self.stdout.write(self.style.SUCCESS(f"Order settings: pk={order_s.pk}"))

        gateways = [
            (
                PaymentGatewaySettings.Gateway.ESEWA,
                {"is_enabled": False, "environment": PaymentGatewaySettings.Environment.TEST},
            ),
            (
                PaymentGatewaySettings.Gateway.KHALTI,
                {"is_enabled": False, "environment": PaymentGatewaySettings.Environment.TEST},
            ),
            (
                PaymentGatewaySettings.Gateway.COD,
                {"is_enabled": True, "environment": PaymentGatewaySettings.Environment.LIVE},
            ),
            (
                PaymentGatewaySettings.Gateway.CONNECTIPS,
                {
                    "is_enabled": False,
                    "environment": PaymentGatewaySettings.Environment.LIVE,
                    "gateway_extras": {
                        "base_url": "https://login.connectips.com",
                        "minimum_payment_amount": "100.00",
                    },
                },
            ),
            (
                PaymentGatewaySettings.Gateway.NCHL_QR,
                {
                    "is_enabled": False,
                    "environment": PaymentGatewaySettings.Environment.TEST,
                    "gateway_extras": {"demo_mode": True, "currency": "NPR"},
                },
            ),
        ]
        for gw, extra in gateways:
            obj, created = PaymentGatewaySettings.objects.get_or_create(
                gateway=gw,
                defaults=extra,
            )
            status = "created" if created else "exists"
            self.stdout.write(f"  {gw}: {status}")

        self.stdout.write(self.style.SUCCESS("Done."))
