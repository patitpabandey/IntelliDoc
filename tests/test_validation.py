"""
test_validation.py
Unit tests for the Lambda validation handler.
Domain: Global Bank N.A. — validation via DynamoDB CustodyCollectionRegistry
  PK = custody_country, SK = legal_entity_id
  Also validates: destination, active_flag, allowed_formats, SHA-256 (filehash)
Uses moto to mock AWS — no live credentials needed.
"""

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "infra" / "lambda"))

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

from moto import mock_aws
import boto3
import validation_handler as vh

BUCKET        = "test-intellidoc"
PDF_CONTENT   = b"%PDF-1.4 Global Bank Custody Billing Invoice test"
CSV_CONTENT   = b"Txn_ID,Account_No,CUSIP\nTXN-001,GB-CUST-00421,037833100\n"
FILE_NAME_PDF = "billing_invoice_INV-2024-GB-0892.pdf"
FILE_NAME_CSV = "billing_transactions_Q1_2024.csv"

# Standard valid registry entry — no client fields stored in DynamoDB
APEX_REGISTRY = {
    "custody_country": "US",
    "legal_entity_id": "GB-CUST-00421",
    "allowed_formats": {"PDF", "XLSX", "CSV"},
    "source": "S3", "destination": "Snowflake", "active_flag": True,
}


@pytest.fixture(autouse=True)
def reset_module_clients():
    vh._s3 = vh._dynamo = vh._sns = None
    yield
    vh._s3 = vh._dynamo = vh._sns = None


@pytest.fixture
def mock_sf():
    mock_conn   = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__  = MagicMock(return_value=False)
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cursor
    mock_conn.cursor.return_value.__exit__  = MagicMock(return_value=False)
    with patch("validation_handler._snowflake_conn", return_value=mock_conn):
        yield mock_conn


def _make_infra(s3, ddb, sns):
    """Create S3 bucket, CustodyCollectionRegistry DynamoDB table, SNS topic."""
    s3.create_bucket(Bucket=BUCKET)
    sns.create_topic(Name="test")
    table = ddb.create_table(
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
    table.put_item(Item=APEX_REGISTRY)
    return table


def _upload(s3, file_id, filename, content,
            country="US", legal_entity="GB-CUST-00421",
            destination="Snowflake", account_id="GB-CUST-00421",
            extra=None):
    """Upload a test file + metadata companion to S3."""
    incoming_key = f"incoming/{account_id}/{filename}"
    s3.put_object(Bucket=BUCKET, Key=incoming_key, Body=content)
    meta = {
        "file_id":         file_id,
        "file_name":       filename,
        "file_format":     filename.rsplit(".", 1)[-1].upper(),
        "custody_country": country,
        "legal_entity_id": legal_entity,
        "allowed_formats": ["PDF", "XLSX", "CSV"],
        "Source":          "S3",
        "Destination":     destination,
        "Accountid":       account_id,
        "branch_id":       "BRANCH-GLOBALBANK",
        "filehash":        hashlib.sha256(content).hexdigest(),
        "file_size":       len(content),
        "source_system":   "TEST",
    }
    if extra:
        meta.update(extra)
    s3.put_object(Bucket=BUCKET, Key=f"metadata/{file_id}.json",
                  Body=json.dumps(meta).encode(), ContentType="application/json")
    return incoming_key


# ── Tests ──────────────────────────────────────────────────────────────────────

@mock_aws
def test_valid_pdf_moves_to_validated(mock_sf):
    """Happy-path: valid PDF from Apex (US / GB-CUST-00421) moves to /validated/."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    fid = str(uuid.uuid4())
    incoming = _upload(s3, fid, FILE_NAME_PDF, PDF_CONTENT)

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document(incoming)

    assert result["success"] is True, result
    s3.head_object(Bucket=BUCKET, Key=f"validated/GB-CUST-00421/{FILE_NAME_PDF}")


@mock_aws
def test_valid_csv_moves_to_validated(mock_sf):
    """CSV format (transaction report) validates and moves to /validated/."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    fid = str(uuid.uuid4())
    incoming = _upload(s3, fid, FILE_NAME_CSV, CSV_CONTENT)

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document(incoming)

    assert result["success"] is True, result
    s3.head_object(Bucket=BUCKET, Key=f"validated/GB-CUST-00421/{FILE_NAME_CSV}")


@mock_aws
def test_hash_mismatch_quarantined(mock_sf):
    """Tampered file (wrong filehash in metadata) goes to /quarantine/hash_mismatch/."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    fid = str(uuid.uuid4())
    tampered = PDF_CONTENT + b"TAMPERED"
    incoming = _upload(s3, fid, "tampered.pdf", tampered,
                       extra={"filehash": "deadbeef" * 8, "file_name": "tampered.pdf"})

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document(incoming)

    assert result["success"] is False
    assert "mismatch" in result["reason"].lower()
    s3.head_object(Bucket=BUCKET, Key="quarantine/hash_mismatch/tampered.pdf")


@mock_aws
def test_unknown_cbdc_collection_quarantined(mock_sf):
    """File from an unregistered CBDC collection goes to quarantine."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    fid = str(uuid.uuid4())
    incoming = _upload(s3, fid, "unknown.pdf", PDF_CONTENT,
                       country="GB", legal_entity="GB-CUST-99999",
                       account_id="GB-CUST-99999",
                       extra={"file_name": "unknown.pdf"})

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document(incoming)

    assert result["success"] is False
    assert "collection" in result["reason"].lower() or "CustodyCollectionRegistry" in result["reason"]


@mock_aws
def test_inactive_collection_quarantined(mock_sf):
    """File from an inactive CBDC collection goes to quarantine."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    # Add an inactive collection
    ddb.Table("CustodyCollectionRegistry").put_item(Item={
        "custody_country": "US",
        "legal_entity_id": "GB-CUST-99999",
        "allowed_formats": {"PDF"}, "active_flag": False,
        "source": "S3", "destination": "Snowflake",
    })

    fid = str(uuid.uuid4())
    incoming = _upload(s3, fid, "inactive.pdf", PDF_CONTENT,
                       country="US", legal_entity="GB-CUST-99999",
                       account_id="GB-CUST-99999",
                       extra={"file_name": "inactive.pdf"})

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document(incoming)

    assert result["success"] is False
    assert "inactive" in result["reason"].lower()


@mock_aws
def test_destination_mismatch_quarantined(mock_sf):
    """File with wrong Destination in metadata (not Snowflake) goes to quarantine."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    fid = str(uuid.uuid4())
    incoming = _upload(s3, fid, "wrong_dest.pdf", PDF_CONTENT,
                       destination="Databricks",
                       extra={"file_name": "wrong_dest.pdf"})

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document(incoming)

    assert result["success"] is False
    assert "destination" in result["reason"].lower() or "mismatch" in result["reason"].lower()


@mock_aws
def test_unsupported_format_quarantined(mock_sf):
    """A .txt file is rejected before any DynamoDB lookup."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    fid = str(uuid.uuid4())
    content = b"Unsupported plain text"
    s3.put_object(Bucket=BUCKET, Key="incoming/GB-CUST-00421/doc.txt", Body=content)
    s3.put_object(Bucket=BUCKET, Key=f"metadata/{fid}.json",
                  Body=json.dumps({
                      "file_id": fid, "file_name": "doc.txt", "file_format": "TXT",
                      "custody_country": "US",
                      "legal_entity_id": "GB-CUST-00421",
                      "Destination": "Snowflake",
                      "Accountid": "GB-CUST-00421", "branch_id": "BRANCH-GLOBALBANK",
                      "filehash": hashlib.sha256(content).hexdigest(),
                      "file_size": len(content), "source_system": "TEST",
                  }).encode())

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document("incoming/GB-CUST-00421/doc.txt")

    assert result["success"] is False
    assert "TXT" in result["reason"] or "format" in result["reason"].lower()


@mock_aws
def test_csv_format_not_in_allowed_list_quarantined(mock_sf):
    """A PDF-only collection rejects a CSV file."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    ddb.Table("CustodyCollectionRegistry").put_item(Item={
        "custody_country": "US",
        "legal_entity_id": "GB-CUST-00700",
        "allowed_formats": {"PDF"},
        "source": "S3", "destination": "Snowflake", "active_flag": True,
    })

    fid = str(uuid.uuid4())
    incoming = _upload(s3, fid, "transactions.csv", CSV_CONTENT,
                       country="US", legal_entity="GB-CUST-00700",
                       account_id="GB-CUST-00700",
                       extra={"file_name": "transactions.csv", "file_format": "CSV"})

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document(incoming)

    assert result["success"] is False
    assert "format" in result["reason"].lower() or "not in allowed" in result["reason"].lower()


@mock_aws
def test_missing_cbdc_fields_quarantined(mock_sf):
    """Metadata missing custody_country goes to bad_metadata quarantine."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    fid = str(uuid.uuid4())
    content = PDF_CONTENT
    incoming_key = "incoming/GB-CUST-00421/no_cbdc.pdf"
    s3.put_object(Bucket=BUCKET, Key=incoming_key, Body=content)
    # Metadata deliberately missing custody_country
    s3.put_object(Bucket=BUCKET, Key=f"metadata/{fid}.json",
                  Body=json.dumps({
                      "file_id": fid, "file_name": "no_cbdc.pdf", "file_format": "PDF",
                      "Destination": "Snowflake",
                      "Accountid": "GB-CUST-00421", "branch_id": "BRANCH-GLOBALBANK",
                      "filehash": hashlib.sha256(content).hexdigest(),
                      "file_size": len(content),
                  }).encode())

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    result = vh.validate_document(incoming_key)

    assert result["success"] is False
    assert "custody_country" in result["reason"].lower() or "missing" in result["reason"].lower()


@mock_aws
def test_lambda_handler_processes_sqs_event(mock_sf):
    """lambda_handler correctly parses SQS/EventBridge envelope."""
    s3  = boto3.client("s3", region_name="us-east-1")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    sns = boto3.client("sns", region_name="us-east-1")
    _make_infra(s3, ddb, sns)

    fid = str(uuid.uuid4())
    incoming = _upload(s3, fid, FILE_NAME_PDF, PDF_CONTENT)

    vh._s3    = s3
    vh._dynamo = boto3.resource("dynamodb", region_name="us-east-1")

    sqs_event = {"Records": [{"body": json.dumps({"detail": {"object": {"key": incoming}}})}]}

    with patch("validation_handler.validate_document") as mock_val:
        mock_val.return_value = {"success": True, "file_id": fid, "reason": "OK", "dst_key": "validated/..."}
        resp = vh.lambda_handler(sqs_event, context=None)

    body = json.loads(resp["body"])
    assert body["successes"] == 1
    assert body["failures"] == 0
