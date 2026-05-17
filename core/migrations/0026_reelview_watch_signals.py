from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_app_promotion_attribution"),
    ]

    operations = [
        migrations.AddField(
            model_name="reelview",
            name="watch_seconds",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reelview",
            name="quick_skip",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="reelview",
            name="watch_completed",
            field=models.BooleanField(default=False),
        ),
    ]
