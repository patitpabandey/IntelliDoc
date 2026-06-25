"""
archive_s3_glacier_move.py
Lambda: intellidoc-s3-glacier-move

Moves a validated document from S3 Standard to S3 Glacier Instant Retrieval
(GLACIER_IR) by copying the object with a new storage class, then deleting
the original. Fetches the file's current S3 location from Snowflake
DOCUMENT_REGISTRY.

Input event:
  {
    "file_id":       "uuid",
    "source_prefix": "validated/",
    "storage_class": "GLACIER_IR"
  }

Output:
  { "file_id": "uuid", "glacier_uri": "s3://bucket/archive/path" }

No static AWS keys — Lambda uses its execution role.
"""

import json
import logging
import os

import boto3
import snowflake.connector
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET        = os.environ["S3_BUCKET_NAME"]
ARCHIVE_PREFIX   = os.environ.get("S3_ARCHIVE_PREFIX", "archive/")
AWS_REGION       = os.environ.get("AWS_REGION", "us-east-1")

SF_ACCOUNT  = os.environ.get("SNOWFLAKE_ACCOUNT", "")
SF_USER     = os.environ.get("SNOWFLAKE_USER", "")
SF_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
SF_ROLE     = os.environ.get("SNOWFLAKE_ROLE",     "INTELLIDOC_ROLE")
SF_WH       = os.environ.get("SNOWFLAKE_WAREHOUSE", "INTELLIDOC_WH")
SF_DB       = os.environ.get("SNOWFLAKE_DATABASE",  "INTELLIDOC_DB")
SF_SCHEMA   = os.environ.get("SNOWFLAKE_SCHEMA",    "INTELLIDOC")

_s3 = None


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=AWS_REGION)
    return _s3


def _snowflake_conn():
    return snowflake.connector.connect(
        account=SF_ACCOUNT, user=SF_USER, password=SF_PASSWORD,
        role=SF_ROLE, warehouse=SF_WH, database=SF_DB, schema=SF_SCHEMA,
    )


def get_file_location(file_id: str) -> tuple:
    """Returns (file_name, current_location) from DOCUMENT_REGISTRY."""
    sql = "SELECT FILE_NAME, CURRENT_LOCATION FROM DOCUMENT_REGISTRY WHERE FILE_ID = %s"
    with _snowflake_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (file_id,))
            row = cur.fetchone()
    if not row:
        raise ValueError(f"FILE_ID not found: {file_id}")
    return row[0], row[1]


def glacier_move(file_id: str, storage_class: str) -> str:
    """
    Copies the S3 object to ARCHIVE_PREFIX with the given storage class,
    deletes the original, and returns the new S3 URI.
    """
    file_name, current_location = get_file_location(file_id)

    # current_location is stored as "validated/<account_id>/<filename>"
    src_key = current_location.lstrip("/")
    dst_key = f"{ARCHIVE_PREFIX}{file_name}"

    s3 = _get_s3()

    try:
        s3.copy_object(
            Bucket=S3_BUCKET,
            CopySource={"Bucket": S3_BUCKET, "Key": src_key},
            Key=dst_key,
            StorageClass=storage_class,
        )
        logger.info("Copied %s → %s (%s)", src_key, dst_key, storage_class)
    except ClientError as e:
        logger.error("S3 copy failed for %s: %s", file_id, e)
        raise

    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=src_key)
        logger.info("Deleted source: %s", src_key)
    except ClientError as e:
        logger.warning("Source delete failed (non-fatal) for %s: %s", src_key, e)

    return f"s3://{S3_BUCKET}/{dst_key}"


def lambda_handler(event: dict, context) -> dict:
    file_id       = event["file_id"]
    storage_class = event.get("storage_class", "GLACIER_IR")

    logger.info("Moving file %s to %s", file_id, storage_class)

    try:
        glacier_uri = glacier_move(file_id, storage_class)
    except Exception as e:
        logger.error("Glacier move failed for %s: %s", file_id, e)
        raise

    return {"file_id": file_id, "glacier_uri": glacier_uri}


if __name__ == "__main__":
    import sys
    file_id = sys.argv[1] if len(sys.argv) > 1 else "test-file-id"
    result = lambda_handler({"file_id": file_id, "storage_class": "GLACIER_IR"}, None)
    print(json.dumps(result, indent=2))
