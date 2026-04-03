"""Shared shipping fee calculation (admin calculator, website quote, portal checkout)."""

from decimal import Decimal

from django.db.models import FloatField
from django.db.models.functions import Cast

from core.models import ShippingMethod, ShippingSettings, ShippingZone, WeightRule


def weight_rule_band_values_for_zone(zone: ShippingZone, weight: float):
    """
    Matching WeightRule row as floats. Using Cast avoids SQLite DecimalField converters that can
    raise InvalidOperation on some stored values.
    """
    return (
        WeightRule.objects.filter(zone=zone)
        .annotate(
            min_w=Cast("min_weight", FloatField()),
            max_w=Cast("max_weight", FloatField()),
            rpk=Cast("rate_per_kg", FloatField()),
        )
        .filter(min_w__lte=weight, max_w__gte=weight)
        .values("rpk", "min_w", "max_w")
        .order_by("min_w")
        .first()
    )


def compute_shipping_fee(
    sh: ShippingSettings,
    zone: ShippingZone,
    *,
    order_total: Decimal,
    weight_kg: float,
    method: ShippingMethod | None = None,
) -> tuple[Decimal, list]:
    """
    Returns (shipping_fee, breakdown) using the same rules as the admin shipping calculator.
    Does not apply seller_pays_shipping (callers zero out customer charge when needed).
    """
    order_total_dec = order_total
    weight = float(weight_kg or 0)
    shipping_fee = Decimal("0")
    breakdown: list = []

    if sh.free_shipping_global and order_total_dec > 0:
        shipping_fee = Decimal("0")
        breakdown.append({"step": "global_free", "amount": 0})
    else:
        if method:
            if method.type == ShippingMethod.Type.FREE:
                if order_total_dec >= method.free_threshold:
                    shipping_fee = Decimal("0")
                    breakdown.append(
                        {
                            "step": "method_free_threshold",
                            "amount": 0,
                            "threshold": float(method.free_threshold),
                        }
                    )
                else:
                    shipping_fee = method.cost
                    breakdown.append(
                        {"step": "method_free_not_met_flat", "amount": float(method.cost)}
                    )
            elif method.type == ShippingMethod.Type.FLAT:
                shipping_fee = method.cost
                breakdown.append({"step": "flat", "amount": float(method.cost)})
            elif method.type == ShippingMethod.Type.PICKUP:
                shipping_fee = Decimal("0")
                breakdown.append({"step": "pickup", "amount": 0})
            elif method.type == ShippingMethod.Type.WEIGHT:
                wr = weight_rule_band_values_for_zone(zone, weight)
                if wr:
                    w_fee = Decimal(str(weight)) * Decimal(str(wr["rpk"]))
                    shipping_fee = zone.flat_rate + w_fee
                    breakdown.append(
                        {
                            "step": "zone_flat",
                            "amount": float(zone.flat_rate),
                        }
                    )
                    breakdown.append(
                        {
                            "step": "weight_band",
                            "amount": float(w_fee),
                            "min_weight": float(wr["min_w"]),
                            "max_weight": float(wr["max_w"]),
                            "weight_kg": weight,
                            "rate_per_kg": float(wr["rpk"]),
                        }
                    )
                else:
                    shipping_fee = zone.flat_rate
                    breakdown.append(
                        {
                            "step": "zone_flat_no_weight_rule",
                            "amount": float(zone.flat_rate),
                        }
                    )
        else:
            wr = weight_rule_band_values_for_zone(zone, weight)
            if wr:
                w_fee = Decimal(str(weight)) * Decimal(str(wr["rpk"]))
                shipping_fee = zone.flat_rate + w_fee
                breakdown.append({"step": "zone_flat", "amount": float(zone.flat_rate)})
                breakdown.append(
                    {
                        "step": "weight_band",
                        "amount": float(w_fee),
                        "min_weight": float(wr["min_w"]),
                        "max_weight": float(wr["max_w"]),
                        "weight_kg": weight,
                        "rate_per_kg": float(wr["rpk"]),
                    }
                )
            else:
                shipping_fee = zone.flat_rate
                breakdown.append({"step": "zone_flat_only", "amount": float(zone.flat_rate)})

        def _subtotal_from_breakdown_so_far() -> Decimal:
            s = Decimal("0")
            for row in breakdown:
                a = row.get("amount")
                if a is not None:
                    s += Decimal(str(a))
            return s

        if zone.free_above is not None and order_total_dec >= zone.free_above:
            subtotal_before_free = _subtotal_from_breakdown_so_far()
            shipping_fee = Decimal("0")
            breakdown.append(
                {
                    "step": "zone_free_above",
                    "order_total": float(order_total_dec),
                    "free_above": float(zone.free_above),
                    "subtotal_before_free": float(subtotal_before_free),
                }
            )

    return shipping_fee, breakdown
