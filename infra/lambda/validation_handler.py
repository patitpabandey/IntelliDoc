"""
validation_handler.py
AWS Lambda (Python 3.11) — triggered by SQS messages from S3 EventBridge.

Validation flow per message:
  1. Parse S3 key from SQS/EventBridge envelope
  2. Fetch companion metadata JSON from s3://bucket/metadata/<file_id>.json
  3. Validate file format (PDF | XLSX | CSV) and SHA-256 integrity (filehash)
  4. Look up DynamoDB CustodyCollectionRegistry:
       PK = custody_country   (from metadata)
       SK = legal_entity_id   (from metadata)
       Validate: active_flag=True, destination matches, file_format in allowed_formats
  5a. Valid  → copy to /validated/<Accountid>/<filename>
              → insert DOCUMENT_REGISTRY (ACCOUNT_ID, BRANCH_ID from metadata)
              → write VALIDATED to DOCUMENT_PROCESSING_AUDIT
  5b. Invalid → copy to /quarantine/<reason>/<filename>
              → publish SNS alert
              → write FAILED to DOCUMENT_PROCESSING_AUDIT (if file_id known)

Note: CLIENT_ID is NOT set here. It is resolved at routing time in Snowflake
by joining DOCUMENT_REGISTRY.ACCOUNT_ID + BRANCH_ID against CLIENT_MAPPING.

No static AWS keys — Lambda uses its execution role.
"""

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
import snowflake.connector
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment variables ─────────────────────────────────────────────────────
S3_BUCKET           = os.environ["S3_BUCKET_NAME"]
INCOMING_PREFIX     = os.environ.get("S3_INCOMING_PREFIX",  "incoming/")
METADATA_PREFIX     = os.environ.get("S3_METADATA_PREFIX",  "metadata/")
VALIDATED_PREFIX    = os.environ.get("S3_VALIDATED_PREFIX", "validated/")
QUARANTINE_PREFIX   = os.environ.get("S3_QUARANTINE_PREFIX","quarantine/")
DYNAMODB_TABLE      = os.environ.get("DYNAMODB_TABLE_NAME", "CustodyCollectionRegistry")
SNS_TOPIC_ARN       = os.environ.get("SNS_ALERT_TOPIC_ARN", "")
AWS_REGION          = os.environ.get("AWS_REGION", "us-east-1")

SF_ACCOUNT   = os.environ.get("SNOWFLAKE_ACCOUNT", "")
SF_USER      = os.environ.get("SNOWFLAKE_USER", "")
SF_PASSWORD  = os.environ.get("SNOWFLAKE_PASSWORD", "")
SF_ROLE      = os.environ.get("SNOWFLAKE_ROLE",      "INTELLIDOC_ROLE")
SF_WH        = os.environ.get("SNOWFLAKE_WAREHOUSE",  "INTELLIDOC_WH")
SF_DB        = os.environ.get("SNOWFLAKE_DATABASE",   "INTELLIDOC_DB")
SF_SCHEMA    = os.environ.get("SNOWFLAKE_SCHEMA",     "INTELLIDOC")

ALLOWED_FORMATS = {"PDF", "XLSX", "CSV"}

# ── AWS clients ───────────────────────────────────────────────────────────────
_s3     = None
_dynamo = None
_sns    = None

def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=AWS_REGION)
    return _s3

def _get_dynamo():
    global _dynamo
    if _dynamo is None:
        _dynamo = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _dynamo

def _get_sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", region_name=AWS_REGION)
    return _sns

# ── Helpers ───────────────────────────────────────────────────────────────────

def sha256_of_s3_object(bucket: str, key: str) -> str:
    response = _get_s3().get_object(Bucket=bucket, Key=key)
    h = hashlib.sha256()
    for chunk in response["Body"].iter_chunks(chunk_size=65536):
        h.update(chunk)
    return h.hexdigest()


def fetch_metadata(file_id: str) -> Optional[dict]:
    """Download companion metadata JSON from S3 metadata/ prefix."""
    key = f"{METADATA_PREFIX}{file_id}.json"
    try:
        obj = _get_s3().get_object(Bucket=S3_BUCKET, Key=key)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def lookup_custody_registry(country: str, legal_entity_id: str) -> Optional[dict]:
    """
    Look up the CustodyCollectionRegistry table using:
      PK = custody_country
      SK = legal_entity_id
    Returns the DynamoDB item or None.
    """
    table = _get_dynamo().Table(DYNAMODB_TABLE)
    resp = table.get_item(Key={
        "custody_country": country,
        "legal_entity_id": legal_entity_id,
    })
    return resp.get("Item")


def move_s3_object(src_key: str, dst_key: str):
    s3 = _get_s3()
    s3.copy_object(Bucket=S3_BUCKET,
                   CopySource={"Bucket": S3_BUCKET, "Key": src_key},
                   Key=dst_key)
    s3.delete_object(Bucket=S3_BUCKET, Key=src_key)


def publish_alert(subject: str, message: str):
    if not SNS_TOPIC_ARN:
        logger.warning("SNS_ALERT_TOPIC_ARN not set; skipping alert.")
        return
    # SNS Subject max = 100 characters
    _get_sns().publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)


def _snowflake_conn():
    return snowflake.connector.connect(
        account=SF_ACCOUNT, user=SF_USER, password=SF_PASSWORD,
        role=SF_ROLE, warehouse=SF_WH, database=SF_DB, schema=SF_SCHEMA,
    )


def insert_document_registry(meta: dict, status: str, location: str):
    """
    Insert or update DOCUMENT_REGISTRY.
    Uses Accountid + branch_id from metadata.
    CLIENT_ACCOUNT_ID is intentionally left NULL here — the routing stored proc
    resolves it from CLIENT_MAPPING (ACCOUNT_ID + BRANCH_ID) at routing time.
    DynamoDB holds no client information; client identity lives only in Snowflake.
    """
    sql_check = "SELECT PROCESSING_STATUS FROM DOCUMENT_REGISTRY WHERE FILE_ID = %s"
    sql_insert = """
        INSERT INTO DOCUMENT_REGISTRY
            (FILE_ID, FILE_NAME, FILE_FORMAT, CLIENT_ACCOUNT_ID, ACCOUNT_ID, BRANCH_ID,
             FILE_HASH, FILE_SIZE, CURRENT_LOCATION, PROCESSING_STATUS, SOURCE_SYSTEM,
             CREATED_TS, UPDATED_TS)
        SELECT %s,%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s,SYSDATE(),SYSDATE()
        WHERE NOT EXISTS (
            SELECT 1 FROM DOCUMENT_REGISTRY WHERE FILE_ID = %s
        )
    """
    sql_update = """
        UPDATE DOCUMENT_REGISTRY
        SET PROCESSING_STATUS = %s, CURRENT_LOCATION = %s, UPDATED_TS = SYSDATE()
        WHERE FILE_ID = %s
          AND PROCESSING_STATUS NOT IN ('AVAILABLE', 'CLASSIFIED', 'ARCHIVED')
    """
    sql_audit = """
        INSERT INTO DOCUMENT_PROCESSING_AUDIT
            (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS)
        VALUES (%s, 'VALIDATE', %s, SYSDATE(), SYSDATE())
    """
    with _snowflake_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_check, (meta["file_id"],))
            row = cur.fetchone()
            if row and row[0] in ("AVAILABLE", "CLASSIFIED", "ARCHIVED"):
                logger.info("Idempotency skip: %s already %s", meta["file_id"], row[0])
                return
            cur.execute(sql_insert, (
                meta["file_id"],
                meta["file_name"],
                meta["file_format"],
                meta.get("Accountid", ""),
                meta.get("branch_id", ""),
                meta.get("filehash", ""),
                meta.get("file_size", 0),
                location,
                status,
                meta.get("source_system", "S3"),
                meta["file_id"],
            ))
            cur.execute(sql_update, (status, location, meta["file_id"]))
            cur.execute(sql_audit, (meta["file_id"], status))
        conn.commit()


def write_audit_failed(file_id: str, step: str, error: str):
    try:
        sql = """
            INSERT INTO DOCUMENT_PROCESSING_AUDIT
                (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS, ERROR_MESSAGE)
            VALUES (%s, %s, 'FAILED', SYSDATE(), SYSDATE(), %s)
        """
        with _snowflake_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (file_id, step, error[:4000]))
            conn.commit()
    except Exception as sf_err:
        logger.error("Could not write audit FAILED row: %s", sf_err)


# ── Core validation logic ─────────────────────────────────────────────────────

def validate_document(s3_key: str) -> dict:
    """
    Validate a single document identified by its S3 incoming key.
    Returns dict with: success, file_id, reason, dst_key.
    """
    filename  = s3_key.split("/")[-1]
    file_ext  = filename.rsplit(".", 1)[-1].upper() if "." in filename else ""
    file_id_raw: Optional[str] = None

    def _quarantine(reason: str, sub: str = "unknown"):
        dst = f"{QUARANTINE_PREFIX}{sub}/{filename}"
        try:
            move_s3_object(s3_key, dst)
        except Exception as mv_err:
            logger.error("Failed to quarantine %s: %s", s3_key, mv_err)
        publish_alert(
            subject=f"IntelliDoc quarantine: {sub}",
            message=f"File: {s3_key}\nReason: {reason}\nFile ID: {file_id_raw or 'unknown'}",
        )
        if file_id_raw:
            write_audit_failed(file_id_raw, "VALIDATE", reason)
        return {"success": False, "file_id": file_id_raw, "reason": reason, "dst_key": dst}

    # 1. Basic format gate (before any DynamoDB call)
    if file_ext not in ALLOWED_FORMATS:
        return _quarantine(f"Unsupported format: {file_ext}", "bad_format")

    # 2. Locate companion metadata JSON by scanning S3 metadata/ prefix
    #    Convention: metadata/<file_id>.json where file_name == this file's name
    s3 = _get_s3()
    meta: Optional[dict] = None
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=METADATA_PREFIX):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue
            candidate_id = obj["Key"].replace(METADATA_PREFIX, "").replace(".json", "")
            candidate = fetch_metadata(candidate_id)
            if candidate and candidate.get("file_name") == filename:
                meta = candidate
                file_id_raw = candidate["file_id"]
                break
        if meta:
            break

    if not meta:
        return _quarantine("Metadata JSON not found in S3", "no_metadata")

    file_id_raw = meta["file_id"]

    # 3. SHA-256 integrity check (against filehash field in metadata)
    actual_hash = sha256_of_s3_object(S3_BUCKET, s3_key)
    expected_hash = meta.get("filehash", meta.get("file_hash", ""))
    if actual_hash != expected_hash:
        return _quarantine(
            f"SHA-256 mismatch: expected {expected_hash[:16]}..., got {actual_hash[:16]}...",
            "hash_mismatch"
        )

    # 4. DynamoDB CustodyCollectionRegistry lookup
    #    Keys: custody_country + legal_entity_id
    country        = meta.get("custody_country", "")
    legal_entity   = meta.get("legal_entity_id", "")
    meta_dest      = meta.get("Destination", meta.get("destination", ""))

    if not country or not legal_entity:
        return _quarantine(
            "Missing custody_country or legal_entity_id in metadata",
            "bad_metadata"
        )

    registry_item = lookup_custody_registry(country, legal_entity)

    if not registry_item:
        return _quarantine(
            f"Not found in CustodyCollectionRegistry: country={country}, legal_entity_id={legal_entity}",
            "unknown_collection"
        )

    # 4a. Validate destination matches
    registry_dest = registry_item.get("destination", registry_item.get("Destination", ""))
    if meta_dest and registry_dest and meta_dest.lower() != registry_dest.lower():
        return _quarantine(
            f"Destination mismatch: metadata={meta_dest}, registry={registry_dest}",
            "destination_mismatch"
        )

    # 4b. Validate active
    if not registry_item.get("active_flag", False):
        return _quarantine(
            f"CustodyCollectionRegistry entry inactive: {country}/{legal_entity}",
            "inactive_collection"
        )

    # 4c. Validate allowed format
    allowed = registry_item.get("allowed_formats", set())
    if file_ext not in allowed:
        return _quarantine(
            f"Format {file_ext} not in allowed_formats {list(allowed)} for {legal_entity}",
            "format_not_allowed"
        )

    # 5. Move to /validated/<Accountid>/<filename>
    account_id = meta.get("Accountid", legal_entity)
    dst_key    = f"{VALIDATED_PREFIX}{account_id}/{filename}"
    move_s3_object(s3_key, dst_key)

    # 6. Register in Snowflake DOCUMENT_REGISTRY
    try:
        insert_document_registry(meta, "VALIDATED", dst_key)
    except Exception as sf_err:
        logger.error("Snowflake insert failed for %s: %s", file_id_raw, sf_err)
        write_audit_failed(file_id_raw, "VALIDATE", str(sf_err))
        publish_alert(
            subject="IntelliDoc: Snowflake registration failed",
            message=f"File: {filename}\nFile ID: {file_id_raw}\nError: {sf_err}",
        )
        return {"success": False, "file_id": file_id_raw, "reason": str(sf_err), "dst_key": dst_key}

    logger.info("Validated: %s -> %s", filename, dst_key)
    return {"success": True, "file_id": file_id_raw, "reason": "OK", "dst_key": dst_key}


# ── Lambda entry point ────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """Processes SQS messages wrapping S3 EventBridge PutObject notifications."""
    successes, failures = 0, 0
    for record in event.get("Records", []):
        try:
            body   = json.loads(record["body"])
            detail = body.get("detail", body)
            s3_key = detail["object"]["key"]
        except (KeyError, json.JSONDecodeError) as e:
            logger.error("Malformed SQS record: %s — %s", record, e)
            failures += 1
            continue

        if not s3_key.startswith(INCOMING_PREFIX):
            logger.info("Skipping non-incoming key: %s", s3_key)
            continue

        try:
            result = validate_document(s3_key)
            if result["success"]:
                successes += 1
            else:
                failures += 1
                logger.warning("Validation failed for %s: %s", s3_key, result["reason"])
        except Exception:
            logger.exception("Unexpected error processing %s", s3_key)
            failures += 1

    return {
        "statusCode": 200,
        "body": json.dumps({"processed": successes + failures,
                            "successes": successes, "failures": failures}),
    }


# ── Local test harness ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    print("=" * 60)
    print("IntelliDoc Validation Lambda — local test harness")
    print("=" * 60)

    try:
        from moto import mock_aws
    except ImportError:
        print("ERROR: moto not installed. Run: pip install 'moto[s3,dynamodb,sns]'")
        sys.exit(1)

    mock_sf_conn   = MagicMock()
    mock_sf_cursor = MagicMock()
    mock_sf_cursor.fetchone.return_value = None
    mock_sf_conn.__enter__ = lambda s: s
    mock_sf_conn.__exit__  = MagicMock(return_value=False)
    mock_sf_conn.cursor.return_value.__enter__ = lambda s: mock_sf_cursor
    mock_sf_conn.cursor.return_value.__exit__  = MagicMock(return_value=False)

    os.environ.update({
        "S3_BUCKET_NAME":        "test-intellidoc",
        "DYNAMODB_TABLE_NAME":   "CustodyCollectionRegistry",
        "SNS_ALERT_TOPIC_ARN":   "arn:aws:sns:us-east-1:123456789012:test",
        "AWS_DEFAULT_REGION":    "us-east-1",
        "AWS_ACCESS_KEY_ID":     "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN":    "testing",
        "AWS_SESSION_TOKEN":     "testing",
        "SNOWFLAKE_ACCOUNT":     "test",
        "SNOWFLAKE_USER":        "test",
        "SNOWFLAKE_PASSWORD":    "test",
    })
    import importlib, sys as _sys
    mod = _sys.modules[__name__]
    mod._s3 = mod._dynamo = mod._sns = None

    BUCKET       = "test-intellidoc"
    FILE_CONTENT = b"%PDF-1.4 Global Bank Custody Billing Invoice test content"
    FILE_NAME    = "billing_invoice_INV-2024-GB-0892.pdf"
    FILE_HASH    = hashlib.sha256(FILE_CONTENT).hexdigest()
    FILE_ID      = str(uuid.uuid4())

    @mock_aws
    def run_test():
        import boto3 as b3
        s3  = b3.client("s3", region_name="us-east-1")
        ddb = b3.resource("dynamodb", region_name="us-east-1")
        sns = b3.client("sns", region_name="us-east-1")

        s3.create_bucket(Bucket=BUCKET)
        sns.create_topic(Name="test")
        ddb.create_table(
            TableName="CustodyCollectionRegistry",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "custody_country", "KeyType": "HASH"},
                {"AttributeName": "legal_entity_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "custody_country", "AttributeType": "S"},
                {"AttributeName": "legal_entity_id", "AttributeType": "S"},
            ],
        )
        ddb.Table("CustodyCollectionRegistry").put_item(Item={
            "custody_country": "US",
            "legal_entity_id": "GB-CUST-00421",
            "allowed_formats": {"PDF", "XLSX", "CSV"},
            "source": "S3", "destination": "Snowflake", "active_flag": True,
        })

        incoming_key = f"incoming/GB-CUST-00421/{FILE_NAME}"
        s3.put_object(Bucket=BUCKET, Key=incoming_key, Body=FILE_CONTENT)
        meta = {
            "file_id": FILE_ID, "file_name": FILE_NAME, "file_format": "PDF",
            "custody_country": "US",
            "legal_entity_id": "GB-CUST-00421",
            "Destination": "Snowflake", "allowed_formats": ["PDF","XLSX","CSV"],
            "Accountid": "GB-CUST-00421", "branch_id": "BRANCH-GLOBALBANK",
            "filehash": FILE_HASH, "file_size": len(FILE_CONTENT),
            "source_system": "TEST",
        }
        s3.put_object(Bucket=BUCKET, Key=f"metadata/{FILE_ID}.json",
                      Body=json.dumps(meta).encode(), ContentType="application/json")

        mod._s3    = s3
        mod._dynamo = b3.resource("dynamodb", region_name="us-east-1")

        with patch(f"{__name__}._snowflake_conn", return_value=mock_sf_conn):
            result = validate_document(incoming_key)

        print(f"\nTest result: {result}")
        assert result["success"], f"Expected success: {result}"
        s3.head_object(Bucket=BUCKET, Key=f"validated/GB-CUST-00421/{FILE_NAME}")
        print("File correctly moved to validated/")
        print("Local test PASSED.")

    run_test()
