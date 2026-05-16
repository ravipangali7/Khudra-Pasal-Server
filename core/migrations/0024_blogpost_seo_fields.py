from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_vendor_pos_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="blogpost",
            name="seo_title",
            field=models.CharField(blank=True, max_length=70),
        ),
        migrations.AddField(
            model_name="blogpost",
            name="seo_description",
            field=models.TextField(blank=True, max_length=160),
        ),
    ]
