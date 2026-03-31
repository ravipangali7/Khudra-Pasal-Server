from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_deliveryman_kyc_images"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="customer_document",
            field=models.FileField(blank=True, upload_to="customers/documents/"),
        ),
    ]
