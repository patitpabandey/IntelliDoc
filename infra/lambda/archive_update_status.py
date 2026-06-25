"""
archive_update_status.py
Lambda: intellidoc-update-status

Updates PROCESSING_STATUS in DOCUMENT_REGISTRY and appends a row to
DOCUMENT_PROCESSING_AUDIT. Used by the archive Step Functions workflow
for both ARCHIVING (start) and ARCHIVED (completion) transitions.

Input event:
  {
    "file_id":    "uuid",
    "new_status": "ARCHIVING" | "ARCHIVED",
    "step_name":  "ARCHIVE",
    "location":   "s3://bucket/path"   # optional, only for ARCHIVED
  }

Output:
  { "file_id": "uuid", "status": "ARCHIVING" | "ARCHIVED" }

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

TERMINAL_STATUSES = {"AVAILABLE", "ARCHIVED"}


def _snowflake_conn():
    return snowflake.connector.connect(
        account=SF_ACCOUNT, user=SF_USER, password=SF_PASSWORD,
        role=SF_ROLE, warehouse=SF_WH, database=SF_DB, schema=SF_SCHEMA,
    )


def update_status(file_id: str, new_status: str, step_name: str, location: str = None):
    sql_check = "SELECT PROCESSING_STATUS FROM DOCUMENT_REGISTRY WHERE FILE_ID = %s"

    sql_update = """
        UPDATE DOCUMENT_REGISTRY
        SET PROCESSING_STATUS = %s,
            CURRENT_LOCATION  = COALESCE(%s, CURRENT_LOCATION),
            UPDATED_TS        = SYSDATE()
        WHERE FILE_ID = %s
    """

    sql_audit = """
        INSERT INTO DOCUMENT_PROCESSING_AUDIT
            (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS)
        VALUES (%s, %s, %s, SYSDATE(), SYSDATE())
    """

    with _snowflake_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_check, (file_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"FILE_ID not found in DOCUMENT_REGISTRY: {file_id}")

            current_status = row[0]
            if current_status == "ARCHIVED":
                logger.info("Idempotency skip: %s already ARCHIVED", file_id)
                return

            cur.execute(sql_update, (new_status, location, file_id))
            cur.execute(sql_audit, (file_id, step_name, new_status))
        conn.commit()

    logger.info("Updated %s → %s", file_id, new_status)


def lambda_handler(event: dict, context) -> dict:
    file_id    = event["file_id"]
    new_status = event["new_status"]
    step_name  = event.get("step_name", "ARCHIVE")
    location   = event.get("location")

    try:
        update_status(file_id, new_status, step_name, location)
    except Exception as e:
        logger.error("Status update failed for %s: %s", file_id, e)
        raise

    return {"file_id": file_id, "status": new_status}


if __name__ == "__main__":
    import sys
    file_id = sys.argv[1] if len(sys.argv) > 1 else "test-file-id"
    result = lambda_handler({"file_id": file_id, "new_status": "ARCHIVING", "step_name": "ARCHIVE"}, None)
    print(json.dumps(result, indent=2))
