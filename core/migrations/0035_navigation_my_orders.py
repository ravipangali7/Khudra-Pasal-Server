from django.db import migrations


def add_my_orders_nav(apps, schema_editor):
    NavigationItem = apps.get_model("core", "NavigationItem")
    rows = [
        {
            "surface": "portal_family",
            "key": "my-orders",
            "label": "My Orders",
            "icon": "ShoppingBag",
            "view_key": "my-orders",
            "parent_key": "",
            "sort_order": 36,
            "badge_key": "",
            "roles_filter": "",
        },
        {
            "surface": "portal_child",
            "key": "my-orders",
            "label": "My Orders",
            "icon": "ShoppingBag",
            "view_key": "my-orders",
            "parent_key": "",
            "sort_order": 16,
            "badge_key": "",
            "roles_filter": "",
        },
    ]
    for row in rows:
        NavigationItem.objects.update_or_create(
            surface=row["surface"],
            key=row["key"],
            defaults={
                "label": row["label"],
                "icon": row["icon"],
                "view_key": row["view_key"],
                "parent_key": row["parent_key"],
                "sort_order": row["sort_order"],
                "badge_key": row["badge_key"],
                "roles_filter": row["roles_filter"],
            },
        )


def remove_my_orders_nav(apps, schema_editor):
    NavigationItem = apps.get_model("core", "NavigationItem")
    NavigationItem.objects.filter(key="my-orders", surface__in=["portal_family", "portal_child"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_order_placed_portal_payment_wallet"),
    ]

    operations = [
        migrations.RunPython(add_my_orders_nav, remove_my_orders_nav),
    ]
