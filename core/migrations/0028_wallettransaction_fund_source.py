from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_commission_settlement_platform_wallet"),
    ]

    operations = [
        migrations.AddField(
            model_name="wallettransaction",
            name="fund_source",
            field=models.CharField(
                blank=True,
                help_text="Human-readable origin of funds (e.g. Personal wallet, Child wallet). Shown in portal history.",
                max_length=200,
            ),
        ),
    ]
