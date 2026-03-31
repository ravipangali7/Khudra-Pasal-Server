# Generated manually for family portal dynamic views

from django.db import migrations, models


FAMILY_VIEW_KEYS = {
    "dashboard": "dashboard",
    "members": "members",
    "members-list": "members",
    "members-add": "members-add",
    "members-requests": "members-requests",
    "wallets": "wallets",
    "wallets-overview": "wallets",
    "wallets-load": "wallets",
    "wallets-transfer": "wallets",
    "controls": "spending-limits",
    "controls-limits": "spending-limits",
    "controls-restrictions": "product-restrictions",
    "controls-auto-approval": "auto-approval",
    "history": "history",
    "profile": "profile",
    "settings": "settings",
}


def backfill_family_view_keys(apps, schema_editor):
    NavigationItem = apps.get_model("core", "NavigationItem")
    for key, vk in FAMILY_VIEW_KEYS.items():
        NavigationItem.objects.filter(surface="portal_family", key=key).update(view_key=vk)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0018_family_wallet_category_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="navigationitem",
            name="view_key",
            field=models.SlugField(
                blank=True,
                default="",
                help_text="Frontend screen id (empty = same as key). URL segment stays `key`.",
                max_length=80,
            ),
        ),
        migrations.RunPython(backfill_family_view_keys, noop_reverse),
    ]
