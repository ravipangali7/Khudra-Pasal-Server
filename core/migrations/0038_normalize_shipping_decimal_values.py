from decimal import Decimal, InvalidOperation

from django.db import migrations


def _normalize_decimal(value, *, allow_null: bool) -> str | None:
    if value is None:
        return None

    raw = str(value).strip()
    if raw == "":
        return None if allow_null else "0.00"

    try:
        normalized = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None if allow_null else "0.00"

    if not normalized.is_finite():
        return None if allow_null else "0.00"

    return str(normalized)


def clean_shipping_decimal_values(apps, schema_editor):
    connection = schema_editor.connection
    targets = [
        ("core_shippingzone", "flat_rate", False),
        ("core_shippingzone", "free_above", True),
        ("core_weightrule", "min_weight", False),
        ("core_weightrule", "max_weight", False),
        ("core_weightrule", "rate_per_kg", False),
        ("core_deliveryman", "rating", False),
        ("core_deliveryman", "total_earnings", False),
        ("core_deliveryman", "pending_earnings", False),
    ]

    with connection.cursor() as cursor:
        for table_name, column_name, allow_null in targets:
            cursor.execute(f"SELECT rowid, {column_name} FROM {table_name}")
            rows = cursor.fetchall()
            for rowid, current_value in rows:
                normalized = _normalize_decimal(current_value, allow_null=allow_null)
                if normalized != current_value:
                    cursor.execute(
                        f"UPDATE {table_name} SET {column_name} = %s WHERE rowid = %s",
                        [normalized, rowid],
                    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_navigation_portal_wishlist"),
    ]

    operations = [
        migrations.RunPython(
            clean_shipping_decimal_values,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
