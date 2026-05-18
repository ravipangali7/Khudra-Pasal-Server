from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_user_fcm_device"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="site_favicon",
            field=models.ImageField(blank=True, upload_to="site/"),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="cover_image",
            field=models.ImageField(
                blank=True,
                help_text="Default Open Graph image when a page has no featured image.",
                upload_to="site/",
            ),
        ),
    ]
