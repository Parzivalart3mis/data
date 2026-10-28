
"""Enriched request archival for Saayam Issue #574."""

import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
from psycopg2 import sql
from aws_lambda_powertools.utilities import parameters


logger = logging.getLogger(__name__)

DB_PARAMETER = os.getenv(
    "DB_PARAMETER",
    "/dev/saayam/db/Virginia/Analytics/user",
)

SCHEMA = os.getenv(
    "ARCHIVE_DB_SCHEMA",
    "virginia_dev_saayam_rdbms",
)

TABLE = "requests"
WATERMARK_TABLE = "request_archive_watermark_574"


# Request foreign keys and their human-readable lookups.
# Format: lookup table, alias, lookup key, label column.

REFERENCES = {
    "creator_id": ("users", "creator", "user_id", "full_name"),
    "beneficiary_id": ("users", "beneficiary", "user_id", "full_name"),
    "lead_volunteer_id": ("users", "lead_volunteer", "user_id", "full_name"),
    "req_for_id": ("request_for", "rf", "req_for_id", "req_for"),
    "req_islead_id": ("request_isleadvol", "il", "req_islead_id", "req_islead"),
    "req_cat_id": ("help_category", "hc", "cat_id", "cat_name"),
    "req_type_id": ("request_type", "rt", "req_type_id", "req_type"),
    "req_priority_id": ("request_priority", "rp", "req_priority_id", "req_priority"),
    "req_status_id": ("request_statuses", "rs", "req_status_id", "req_status"),
}


def get_db_connection():
    """Connect to PostgreSQL using AWS Parameter Store."""

    credentials = json.loads(
        parameters.get_parameter(
            DB_PARAMETER,
            decrypt=True,
            max_age=3600,
        )
    )

    return psycopg2.connect(
        host=credentials["HOST"],
        user=credentials["USERNAME"],
        password=credentials["PASSWORD"],
        database=credentials["DATABASE_NAME"],
        port=credentials["PORT"],
        sslmode="require",
        connect_timeout=15,
    )


def get_request_columns(cursor):
    """Get request columns in their original order."""

    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (SCHEMA, TABLE),
    )

    columns = [row[0] for row in cursor.fetchall()]

    required = set(REFERENCES) | {
        "req_id",
        "serviced_date",
    }

    missing = required - set(columns)

    if missing:
        raise RuntimeError(
            f"Missing expected request columns: {sorted(missing)}"
        )

    return columns


def build_enriched_requests_query(columns):
    """Build the enriched SELECT query with nine LEFT JOINs."""

    select_columns = []
    joins = []

    for column in columns:

        if column in REFERENCES:

            table, alias, key, label = REFERENCES[column]

            select_columns.append(
                sql.SQL(
                    "COALESCE("
                    "{alias}.{label}::text, "
                    "r.{column}::text"
                    ") AS {column}"
                ).format(
                    alias=sql.Identifier(alias),
                    label=sql.Identifier(label),
                    column=sql.Identifier(column),
                )
            )

            joins.append(
                sql.SQL(
                    "LEFT JOIN {schema}.{table} AS {alias} "
                    "ON r.{column} = {alias}.{key}"
                ).format(
                    schema=sql.Identifier(SCHEMA),
                    table=sql.Identifier(table),
                    alias=sql.Identifier(alias),
                    column=sql.Identifier(column),
                    key=sql.Identifier(key),
                )
            )

        else:

            select_columns.append(
                sql.SQL("r.{column}").format(
                    column=sql.Identifier(column)
                )
            )

    return sql.SQL(
        "SELECT {columns} "
        "FROM {schema}.{table} AS r "
        "{joins}"
    ).format(
        columns=sql.SQL(", ").join(select_columns),
        schema=sql.Identifier(SCHEMA),
        table=sql.Identifier(TABLE),
        joins=sql.SQL(" ").join(joins),
    )


def validate_reference_keys(cursor):
    """Ensure lookup keys are unique."""

    checked = set()

    for table, alias, key, label in REFERENCES.values():

        if (table, key) in checked:
            continue

        checked.add((table, key))

        query = sql.SQL(
            "SELECT {key} "
            "FROM {schema}.{table} "
            "WHERE {key} IS NOT NULL "
            "GROUP BY {key} "
            "HAVING COUNT(*) > 1 "
            "LIMIT 1"
        ).format(
            key=sql.Identifier(key),
            schema=sql.Identifier(SCHEMA),
            table=sql.Identifier(table),
        )

        cursor.execute(query)

        if cursor.fetchone():
            raise RuntimeError(
                f"Duplicate lookup keys found in {table}.{key}"
            )


def ensure_watermark(cursor):
    """Create, lock, and read the archival watermark."""

    cursor.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {schema}.{table} ("
            "archive_name TEXT PRIMARY KEY, "
            "last_serviced_date TIMESTAMPTZ"
            ")"
        ).format(
            schema=sql.Identifier(SCHEMA),
            table=sql.Identifier(WATERMARK_TABLE),
        )
    )

    cursor.execute(
        sql.SQL(
            "INSERT INTO {schema}.{table} "
            "(archive_name, last_serviced_date) "
            "VALUES (%s, NULL) "
            "ON CONFLICT (archive_name) DO NOTHING"
        ).format(
            schema=sql.Identifier(SCHEMA),
            table=sql.Identifier(WATERMARK_TABLE),
        ),
        ("enriched_requests",),
    )

    cursor.execute(
        sql.SQL(
            "SELECT last_serviced_date "
            "FROM {schema}.{table} "
            "WHERE archive_name = %s "
            "FOR UPDATE"
        ).format(
            schema=sql.Identifier(SCHEMA),
            table=sql.Identifier(WATERMARK_TABLE),
        ),
        ("enriched_requests",),
    )

    return cursor.fetchone()[0]


def archive_requests():
    """Archive enriched requests into a single CSV export."""

    # Prevent accidental AWS writes during development.
    if os.getenv(
        "ARCHIVE_ENABLE_EXPORT", "false"
    ).lower() != "true":
        raise RuntimeError(
            "Export disabled. Set ARCHIVE_ENABLE_EXPORT=true "
            "only after validation."
        )

    bucket = os.getenv("ARCHIVE_S3_BUCKET")
    region = os.getenv("ARCHIVE_S3_REGION")

    prefix = os.getenv(
        "ARCHIVE_S3_PREFIX",
        "enriched-requests",
    ).strip("/")

    if not bucket or not region:
        raise RuntimeError(
            "ARCHIVE_S3_BUCKET and ARCHIVE_S3_REGION are required."
        )

    if not prefix or any(
        part in (".", "..") for part in prefix.split("/")
    ):
        raise RuntimeError("Invalid S3 prefix.")

    connection = get_db_connection()

    try:
        with connection.cursor() as cursor:

            # Step 1: Inspect request columns.
            columns = get_request_columns(cursor)

            # Step 2: Check reference-table uniqueness.
            validate_reference_keys(cursor)

            # Step 3: Read and lock the watermark.
            previous_watermark = ensure_watermark(cursor)

            cutoff = datetime.now(timezone.utc)

            if (
                previous_watermark is not None
                and previous_watermark >= cutoff
            ):
                raise RuntimeError("Invalid watermark window.")

            # Step 4: Define the incremental date filter.
            date_filter = sql.SQL(
                "r.serviced_date IS NOT NULL "
                "AND ("
                "%s::timestamptz IS NULL "
                "OR r.serviced_date > %s::timestamptz"
                ") "
                "AND r.serviced_date <= %s::timestamptz"
            )

            params = (
                previous_watermark,
                previous_watermark,
                cutoff,
            )

            # Step 5: Count source requests.
            source_query = sql.SQL(
                "SELECT COUNT(*), COUNT(DISTINCT r.req_id) "
                "FROM {schema}.{table} AS r "
                "WHERE {date_filter}"
            ).format(
                schema=sql.Identifier(SCHEMA),
                table=sql.Identifier(TABLE),
                date_filter=date_filter,
            )

            cursor.execute(source_query, params)

            source_count, source_unique = cursor.fetchone()

            if source_count != source_unique:
                raise RuntimeError(
                    "Duplicate or NULL source request IDs."
                )

            if source_count == 0:
                connection.rollback()

                return {
                    "status": "no_data",
                    "source_count": 0,
                    "exported_count": 0,
                }

            # Step 6: Build the enriched query.
            enriched_query = build_enriched_requests_query(
                columns
            )

            # Apply filtering BEFORE wrapping the query.
            filtered_query = sql.SQL(
                "{query} WHERE {date_filter}"
            ).format(
                query=enriched_query,
                date_filter=date_filter,
            )

            # Step 7: Validate row counts and uniqueness.
            validation_query = sql.SQL(
                "SELECT COUNT(*), COUNT(DISTINCT req_id) "
                "FROM ({query}) AS enriched"
            ).format(
                query=filtered_query,
            )

            cursor.execute(validation_query, params)

            enriched_count, enriched_unique = cursor.fetchone()

            if (
                enriched_count != source_count
                or enriched_unique != source_count
            ):
                raise RuntimeError(
                    "Enrichment row-count mismatch: "
                    f"source={source_count}, "
                    f"enriched={enriched_count}, "
                    f"unique={enriched_unique}"
                )

            # Step 8: Create a unique S3 object key.
            timestamp = cutoff.strftime(
                "%Y%m%dT%H%M%S%fZ"
            )

            s3_key = (
                f"{prefix}/requests_{timestamp}.csv"
            )

            # Step 9: Convert the parameterized query to
            # SQL text for aws_s3.query_export_to_s3.
            export_sql = cursor.mogrify(
                filtered_query,
                params,
            ).decode("utf-8")

            # Step 10: Export CSV to S3.
            cursor.execute(
                """
                SELECT *
                FROM aws_s3.query_export_to_s3(
                    %s,
                    aws_commons.create_s3_uri(
                        %s, %s, %s
                    ),
                    options := 'format csv, header true'
                )
                """,
                (
                    export_sql,
                    bucket,
                    s3_key,
                    region,
                ),
            )

            export_result = cursor.fetchone()
            if export_result is None:
                raise RuntimeError("S3 export returned no result.")

            rows_uploaded = int(export_result[0])
            files_uploaded = int(export_result[1])

            # Step 11: Validate export result.
            if rows_uploaded != source_count:
                raise RuntimeError(
                    "S3 row-count mismatch: "
                    f"expected={source_count}, "
                    f"uploaded={rows_uploaded}"
                )

            if files_uploaded != 1:
                raise RuntimeError(
                    "Expected exactly one CSV export, "
                    f"but received {files_uploaded} files."
                )

            # Step 12: Advance the watermark.
            cursor.execute(
                sql.SQL(
                    "UPDATE {schema}.{table} "
                    "SET last_serviced_date = %s "
                    "WHERE archive_name = %s"
                ).format(
                    schema=sql.Identifier(SCHEMA),
                    table=sql.Identifier(WATERMARK_TABLE),
                ),
                (
                    cutoff,
                    "enriched_requests",
                ),
            )

            connection.commit()

            logger.info(
                "Archived %s requests to s3://%s/%s",
                rows_uploaded,
                bucket,
                s3_key,
            )

            return {
                "status": "success",
                "source_count": source_count,
                "enriched_count": enriched_count,
                "exported_count": rows_uploaded,
                "s3_bucket": bucket,
                "s3_key": s3_key,
                "watermark": cutoff.isoformat(),
            }

    except Exception:
        connection.rollback()
        logger.exception("Request archival failed.")
        raise

    finally:
        connection.close()
