"""Reverse geocoding via OSM Nominatim (server-side only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
# Nominatim requires a valid User-Agent identifying the application.
USER_AGENT = "KhudraPasalPortal/1.0"


class NominatimError(Exception):
    """Reverse geocoding failed or returned unusable data."""


def reverse_geocode(lat: float, lon: float, timeout: int = 12) -> dict:
    """Call Nominatim reverse API; returns parsed JSON object."""
    params = urllib.parse.urlencode(
        {
            "lat": lat,
            "lon": lon,
            "format": "json",
            "addressdetails": "1",
        }
    )
    url = f"{NOMINATIM_REVERSE}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise NominatimError(f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise NominatimError(str(e)) from e
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise NominatimError("invalid JSON") from e
    if not isinstance(data, dict) or data.get("error"):
        raise NominatimError(data.get("error", "no result"))
    return data


def area_and_landmark_from_nominatim(data: dict) -> tuple[str, str]:
    """
    Derive area/location label and optional nearby landmark from Nominatim JSON.
    """
    addr = data.get("address") or {}
    if not isinstance(addr, dict):
        addr = {}

    area = ""
    for key in (
        "neighbourhood",
        "suburb",
        "quarter",
        "city_district",
        "residential",
        "village",
        "town",
        "city",
        "municipality",
        "county",
    ):
        v = addr.get(key)
        if v and isinstance(v, str) and v.strip():
            area = v.strip()[:255]
            break

    display = (data.get("display_name") or "").strip()
    if not area and display:
        area = display.split(",")[0].strip()[:255]

    landmark_parts: list[str] = []
    road = addr.get("road") or addr.get("pedestrian") or addr.get("footway")
    if road and isinstance(road, str) and road.strip():
        landmark_parts.append(road.strip())
    for key in ("amenity", "shop", "building", "tourism", "leisure"):
        v = addr.get(key)
        if v and isinstance(v, str) and v.strip():
            landmark_parts.append(v.strip())
            break
    landmark = " · ".join(landmark_parts)[:255] if landmark_parts else ""

    return area, landmark
