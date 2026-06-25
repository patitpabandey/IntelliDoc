"""
archive_find_candidates.py
Lambda: intellidoc-find-archive-candidates

Queries Snowflake DOCUMENT_REGISTRY for documents in AVAILABLE status
that are older than the configured retention threshold (default 730 days).
Returns a list of file_ids for the Step Functions Map state to process.

Input event:
  { "retention_days": 730, "status_filter": "AVAILABLE" }

Output:
  { "count": N, "file_ids": ["uuid1", "uuid2", ...] }

No static AWS keys — Lambda uses its execution role.
"""

import json
import logging
import os

import snowflake.connector

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SF_ACCOUNT  = os.environ.get("SNOWFLAKE_ACCOUNT", "")
SF_USER     = os.environ.get("SNOWFLAKE_USER", "")
SF_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
SF_ROLE     = os.environ.get("SNOWFLAKE_ROLE",     "INTELLIDOC_ROLE")
SF_WH       = os.environ.get("SNOWFLAKE_WAREHOUSE", "INTELLIDOC_WH")
SF_DB       = os.environ.get("SNOWFLAKE_DATABASE",  "INTELLIDOC_DB")
SF_SCHEMA   = os.environ.get("SNOWFLAKE_SCHEMA",    "INTELLIDOC")

DEFAULT_RETENTION_DAYS = 730


def _snowflake_conn():
    return snowflake.connector.connect(
        account=SF_ACCOUNT, user=SF_USER, password=SF_PASSWORD,
        role=SF_ROLE, warehouse=SF_WH, database=SF_DB, schema=SF_SCHEMA,
    )


def find_candidates(retention_days: int, status_filter: str) -> list:
    sql = """
        SELECT FILE_ID
        FROM DOCUMENT_REGISTRY
        WHERE PROCESSING_STATUS = %s
          AND CREATED_TS < DATEADD('day', -%s, SYSDATE())
        ORDER BY CREATED_TS ASC
    """
    with _snowflake_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (status_filter, retention_days))
            rows = cur.fetchall()
    return [row[0] for row in rows]


def lambda_handler(event: dict, context) -> dict:
    retention_days = int(event.get("retention_days", DEFAULT_RETENTION_DAYS))
    status_filter  = event.get("status_filter", "AVAILABLE")

    logger.info("Searching for %s documents older than %d days", status_filter, retention_days)

    try:
        file_ids = find_candidates(retention_days, status_filter)
    except Exception as e:
        logger.error("Snowflake query failed: %s", e)
        raise

    logger.info("Found %d candidates for archival", len(file_ids))
    return {"count": len(file_ids), "file_ids": file_ids}


if __name__ == "__main__":
    result = lambda_handler({"retention_days": 730, "status_filter": "AVAILABLE"}, None)
    print(json.dumps(result, indent=2))
