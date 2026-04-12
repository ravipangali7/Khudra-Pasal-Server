# Generated manually for aggregated bookmark counts on reels.

from django.db import migrations, models
from django.db.models import Count


def backfill_bookmarks(apps, schema_editor):
    Reel = apps.get_model("core", "Reel")
    ReelInteraction = apps.get_model("core", "ReelInteraction")
    tallies = (
        ReelInteraction.objects.filter(type="bookmark")
        .values("reel_id")
        .annotate(n=Count("id"))
    )
    for row in tallies:
        rid = row["reel_id"]
        n = row["n"]
        Reel.objects.filter(pk=rid).update(bookmarks=n)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_user_oauth_phone_completed"),
    ]

    operations = [
        migrations.AddField(
            model_name="reel",
            name="bookmarks",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.RunPython(backfill_bookmarks, noop_reverse),
    ]
