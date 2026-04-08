# Generated manually for SMTP fields on SiteSettings

from django.db import migrations, models


def migrate_email_extras_to_smtp_fields(apps, schema_editor):
    SiteSettings = apps.get_model("core", "SiteSettings")
    for site in SiteSettings.objects.all():
        extras = dict(site.admin_extras or {})
        email = extras.pop("email", None)
        if isinstance(email, dict):
            h = email.get("smtpHost")
            if h:
                site.smtp_host = str(h)[:255]
            port = email.get("smtpPort")
            if port is not None and str(port).strip():
                try:
                    site.smtp_port = int(port)
                except (TypeError, ValueError):
                    pass
            u = email.get("user")
            if u:
                site.smtp_username = str(u)[:255]
            pw = email.get("password")
            if pw:
                site.smtp_password = str(pw)[:255]
            fn = email.get("fromName")
            if fn:
                site.smtp_from_name = str(fn)[:150]
            fe = email.get("fromEmail")
            if fe:
                site.smtp_from_email = str(fe)[:254]
        site.admin_extras = extras
        site.save()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_coupon_products_and_orderitem_discounts"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="smtp_from_email",
            field=models.EmailField(blank=True, max_length=254),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="smtp_from_name",
            field=models.CharField(blank=True, max_length=150),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="smtp_host",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="smtp_password",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="smtp_port",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="smtp_username",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.RunPython(migrate_email_extras_to_smtp_fields, noop_reverse),
    ]
