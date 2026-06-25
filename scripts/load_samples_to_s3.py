"""
load_samples_to_s3.py
Reads samples/manifest.json and uploads each file to:
  S3 /incoming/<Accountid>/<filename>       (document)
  S3 /metadata/<file_id>.json               (CBDC metadata companion)

Metadata schema matches the new CustodyCollectionRegistry DynamoDB table:
  custody_country, legal_entity_id,
  allowed_formats, Source, Destination, Accountid, branch_id, filehash

The manifest (produced by generate_sample_data.py) also includes the real
Source Files, so this uploads both synthetic and real documents in one pass.

No static AWS keys — uses IAM role of the executing environment.

Run:
    python scripts/load_samples_to_s3.py [--dry-run]
"""

import argparse
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv()

SAMPLES_DIR    = Path(__file__).parent.parent / "samples"
MANIFEST_PATH  = SAMPLES_DIR / "manifest.json"

S3_BUCKET       = os.environ["S3_BUCKET_NAME"]
INCOMING_PREFIX = os.environ.get("S3_INCOMING_PREFIX", "incoming/")
METADATA_PREFIX = os.environ.get("S3_METADATA_PREFIX", "metadata/")
AWS_REGION      = os.environ.get("AWS_REGION", "us-east-1")

# CBDC collection registry — mirrors CustodyCollectionRegistry DynamoDB items
# Key: (custody_country, legal_entity_id)
_CBDC_REGISTRY = {
    ("US", "GB-CUST-00421"): {
        "Accountid":  "GB-CUST-00421",
        "branch_id":  "BRANCH-GLOBALBANK",
        "client_id":  "CLIENT_APEX",
    },
    ("US", "GB-CUST-00532"): {
        "Accountid":  "GB-CUST-00532",
        "branch_id":  "BRANCH-GLOBALBANK",
        "client_id":  "CLIENT_MERIDIAN",
    },
    ("US", "GB-CUST-00615"): {
        "Accountid":  "GB-CUST-00615",
        "branch_id":  "BRANCH-GLOBALBANK",
        "client_id":  "CLIENT_SUMMIT",
    },
}

# Map from manifest client_id to (country, legalentityid)
_CLIENT_TO_CBDC = {
    "CLIENT_APEX":     ("US", "GB-CUST-00421"),
    "CLIENT_MERIDIAN": ("US", "GB-CUST-00532"),
    "CLIENT_SUMMIT":   ("US", "GB-CUST-00615"),
}


def _content_type(suffix: str) -> str:
    return {
        "PDF":  "application/pdf",
        "XLSX": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "CSV":  "text/csv",
    }.get(suffix.upper(), "application/octet-stream")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_file(s3, path: Path, entry: dict, dry_run: bool) -> dict:
    client_id = entry["client"]
    country, legal_entity = _CLIENT_TO_CBDC.get(client_id, ("US", "UNKNOWN"))
    cbdc_rec  = _CBDC_REGISTRY.get((country, legal_entity), {})
    account_id = cbdc_rec.get("Accountid", legal_entity)
    branch_id  = cbdc_rec.get("branch_id", "UNKNOWN")

    file_id    = str(uuid.uuid4())
    file_hash  = sha256(path)
    suffix     = path.suffix.lstrip(".").upper()

    # Metadata schema aligned with CustodyCollectionRegistry DynamoDB table
    metadata = {
        "file_id":                    file_id,
        "file_name":                  path.name,
        "file_format":                suffix,
        "custody_country":      country,
        "legal_entity_id": legal_entity,
        "allowed_formats":            ["PDF", "XLSX", "CSV"],
        "Source":                     "S3",
        "Destination":                "Snowflake",
        "Accountid":                  account_id,
        "branch_id":                  branch_id,
        "filehash":                   file_hash,
        "file_size":                  path.stat().st_size,
        "document_type_hint":         entry.get("type", "UNKNOWN"),
        "source_system":              "SAMPLE_GENERATOR",
        "upload_ts":                  datetime.now(timezone.utc).isoformat(),
    }

    # Incoming key uses Accountid (not client_id) to match validated/ prefix convention
    doc_key  = f"{INCOMING_PREFIX}{account_id}/{path.name}"
    meta_key = f"{METADATA_PREFIX}{file_id}.json"

    if dry_run:
        print(f"  [DRY-RUN] [{entry.get('type','?')}] s3://{S3_BUCKET}/{doc_key}")
        return metadata

    s3.upload_file(str(path), S3_BUCKET, doc_key,
                   ExtraArgs={"ContentType": _content_type(suffix)})
    s3.put_object(Bucket=S3_BUCKET, Key=meta_key,
                  Body=json.dumps(metadata, indent=2).encode(),
                  ContentType="application/json")
    print(f"  Uploaded [{entry.get('type','?')}] -> s3://{S3_BUCKET}/{doc_key}")
    return metadata


def main():
    parser = argparse.ArgumentParser(description="Upload IntelliDoc samples to S3")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not MANIFEST_PATH.exists():
        print("ERROR: samples/manifest.json not found. Run scripts/generate_sample_data.py first.")
        raise SystemExit(1)

    entries = json.loads(MANIFEST_PATH.read_text())
    s3 = boto3.client("s3", region_name=AWS_REGION)

    print(f"Uploading {len(entries)} files to s3://{S3_BUCKET} ...")
    by_fmt = {"PDF": 0, "CSV": 0, "XLSX": 0}
    for entry in entries:
        path = Path(entry["path"])
        if not path.exists():
            print(f"  SKIP (missing): {path.name}")
            continue
        upload_file(s3, path, entry, dry_run=args.dry_run)
        by_fmt[entry.get("format", "PDF").upper()] = by_fmt.get(entry.get("format","PDF").upper(), 0) + 1

    status = "would be " if args.dry_run else ""
    print(f"\nDone -- {sum(by_fmt.values())} files {status}uploaded.")
    for fmt, n in by_fmt.items():
        if n:
            print(f"  {fmt}: {n}")


if __name__ == "__main__":
    main()
