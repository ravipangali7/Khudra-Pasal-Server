from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0012_reelview"),
    ]

    operations = [
        migrations.AddField(
            model_name="reel",
            name="boost_expected_views",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="reel",
            name="boost_tier",
            field=models.CharField(
                blank=True,
                choices=[
                    ("standard", "Standard"),
                    ("premium", "Premium"),
                    ("mega", "Mega"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="reel",
            name="boost_daily_budget_npr",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
