from distance_utils import build_distance_result
from geocoding_utils import (
    build_organization_address,
    geocode_address_cached,
)


def enrich_organizations_with_distance(
    organizations,
    beneficiary_coordinates,
    geocoding_provider=None,
):
    """
    Add beneficiary-to-organization distance information.

    Does not modify the original organization records.
    Geocoding is optional and requires an approved provider.
    """
    results = []

    for organization in organizations:
        enriched = organization.copy()

        address = build_organization_address(organization)

        organization_coordinates = geocode_address_cached(
            address,
            provider=geocoding_provider,
        )

        distance_result = build_distance_result(
            beneficiary_coordinates,
            organization_coordinates,
        )

        enriched.update(distance_result)
        results.append(enriched)

    return results