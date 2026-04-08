# Generated manually for User.fcm_token

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_wallet_hub_transfer_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="fcm_token",
            field=models.TextField(blank=True, default=""),
        ),
    ]
