# Product: replace discount_price with discount_type + discount

from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations, models


def _eff_from_row(price, dtype, disc):
    if not dtype or disc is None or disc <= 0:
        return price
    if dtype == "percentage":
        if disc >= 100:
            return Decimal("0.00").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        eff = price * (Decimal(100) - disc) / Decimal(100)
        return eff.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if dtype == "flat":
        eff = price - disc
        if eff < 0:
            return Decimal("0.00").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return eff.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return price


def forwards_copy_discount_price(apps, schema_editor):
    Product = apps.get_model("core", "Product")
    for p in Product.objects.all():
        dp = p.discount_price
        if dp is not None and p.price is not None and dp < p.price:
            p.discount_type = "flat"
            p.discount = p.price - dp
            p.save(update_fields=["discount_type", "discount"])


def backwards_restore_discount_price(apps, schema_editor):
    Product = apps.get_model("core", "Product")
    for p in Product.objects.all():
        eff = _eff_from_row(p.price, (p.discount_type or "").strip(), p.discount)
        p.discount_price = eff
        p.save(update_fields=["discount_price"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_purchaseapprovalrequest_consumed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="discount_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("flat", "Flat (amount off list price)"),
                    ("percentage", "Percentage off list price"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="discount",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True
            ),
        ),
        migrations.RunPython(forwards_copy_discount_price, backwards_restore_discount_price),
        migrations.RemoveField(
            model_name="product",
            name="discount_price",
        ),
    ]
