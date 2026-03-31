from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_cart_reelcomment_cartitem"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReelView",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reel", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="unique_views", to="core.reel")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reel_views", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
                "unique_together": {("reel", "user")},
            },
        ),
    ]
