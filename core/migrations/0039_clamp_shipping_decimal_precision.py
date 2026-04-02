from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import migrations


def _clamp_decimal(raw_value, *, max_digits: int, decimal_places: int, allow_null: bool):
    if raw_value is None:
        return None if allow_null else str(Decimal("0").quantize(Decimal(10) ** -decimal_places))

    value_text = str(raw_value).strip()
    if value_text == "":
        return None if allow_null else str(Decimal("0").quantize(Decimal(10) ** -decimal_places))

    try:
        dec = Decimal(value_text)
    except (InvalidOperation, ValueError):
        return None if allow_null else str(Decimal("0").quantize(Decimal(10) ** -decimal_places))

    if not dec.is_finite():
        return None if allow_null else str(Decimal("0").quantize(Decimal(10) ** -decimal_places))

    quantum = Decimal(10) ** -decimal_places
    integral_digits = max_digits - decimal_places
    max_abs = (Decimal(10) ** integral_digits) - quantum

    if dec > max_abs:
        dec = max_abs
    elif dec < -max_abs:
        dec = -max_abs

    dec = dec.quantize(quantum, rounding=ROUND_HALF_UP)
    return str(dec)


def clamp_shipping_decimal_precision(apps, schema_editor):
    connection = schema_editor.connection
    targets = [
        ("core_shippingzone", "flat_rate", 8, 2, False),
        ("core_shippingzone", "free_above", 8, 2, True),
        ("core_weightrule", "min_weight", 6, 3, False),
        ("core_weightrule", "max_weight", 6, 3, False),
        ("core_weightrule", "rate_per_kg", 8, 2, False),
        ("core_deliveryman", "rating", 3, 2, False),
        ("core_deliveryman", "total_earnings", 10, 2, False),
        ("core_deliveryman", "pending_earnings", 10, 2, False),
    ]

    with connection.cursor() as cursor:
        for table_name, column_name, max_digits, decimal_places, allow_null in targets:
            cursor.execute(f"SELECT rowid, {column_name} FROM {table_name}")
            for rowid, current_value in cursor.fetchall():
                normalized = _clamp_decimal(
                    current_value,
                    max_digits=max_digits,
                    decimal_places=decimal_places,
                    allow_null=allow_null,
                )
                if str(current_value) != str(normalized):
                    cursor.execute(
                        f"UPDATE {table_name} SET {column_name} = %s WHERE rowid = %s",
                        [normalized, rowid],
                    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0038_normalize_shipping_decimal_values"),
    ]

    operations = [
        migrations.RunPython(
            clamp_shipping_decimal_precision,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
