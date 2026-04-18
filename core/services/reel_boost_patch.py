"""Shared PATCH handling for reel boost fields (admin + vendor)."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional, Tuple

from django.utils import timezone

from core.models import Reel


def _truthy_boost(val) -> bool:
    if val is None:
        return False
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def apply_reel_boost_from_data(row: Reel, data) -> Optional[Tuple[str, str]]:
    """
    Mutates row when clear_boost / apply_boost are present.
    Returns (message, field) on validation error, else None.
    """
    if "clear_boost" in data and _truthy_boost(data.get("clear_boost")):
        row.is_sponsored = False
        row.boost_expires_at = None
        row.boost_expected_views = None
        row.boost_tier = ""
        row.boost_daily_budget_npr = None
        return None
    if "apply_boost" in data and _truthy_boost(data.get("apply_boost")):
        raw_dur = data.get("boost_duration_days") or data.get("duration_days")
        try:
            duration = int(raw_dur)
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            return (
                "boost_duration_days must be a positive integer",
                "boost_duration_days",
            )
        raw_ev = data.get("boost_expected_views") or data.get("expected_views")
        try:
            ev = int(raw_ev) if raw_ev not in (None, "") else 0
        except (TypeError, ValueError):
            return ("invalid boost_expected_views", "boost_expected_views")
        if ev < 0:
            return ("invalid boost_expected_views", "boost_expected_views")
        tier = (data.get("boost_tier") or "").strip()
        valid_tiers = {c[0] for c in Reel.BoostTier.choices}
        if not tier:
            tier = Reel.BoostTier.STANDARD
        elif tier not in valid_tiers:
            return ("invalid boost_tier", "boost_tier")
        row.is_sponsored = True
        row.boost_expires_at = timezone.now() + timedelta(days=duration)
        row.boost_expected_views = ev if ev > 0 else None
        row.boost_tier = tier
        bud = data.get("boost_daily_budget_npr")
        if bud not in (None, ""):
            try:
                b = int(bud)
            except (TypeError, ValueError):
                return ("invalid boost_daily_budget_npr", "boost_daily_budget_npr")
            if b < 0:
                return ("invalid boost_daily_budget_npr", "boost_daily_budget_npr")
            row.boost_daily_budget_npr = b if b > 0 else None
        else:
            row.boost_daily_budget_npr = None
    return None
