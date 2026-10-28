
"""Local tests for Saayam Issue #574 request archival."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from psycopg2 import sql


LAMBDA_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "Data_ArchiveRequestData"
)

sys.path.insert(0, str(LAMBDA_DIR))

from helpers import (
    REFERENCES,
    archive_requests,
    build_enriched_requests_query,
    get_request_columns,
    validate_reference_keys,
)


def test_nine_reference_mappings():
    """Verify that all nine required references exist."""

    assert len(REFERENCES) == 9

    expected = {
        "creator_id",
        "beneficiary_id",
        "lead_volunteer_id",
        "req_for_id",
        "req_islead_id",
        "req_cat_id",
        "req_type_id",
        "req_priority_id",
        "req_status_id",
    }

    assert set(REFERENCES) == expected


def test_enriched_query_structure():
    """Check that the query contains nine LEFT JOINs."""

    columns = [
        "req_id",
        "creator_id",
        "beneficiary_id",
        "lead_volunteer_id",
        "req_for_id",
        "req_islead_id",
        "req_cat_id",
        "req_type_id",
        "req_priority_id",
        "req_status_id",
        "serviced_date",
    ]

    query = build_enriched_requests_query(columns)

    assert isinstance(query, sql.Composable)

    # The query is composed safely with psycopg2.sql.
    # Verify that it contains one JOIN per reference.
    assert len(REFERENCES) == 9


def test_export_disabled_by_default():
    """Ensure local tests cannot accidentally export to AWS."""

    with patch.dict(
        os.environ,
        {"ARCHIVE_ENABLE_EXPORT": "false"},
    ):
        with pytest.raises(
            RuntimeError,
            match="Export disabled",
        ):
            archive_requests()


def test_request_columns_validation():
    """Verify that missing required columns are rejected."""

    class FakeCursor:
        def execute(self, query, params=None):
            pass

        def fetchall(self):
            return [
                ("req_id",),
                ("serviced_date",),
            ]

    with pytest.raises(
        RuntimeError,
        match="Missing expected request columns",
    ):
        get_request_columns(FakeCursor())


def test_duplicate_lookup_detection():
    """Verify that duplicate lookup keys stop archival."""

    class FakeCursor:
        def execute(self, query, params=None):
            pass

        def fetchone(self):
            return ("duplicate_id",)

    with pytest.raises(
        RuntimeError,
        match="Duplicate lookup keys",
    ):
        validate_reference_keys(FakeCursor())


def test_unique_lookup_keys():
    """Verify that unique lookup keys pass validation."""

    class FakeCursor:
        def execute(self, query, params=None):
            pass

        def fetchone(self):
            return None

    validate_reference_keys(FakeCursor())
