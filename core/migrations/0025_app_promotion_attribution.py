from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_blogpost_seo_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppPromotionAttribution",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("visit_token", models.CharField(db_index=True, max_length=64, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("clicked", "Banner clicked"),
                            ("installed", "App install claimed"),
                            ("redeemed", "First-order discount used"),
                        ],
                        db_index=True,
                        default="clicked",
                        max_length=20,
                    ),
                ),
                ("clicked_at", models.DateTimeField(auto_now_add=True)),
                ("installed_at", models.DateTimeField(blank=True, null=True)),
                ("redeemed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "discount_percent",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0.00"),
                        help_text="Percent off merchandise on first order after install claim.",
                        max_digits=5,
                    ),
                ),
                ("banner_headline", models.CharField(blank=True, max_length=255)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=512)),
                (
                    "first_order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="core.order",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="app_promotion_attribution",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-clicked_at"],
            },
        ),
        migrations.AddField(
            model_name="order",
            name="app_promo_discount_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="First-order discount from app promotion banner attribution.",
                max_digits=8,
            ),
        ),
    ]
