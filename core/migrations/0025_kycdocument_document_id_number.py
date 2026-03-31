from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_flashdeal_vendor"),
    ]

    operations = [
        migrations.AddField(
            model_name="kycdocument",
            name="document_id_number",
            field=models.CharField(blank=True, max_length=100),
        ),
    ]
