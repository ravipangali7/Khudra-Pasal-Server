from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_unified_payout_withdrawals"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletwithdrawal",
            name="proof_image",
            field=models.ImageField(
                blank=True, null=True, upload_to="withdrawal_proofs/"
            ),
        ),
    ]
