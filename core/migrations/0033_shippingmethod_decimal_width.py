from django.db import migrations, models
from decimal import Decimal


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0032_banner_add_small_footer_placements"),
    ]

    operations = [
        migrations.AlterField(
            model_name="shippingmethod",
            name="cost",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=12
            ),
        ),
        migrations.AlterField(
            model_name="shippingmethod",
            name="free_threshold",
            field=models.DecimalField(
                decimal_places=2, default=Decimal("0.00"), max_digits=12
            ),
        ),
    ]
