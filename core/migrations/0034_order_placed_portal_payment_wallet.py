# Generated manually for portal-scoped orders and refund routing.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_shippingmethod_decimal_width"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="placed_portal",
            field=models.CharField(
                blank=True,
                choices=[
                    ("portal_main", "Customer portal"),
                    ("portal_family", "Family portal"),
                    ("portal_child", "Child portal"),
                ],
                db_index=True,
                help_text="Portal surface used at checkout; null = legacy (listed on main portal only).",
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="payment_wallet",
            field=models.ForeignKey(
                blank=True,
                help_text="Wallet debited for wallet checkout; used for refund credit.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="orders_paid_from",
                to="core.wallet",
            ),
        ),
    ]
