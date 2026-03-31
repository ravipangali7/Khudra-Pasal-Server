# Generated manually for default delivery address fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_family_join_request_wallet_category"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="default_area_location",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="user",
            name="default_landmark",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="user",
            name="default_google_map_link",
            field=models.URLField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="user",
            name="default_latitude",
            field=models.DecimalField(
                blank=True, decimal_places=6, max_digits=9, null=True
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="default_longitude",
            field=models.DecimalField(
                blank=True, decimal_places=6, max_digits=9, null=True
            ),
        ),
    ]
