# Multi-device FCM tokens per user

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def migrate_legacy_fcm_tokens(apps, schema_editor):
    User = apps.get_model("core", "User")
    UserFcmDevice = apps.get_model("core", "UserFcmDevice")
    for u in User.objects.exclude(fcm_token="").exclude(fcm_token__isnull=True).iterator(chunk_size=500):
        tok = (u.fcm_token or "").strip()
        if not tok or len(tok) > 8192:
            continue
        UserFcmDevice.objects.update_or_create(
            token=tok,
            defaults={"user_id": u.pk, "platform": ""},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_order_bill_notification_image"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserFcmDevice",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("token", models.CharField(db_index=True, max_length=8192, unique=True)),
                (
                    "platform",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("web", "Web"),
                            ("android", "Android"),
                            ("ios", "iOS"),
                            ("", "Unknown"),
                        ],
                        default="",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fcm_devices",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "FCM device",
                "verbose_name_plural": "FCM devices",
            },
        ),
        migrations.AddIndex(
            model_name="userfcmdevice",
            index=models.Index(fields=["user", "updated_at"], name="core_userfc_user_id_updated_idx"),
        ),
        migrations.RunPython(migrate_legacy_fcm_tokens, migrations.RunPython.noop),
    ]
