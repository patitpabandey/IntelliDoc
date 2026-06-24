"""
conftest.py
Shared pytest configuration for IntelliDoc test suite.
"""
import os
import sys
from pathlib import Path

# Ensure all source trees are on sys.path
ROOT = Path(__file__).parent.parent
for subpath in [
    ROOT / "infra" / "lambda",
    ROOT / "pipeline" / "processing",
    ROOT / "pipeline" / "search",
]:
    p = str(subpath)
    if p not in sys.path:
        sys.path.insert(0, p)

# Ensure AWS mock environment variables are set before any boto3 import
os.environ.setdefault("AWS_DEFAULT_REGION",    "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID",     "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN",    "testing")
os.environ.setdefault("AWS_SESSION_TOKEN",     "testing")
os.environ.setdefault("S3_BUCKET_NAME",        "test-intellidoc")
os.environ.setdefault("DYNAMODB_TABLE_NAME",   "CLIENT_REGISTRY")
os.environ.setdefault("SNOWFLAKE_ACCOUNT",     "test")
os.environ.setdefault("SNOWFLAKE_USER",        "test")
os.environ.setdefault("SNOWFLAKE_PASSWORD",    "test")
