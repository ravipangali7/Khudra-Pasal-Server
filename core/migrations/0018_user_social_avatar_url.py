# Generated manually for Google/Facebook profile picture URLs (OAuth userinfo).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_reel_bookmarks_count"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="social_avatar_url",
            field=models.URLField(
                blank=True,
                default="",
                max_length=512,
                help_text="Profile image URL from the OAuth provider (e.g. Google picture).",
            ),
        ),
    ]
