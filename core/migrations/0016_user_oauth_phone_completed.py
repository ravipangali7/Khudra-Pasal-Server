from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_vendor_supplier_ledger_purchase"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="oauth_phone_completed",
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text="False until OAuth sign-up completes phone verification (OTP).",
            ),
        ),
    ]
