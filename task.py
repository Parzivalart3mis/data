"""Provider-neutral distance enrichment helpers for organization responses.

The Lambda can pass its existing database records and an optional geocoder/cache
adapter to :func:`enrich_organizations`. No viewer location or default location
is used. Coordinates in ``req_loc`` and ``curr_loc`` take precedence over the
beneficiary profile address.
"""

from __future__ import annotations

import math
import json
import os
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

Geocoder = Callable[[str], tuple[float, float] | Mapping[str, Any] | None]


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def coordinates(value: Any) -> tuple[float, float] | None:
    """Read (latitude, longitude) from common DB/API location representations."""
    lat: Any = None
    lon: Any = None
    if isinstance(value, Mapping):
        lat = value.get("latitude", value.get("lat"))
        lon = value.get("longitude", value.get("lon", value.get("lng")))
        # PostGIS GeoJSON uses [longitude, latitude].
        if (lat is None or lon is None) and isinstance(value.get("coordinates"), Sequence):
            pair = value["coordinates"]
            if len(pair) >= 2:
                lon, lat = pair[0], pair[1]
    elif isinstance(value, str):
        # Supports e.g. "longitude:-121.9780,latitude:37.7799" and JSON text.
        import json
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            return coordinates(parsed)
        import re
        fields = dict(re.findall(r"(longitude|latitude|lon|lat|lng)\s*[:=]\s*(-?\d+(?:\.\d+)?)", value, re.I))
        lat = fields.get("latitude", fields.get("lat"))
        lon = fields.get("longitude", fields.get("lon", fields.get("lng")))
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)) and len(value) >= 2:
        # Tuple/list inputs conventionally use (latitude, longitude).
        lat, lon = value[0], value[1]
    lat_n, lon_n = _finite_number(lat), _finite_number(lon)
    if lat_n is None or lon_n is None or not (-90 <= lat_n <= 90 and -180 <= lon_n <= 180):
        return None
    return lat_n, lon_n


def address_from_record(record: Mapping[str, Any] | None) -> str | None:
    """Build a geocodable address from actual location names, never IDs."""
    if not record:
        return None
    keys = ("street", "address", "address_line_1", "city_name", "city", "state_name", "state", "zip_code", "postal_code", "country_name", "country")
    parts: list[str] = []
    for key in keys:
        value = record.get(key)
        if value is None or isinstance(value, (int, float)):
            continue
        value = str(value).strip()
        if value and value.casefold() not in {part.casefold() for part in parts}:
            parts.append(value)
    return ", ".join(parts) or None


def beneficiary_coordinates(
    request: Mapping[str, Any] | None,
    user_location: Mapping[str, Any] | None = None,
    profile_address: Mapping[str, Any] | str | None = None,
    *,
    geocode: Geocoder | None = None,
) -> tuple[float, float] | None:
    """Resolve request coordinates, then current location, then profile address."""
    request = request or {}
    point = coordinates(request.get("req_loc"))
    if point:
        return point
    point = coordinates((user_location or {}).get("curr_loc"))
    if point:
        return point
    if isinstance(profile_address, str):
        address = profile_address.strip() or None
    else:
        address = address_from_record(profile_address)
    return _resolve_address(address, geocode, {}) if address else None


def _resolve_address(
    address: str | None,
    geocode: Geocoder | None,
    coordinate_cache: MutableMapping[str, Any],
) -> tuple[float, float] | None:
    if not address:
        return None
    key = " ".join(address.casefold().split())
    if key in coordinate_cache:
        return coordinates(coordinate_cache[key])
    if geocode is None:
        return None
    try:
        result = geocode(address)
    except Exception:
        return None
    point = coordinates(result)
    if point:
        coordinate_cache[key] = {"latitude": point[0], "longitude": point[1]}
    return point


def straight_line_miles(origin: tuple[float, float], destination: tuple[float, float]) -> float:
    """Return great-circle (straight-line) miles using the haversine formula."""
    lat1, lon1 = map(math.radians, origin)
    lat2, lon2 = map(math.radians, destination)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.7613 * 2 * math.asin(min(1.0, math.sqrt(hav)))


def enrich_organizations(
    organizations: Sequence[Mapping[str, Any]],
    beneficiary_point: tuple[float, float] | None,
    *,
    geocode: Geocoder | None = None,
    coordinate_cache: MutableMapping[str, Any] | None = None,
    online_only: Callable[[Mapping[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Add distance metadata while preserving all organization fields and rows.

    Supply ``geocode`` only after selecting a provider. ``coordinate_cache`` can
    be backed by the Lambda's approved persistent cache (for example its DB).
    Without a provider/cache hit, addresses are left unmeasured, never guessed.
    """
    cache = coordinate_cache if coordinate_cache is not None else {}
    enriched: list[dict[str, Any]] = []
    for organization in organizations:
        row = dict(organization)
        row["distance_unit"] = "miles"
        row["distance_method"] = "straight_line"
        row["distance"] = None
        if online_only and online_only(organization):
            row["distance_status"] = "online"
            enriched.append(row)
            continue
        if beneficiary_point is None:
            row["distance_status"] = "unknown_location"
            enriched.append(row)
            continue

        point = coordinates(organization.get("coordinates"))
        address = organization.get("location")
        if not isinstance(address, str) or not address.strip():
            address = address_from_record(organization)
        # Prefer address keys in cache so a DB or GenAI address can be reused.
        if point is None:
            point = _resolve_address(address, geocode, cache)
        if point is None:
            row["distance_status"] = "not_found" if address else "unknown_location"
        else:
            try:
                distance = straight_line_miles(beneficiary_point, point)
                row["distance"] = round(distance, 1)
                row["distance_status"] = "ok"
            except Exception:
                row["distance_status"] = "error"
        enriched.append(row)
    return enriched


def sort_by_distance(organizations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Sort known distances nearest-first, placing unknown values last (including 0)."""
    return sorted(
        (dict(row) for row in organizations),
        key=lambda row: (row.get("distance") is None, _finite_number(row.get("distance")) or 0.0),
    )


# ---------------------------------------------------------------------------
# Local sample API wiring. Replace these records with the aggregator's DB and
# GenAI results when connecting this module to the real Lambda.
# ---------------------------------------------------------------------------
DUMMY_REQUESTS: dict[str, dict[str, Any]] = {
    "req-001": {"request_id": "req-001", "beneficiary_id": "user-001", "req_loc": "longitude:-121.9780,latitude:37.7799"},
    "req-002": {"request_id": "req-002", "beneficiary_id": "user-002", "req_loc": None},
    "req-003": {"request_id": "req-003", "beneficiary_id": "user-003", "req_loc": None},
}
DUMMY_USER_LOCATIONS: dict[str, dict[str, Any]] = {
    "user-002": {"curr_loc": {"latitude": 37.7799, "longitude": -121.9780}},
}
DUMMY_USERS: dict[str, dict[str, Any]] = {
    "user-003": {"street": "1 Infinite Loop", "city_name": "Cupertino", "state_name": "CA", "zip_code": "95014", "country_name": "United States"},
}
DUMMY_ORGANIZATIONS: list[dict[str, Any]] = [
    {"Name": "The Nature Conservancy", "Org-type": "nonprofit", "Collaborator": False, "location": "Stockton, CA", "size": "large", "rating": 4.8, "coordinates": {"latitude": 37.9577, "longitude": -121.2908}},
    {"Name": "Khan Academy", "Org-type": "nonprofit", "Collaborator": False, "location": "Mountain View, CA 94043", "size": "medium", "rating": 4.7, "coordinates": {"latitude": 37.4220, "longitude": -122.0841}},
    {"Name": "Microsoft", "Org-type": "company", "Collaborator": True, "location": "Redmond, WA", "size": "large", "rating": 4.5, "coordinates": {"latitude": 47.6740, "longitude": -122.1215}},
    {"Name": "United Way of America", "Org-type": "nonprofit", "Collaborator": False, "location": None, "size": "large", "rating": 4.4},
    {"Name": "Google", "Org-type": "company", "Collaborator": True, "location": "Mountain View, CA", "size": "large", "rating": 4.6, "coordinates": {"latitude": 37.4220, "longitude": -122.0841}},
    {"Name": "Online Help Network", "Org-type": "online", "Collaborator": False, "location": None, "size": "small", "rating": 4.1, "online_only": True},
]

# A process-local cache makes repeat requests within a warm Lambda reuse results.
# For persistent caching, replace with a mapping backed by the chosen DB/table.
_COORDINATE_CACHE: dict[str, Any] = {}


def geocode_via_configured_api(address: str) -> tuple[float, float] | None:
    """Call an optional generic geocoding HTTP endpoint configured by environment.

    Set GEOCODER_API_URL to an endpoint that accepts ``?address=...`` and
    returns JSON with latitude/longitude (or lat/lon; optionally under results[0]
    or location). Set GEOCODER_API_KEY and GEOCODER_API_KEY_HEADER if required.
    Change providers by changing configuration or passing another geocode
    callable; the distance code itself remains unchanged.
    """
    endpoint = os.getenv("GEOCODER_API_URL")
    if not endpoint:
        return None
    separator = "&" if "?" in endpoint else "?"
    url = endpoint + separator + urlencode({"address": address})
    headers = {"Accept": "application/json"}
    api_key = os.getenv("GEOCODER_API_KEY")
    if api_key:
        headers[os.getenv("GEOCODER_API_KEY_HEADER", "Authorization")] = api_key
    request = Request(url, headers=headers)
    timeout = float(os.getenv("GEOCODER_TIMEOUT_SECONDS", "4"))
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, Mapping):
        candidates = payload.get("results")
        if isinstance(candidates, Sequence) and candidates:
            payload = candidates[0]
        if isinstance(payload, Mapping) and isinstance(payload.get("location"), Mapping):
            payload = payload["location"]
    return coordinates(payload)


def _lookup_dummy_request(request_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    request = DUMMY_REQUESTS.get(request_id)
    if not request:
        return None, None, None
    beneficiary_id = request.get("beneficiary_id")
    return request, DUMMY_USER_LOCATIONS.get(beneficiary_id), DUMMY_USERS.get(beneficiary_id)


def get_organizations_response(
    request_id: str,
    *,
    requests: Mapping[str, Mapping[str, Any]] = DUMMY_REQUESTS,
    user_locations: Mapping[str, Mapping[str, Any]] = DUMMY_USER_LOCATIONS,
    users: Mapping[str, Mapping[str, Any]] = DUMMY_USERS,
    organizations: Sequence[Mapping[str, Any]] = DUMMY_ORGANIZATIONS,
    geocode: Geocoder | None = None,
    coordinate_cache: MutableMapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Lambda/API-style response using the supplied data sources."""
    request = requests.get(request_id)
    if request is None:
        return {"statusCode": 404, "body": {"error": "request_not_found", "request_id": request_id}}
    beneficiary_id = request.get("beneficiary_id")
    cache = coordinate_cache if coordinate_cache is not None else _COORDINATE_CACHE
    active_geocoder = geocode if geocode is not None else geocode_via_configured_api
    point = coordinates(request.get("req_loc"))
    if point is None:
        point = coordinates(user_locations.get(beneficiary_id, {}).get("curr_loc"))
    if point is None:
        profile = users.get(beneficiary_id)
        profile_address = address_from_record(profile)
        point = _resolve_address(profile_address, active_geocoder, cache)
    enriched = enrich_organizations(
        organizations,
        point,
        geocode=active_geocoder,
        coordinate_cache=cache,
        online_only=lambda row: bool(row.get("online_only")),
    )
    return {"statusCode": 200, "body": {"request_id": request_id, "beneficiary_id": beneficiary_id, "organizations": enriched}}


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Runnable sample handler; swap dummy mappings for actual DB/API results."""
    params = event.get("queryStringParameters") or {}
    body = event.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = {}
    body = body if isinstance(body, Mapping) else {}
    request_id = params.get("request_id") or event.get("request_id") or body.get("request_id") or "req-001"
    result = get_organizations_response(str(request_id))
    return {"statusCode": result["statusCode"], "headers": {"Content-Type": "application/json"}, "body": json.dumps(result["body"])}


if __name__ == "__main__":
    # Example: python task.py req-001
    import sys
    sample_id = sys.argv[1] if len(sys.argv) > 1 else "req-001"
    print(json.dumps(get_organizations_response(sample_id)["body"], indent=2))
