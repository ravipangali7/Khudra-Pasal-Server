from django.db import migrations


def add_portal_wishlist_nav(apps, schema_editor):
    NavigationItem = apps.get_model("core", "NavigationItem")
    NavigationItem.objects.update_or_create(
        surface="portal_main",
        key="wishlist",
        defaults={
            "label": "Wishlist",
            "icon": "Heart",
            "view_key": "",
            "parent_key": "",
            "sort_order": 37,
            "badge_key": "",
            "roles_filter": "",
        },
    )


def remove_portal_wishlist_nav(apps, schema_editor):
    NavigationItem = apps.get_model("core", "NavigationItem")
    NavigationItem.objects.filter(surface="portal_main", key="wishlist").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0036_productwishlist"),
    ]

    operations = [
        migrations.RunPython(add_portal_wishlist_nav, remove_portal_wishlist_nav),
    ]
