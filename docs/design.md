# IntelliDoc — Design Document

## 1. Purpose

IntelliDoc is an AI-powered document-intelligence platform for processing, classifying, and semantically searching unstructured tax and billing documents (PDF and Excel). All AI runs inside Snowflake Cortex — no external LLM calls, no data egress.

---

## 2. Architecture decisions

### 2.1 Why Snowflake Cortex for AI?

| Concern | Traditional approach | IntelliDoc approach |
|---|---|---|
| Data egress | Send text to external API | Zero — text stays in Snowflake |
| Auth complexity | Manage API keys for OpenAI/Cohere | One Snowflake role |
| Latency | Round-trip to external API | In-warehouse compute |
| Cost | Per-token API billing + egress | Snowflake credits |
| Compliance | Data leaves boundary | Data never leaves |

### 2.2 Embedding model choice

`e5-base-v2` via `EMBED_TEXT_1024` was chosen because:
- 1024-dimensional output balances recall and storage cost.
- Multilingual + domain-agnostic embeddings handle financial terminology well.
- Same model for ingestion AND query: guarantees comparable vector spaces.

### 2.3 Chunking strategy

- **Target**: 500 tokens (~2000 chars at 4 chars/token).
- **Overlap**: 50 tokens (200 chars) to avoid splitting key context across boundaries.
- **Split order**: paragraph breaks (`\n\n`) first, sentence boundaries second.  
  This preserves logical units (invoice line items, tax form boxes) within a single chunk.

### 2.4 Single Snowpipe vs. per-client pipes

A single Snowpipe into `LANDING_STAGE` was chosen over per-client pipes because:
- Simpler operational model (one SQS ARN to configure).
- Client routing happens in a Task (code, not config) — easier to change.
- New clients need a stage and a CLIENT_MAPPING row, not a new pipe.

### 2.5 Client isolation layers

| Layer | Mechanism |
|---|---|
| Storage | Per-client internal stages |
| Query filter | `CLIENT_ID` column in `DOCUMENT_CHUNKS` |
| View access | `SECURE VIEW` filtered by `CURRENT_SESSION()` |
| Role access | Client users granted only Secure View `SELECT` |

---

## 3. Data flow (detailed)

```
1. File upload
   S3 /incoming/<CLIENT_ID>/<filename>
   S3 /metadata/<file_id>.json         ← companion metadata JSON

2. EventBridge rule fires on S3 PutObject (incoming/)
   → SQS message → Lambda VALIDATION_HANDLER
       a. Fetch metadata JSON
       b. SHA-256 integrity check
       c. DynamoDB CLIENT_REGISTRY lookup
       d. Valid → copy to /validated/<CLIENT_ID>/<filename>
               → DOCUMENT_REGISTRY INSERT (status: VALIDATED)
       e. Invalid → /quarantine/<reason>/ + SNS alert

3. Snowpipe (auto-ingest, triggered by S3 event notification)
   s3://bucket/validated/ → LANDING_STAGE
   → COPY INTO LANDING_FILES (relative_path, size, last_modified)

4. ROUTING_TASK (every 1 min, when LANDING_STREAM has data)
   → ROUTE_TO_CLIENT_STAGE()
       COPY FILES @LANDING_STAGE/<path> → @<CLIENT_STAGE_NAME>/
       REMOVE from landing
       UPDATE DOCUMENT_REGISTRY status=STAGED

5. EXTRACT_TASK (after ROUTING_TASK)
   → EXTRACT_TEXT_PROC() (Snowpark Python)
       pdfplumber / openpyxl → DOCUMENT_TEXT table
       UPDATE status=TEXT_EXTRACTED

6. EMBED_TASK (after EXTRACT_TASK)
   → CHUNK_AND_EMBED_PROC()
       Split text into 500/50-token chunks
       EMBED_TEXT_1024('e5-base-v2', chunk) → DOCUMENT_CHUNKS.EMBEDDING
       UPDATE status=EMBEDDED

7. CLASSIFY_TASK (after EMBED_TASK)
   → CLASSIFY_DOCUMENT_PROC()
       COMPLETE('mistral-large', classify_prompt) → JSON
       INSERT DOCUMENT_CLASSIFICATION
       UPDATE DOCUMENT_REGISTRY.DOCUMENT_TYPE, status=AVAILABLE

8. CLIENT_SEARCH('query', 'CLIENT_ID')
   → embed query: EMBED_TEXT_1024('e5-base-v2', query)
   → VECTOR_COSINE_SIMILARITY top-5 (filter: CLIENT_ID + AVAILABLE)
   → COMPLETE rerank → best FILE_ID + confidence + reason
   → INSERT SEARCH_AUDIT
   → return JSON result
```

---

## 4. Security model

- **No static AWS keys anywhere**. Lambda uses its execution role; local dev uses `AWS_PROFILE`.
- **No Snowflake keys in S3**. Snowflake connects to S3 via a Storage Integration (IAM role trust).
- **Secrets Manager** recommended for `SNOWFLAKE_PASSWORD` in Lambda (injected as env var at runtime, not hardcoded).
- **Snowflake Secure Views** ensure clients cannot query each other's data even if they accidentally obtain another client's role.
- **SHA-256 integrity check** at ingestion prevents file tampering in transit.

---

## 5. Operational runbook

### Adding a new client

```sql
-- 1. Snowflake: add stage and mapping
CREATE STAGE NEWCLIENT_STAGE;
INSERT INTO CLIENT_MAPPING VALUES ('ACCT999','BRANCH001','CLIENT_NEW','New Client Inc','NEWCLIENT_STAGE',TRUE,SYSDATE());

-- 2. DynamoDB: add registry entry
aws dynamodb put-item --table-name CLIENT_REGISTRY --item '{"client_id":{"S":"CLIENT_NEW"},"account_id":{"S":"ACCT999"},...}'
```

### Re-processing a failed document

```sql
-- Reset status to allow re-processing
UPDATE DOCUMENT_REGISTRY SET PROCESSING_STATUS = 'STAGED', UPDATED_TS = SYSDATE() WHERE FILE_ID = '<id>';
-- The next EXTRACT_TASK run will pick it up automatically
```

### Running a manual search

```sql
USE ROLE INTELLIDOC_ROLE;
CALL CLIENT_SEARCH('show me the Q1 invoice for Acme Corp', 'CLIENT_ACME');
```
