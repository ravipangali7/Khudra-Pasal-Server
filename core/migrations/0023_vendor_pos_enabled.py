from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_paymentgatewaysettings_gateway_extras"),
    ]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="pos_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When false, this vendor cannot use the POS module (site-wide POS must also be on).",
            ),
        ),
    ]
