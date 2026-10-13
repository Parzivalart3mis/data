from functools import lru_cache
import math
def build_organization_address(organization):
    """
    Build an organization address using available database fields.
    Returns None if no usable address is available.
    """
    if not isinstance(organization, dict):
        return None

    parts = [
        organization.get("street"),
        organization.get("city_name"),
        organization.get("state_code"),
        organization.get("zip_code"),
        organization.get("country"),
    ]

    cleaned = []

    for part in parts:
        if part is None:
            continue

        if isinstance(part, float) and math.isnan(part):
            continue

        value = str(part).strip()

        if value and value.lower() not in ("nan", "none", "null"):
            cleaned.append(value)

    return ", ".join(cleaned) if cleaned else None


def geocode_address(address, provider=None):
    """
    Resolve an address to (latitude, longitude).

    The geocoding provider is intentionally configurable.
    No external service is called unless a provider is supplied.
    """
    if not address or provider is None:
        return None

    return provider(address)

@lru_cache(maxsize=1024)
def _cached_geocode(address, provider):
    return provider(address)


def geocode_address_cached(address, provider=None):
    """
    Geocode an address with an in-memory cache.

    Results are reused within the same Lambda instance.
    Provider failures return None without crashing.
    """
    if not address or provider is None:
        return None

    normalized_address = " ".join(str(address).strip().split())

    if not normalized_address:
        return None

    try:
        return _cached_geocode(normalized_address, provider)
    except Exception:
        return None