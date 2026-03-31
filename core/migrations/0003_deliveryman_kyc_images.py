from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_navigation_and_search_placeholders"),
    ]

    operations = [
        migrations.AddField(
            model_name="deliveryman",
            name="id_document_front",
            field=models.ImageField(blank=True, upload_to="delivery/kyc/"),
        ),
        migrations.AddField(
            model_name="deliveryman",
            name="id_document_back",
            field=models.ImageField(blank=True, upload_to="delivery/kyc/"),
        ),
        migrations.AddField(
            model_name="deliveryman",
            name="selfie",
            field=models.ImageField(blank=True, upload_to="delivery/kyc/"),
        ),
    ]
