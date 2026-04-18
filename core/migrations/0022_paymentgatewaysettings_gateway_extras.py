from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_vendor_refund_commission_percent"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentgatewaysettings",
            name="gateway_extras",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Gateway-specific options (eSewa: form_url, status_url_base; Khalti: api_base_url).",
            ),
        ),
    ]
