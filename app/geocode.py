"""
Turn an address or zip into coordinates, then measure distance.

Two paths (always tried in order):
  1. AWS Location Service when an API key is configured.
  2. Offline zip-code centroids (the `zipcodes` package) as fallback.

Either way we cache results so we geocode each place only once.
Distance is a straight-line haversine in miles, which is plenty accurate
for "is this patient inside the provider's service area" decisions.
"""
import math

import requests

from . import config

_cache: dict[str, tuple[float, float] | None] = {}


def _haversine_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _zip_centroid(zipcode: str) -> tuple[float, float] | None:
    if not zipcode:
        return None
    try:
        import zipcodes
        hit = zipcodes.matching(zipcode.strip()[:5])
        if hit:
            return float(hit[0]["lat"]), float(hit[0]["long"])
    except Exception:
        return None
    return None


def _aws_geocode(text: str) -> tuple[float, float] | None:
    """AWS Location Geocode API (the modern v2 Places API).

    No place index needed: a v1.public API key calls the geocode endpoint
    directly. The key must belong to the region in AWS_REGION, and Position
    comes back as [longitude, latitude].
    """
    if not config.AWS_LOCATION_API_KEY:
        return None
    url = (f"https://places.geo.{config.AWS_REGION}.amazonaws.com"
           f"/v2/geocode?key={config.AWS_LOCATION_API_KEY}")
    try:
        resp = requests.post(url, json={"QueryText": text, "MaxResults": 1,
                                        "Filter": {"IncludeCountries": ["USA"]}}, timeout=8)
        resp.raise_for_status()
        items = resp.json().get("ResultItems", [])
        if items:
            lon, lat = items[0]["Position"]            # [lon, lat]
            return float(lat), float(lon)
    except Exception:
        return None
    return None


def geocode(address: str = "", zipcode: str = "", state: str = "") -> tuple[float, float] | None:
    """Best available coordinates for a place. Returns (lat, lon) or None."""
    key = f"{address}|{zipcode}|{state}".lower()
    if key in _cache:
        return _cache[key]

    coords = None
    if config.AWS_LOCATION_API_KEY:
        query = address.strip() if address else ""
        if not query and zipcode:
            query = f"{zipcode} {state}".strip()
        if query:
            coords = _aws_geocode(query)
    if coords is None:
        coords = _zip_centroid(zipcode)
    _cache[key] = coords
    return coords


def distance_miles(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float | None:
    if a is None or b is None:
        return None
    return _haversine_mi(a, b)
