from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_reelview_watch_signals"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="bill_image",
            field=models.ImageField(
                blank=True,
                help_text="Auto-generated order bill (PNG) for portal customers.",
                upload_to="orders/bills/",
            ),
        ),
        migrations.AddField(
            model_name="notification",
            name="image_url",
            field=models.URLField(
                blank=True,
                help_text="Optional image for rich notifications (e.g. order bill on delivery).",
                max_length=500,
            ),
        ),
    ]
