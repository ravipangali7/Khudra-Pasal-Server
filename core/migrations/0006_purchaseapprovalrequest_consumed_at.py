# Generated manually for purchase approval consumption tracking

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_walletwithdrawal_proof_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseapprovalrequest",
            name="consumed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Set when the child completes checkout using this approval (single-use).",
                null=True,
            ),
        ),
    ]
