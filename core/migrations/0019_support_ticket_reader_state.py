# Generated manually for support ticket read state

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_user_social_avatar_url"),
    ]

    operations = [
        migrations.CreateModel(
            name="SupportTicketReaderState",
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
                ("last_read_at", models.DateTimeField()),
                (
                    "reader",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="support_ticket_reader_states",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reader_states",
                        to="core.supportticket",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="supportticketreaderstate",
            constraint=models.UniqueConstraint(
                fields=("ticket", "reader"),
                name="uniq_support_ticket_reader_state_ticket_reader",
            ),
        ),
    ]
