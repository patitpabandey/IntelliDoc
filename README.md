# IntelliDoc — AI-Powered Document Intelligence on Snowflake Cortex

<p align="center">
  <img src="https://img.shields.io/badge/Snowflake-Cortex_AI-29B5E8?style=for-the-badge&logo=snowflake&logoColor=white"/>
  <img src="https://img.shields.io/badge/RAG-Retrieval_Augmented_Generation-8A2BE2?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/Vector_Search-VECTOR(FLOAT,1024)-FF6B6B?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/LLM-mistral--large-00C851?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/AWS-Serverless_Ingestion-FF9900?style=for-the-badge&logo=amazonaws&logoColor=white"/>
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white"/>
</p>

---

## What is IntelliDoc?

**IntelliDoc** is a production-grade, AI-powered document intelligence platform built entirely on **Snowflake Cortex**. It ingests unstructured custody banking documents — PDFs, CSVs and Excel files — and makes them searchable using plain English queries.

A user types:
> *"show me the Q1 billing invoice for Apex Pension Fund"*

IntelliDoc returns the **exact document** with an explainable confidence score and a one-sentence reason — all without keyword matching, all without leaving Snowflake.

**The key design decision:** every AI operation — embeddings, vector search, LLM re-ranking, and zero-shot classification — runs **natively inside Snowflake Cortex**. Document data never travels to an external API. No external LLM calls. No data egress. No separate vector database.

---

## The AI Pipeline — How It Works

IntelliDoc implements a **three-stage AI pipeline** inside Snowflake Cortex:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         STAGE 1 — INGESTION AI                         │
│                                                                         │
│  PDF / CSV / XLSX                                                       │
│       │                                                                 │
│       ▼                                                                 │
│  Extract Text  (Snowpark Python — pdfplumber / openpyxl / csv)         │
│       │                                                                 │
│       ▼                                                                 │
│  Chunk Text    (500 tokens, 50-token overlap, paragraph boundaries)    │
│       │                                                                 │
│       ▼                                                                 │
│  SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', chunk_text)           │
│       │   converts each chunk into a 1024-dimensional meaning vector   │
│       ▼                                                                 │
│  DOCUMENT_CHUNKS  →  EMBEDDING  VECTOR(FLOAT, 1024)  ✓ stored         │
│                                                                         │
│  SNOWFLAKE.CORTEX.COMPLETE('mistral-large', classify_prompt)           │
│       │   zero-shot classifies document type (INVOICE, TAX_SUMMARY...) │
│       ▼                                                                 │
│  DOCUMENT_CLASSIFICATION  →  type + confidence + summary  ✓ stored    │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                          STAGE 2 — RETRIEVAL AI                        │
│                                                                         │
│  User query: "show me the Q1 invoice for Apex"                         │
│       │                                                                 │
│       ▼                                                                 │
│  SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', query)                │
│       │   same model as ingestion → vectors are in the same space      │
│       ▼                                                                 │
│  VECTOR_COSINE_SIMILARITY(stored_embedding, query_vector)              │
│       │   scores every chunk by semantic closeness to the query        │
│       │   filtered by CLIENT_ID for strict data isolation              │
│       ▼                                                                 │
│  Top-5 most relevant chunks  (score: 0.924, 0.871, 0.743, ...)        │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                         STAGE 3 — GENERATION AI                        │
│                                                                         │
│  Top-5 chunks assembled into a structured prompt                       │
│       │                                                                 │
│       ▼                                                                 │
│  SNOWFLAKE.CORTEX.COMPLETE('mistral-large', rerank_prompt,            │
│                              { temperature: 0, max_tokens: 300 })      │
│       │   LLM reads all 5 excerpts and reasons which best answers      │
│       │   the query — returns JSON with index + confidence + reason    │
│       ▼                                                                 │
│  Final result:                                                          │
│  {                                                                      │
│    "file_id":       "602cc5a9-...",                                    │
│    "file_name":     "billing_invoice_INV-2024-GB-0892.pdf",           │
│    "document_type": "CUSTODY_BILLING_INVOICE",                         │
│    "confidence":    "high",                                             │
│    "reason":        "Q1 2024 invoice for Apex Pension Fund LLC         │
│                      total due $5,729.30 matching the query exactly"   │
│  }                                                                      │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Snowflake Cortex AI — Deep Dive

### 1. Dense Embeddings — `SNOWFLAKE.CORTEX.EMBED_TEXT_1024`

```sql
-- At ingestion: every 500-token chunk gets a semantic fingerprint
INSERT INTO DOCUMENT_CHUNKS (FILE_ID, CHUNK_TEXT, EMBEDDING, ...)
SELECT
    FILE_ID,
    CHUNK_TEXT,
    SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', CHUNK_TEXT) AS EMBEDDING
FROM DOCUMENT_TEXT;

-- At search time: the user query gets the same treatment
SELECT SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', 'show me the Q1 invoice')
AS QUERY_VECTOR;
```

**Why it matters:**
- Model `e5-base-v2` compresses the *meaning* of any text into a 1024-dimensional vector
- Same model used for ingestion AND search — vectors exist in the same mathematical space
- "Q1 billing invoice for Apex" and "INV-2024-GB-0892 Total Due $5,729.30" produce similar vectors even though they share no words
- Stored as Snowflake's native `VECTOR(FLOAT, 1024)` type — no external vector DB needed

---

### 2. Vector Similarity Search — `VECTOR_COSINE_SIMILARITY`

```sql
-- Find the 5 most semantically relevant chunks for this client
SELECT
    dc.FILE_ID,
    dc.CHUNK_TEXT,
    VECTOR_COSINE_SIMILARITY(dc.EMBEDDING, :query_vector) AS SCORE
FROM DOCUMENT_CHUNKS dc
JOIN DOCUMENT_REGISTRY dr ON dc.FILE_ID = dr.FILE_ID
WHERE dc.CLIENT_ID         = :client_id        -- strict data isolation
  AND dr.PROCESSING_STATUS = 'AVAILABLE'
ORDER BY SCORE DESC
LIMIT 5;
```

**Why it matters:**
- Cosine similarity measures the *angle* between two vectors — small angle = similar meaning = high score
- Scores range from 0 (unrelated) to 1 (identical meaning)
- Searches across millions of chunks in milliseconds using Snowflake's native vector indexing
- `CLIENT_ID` filter enforces multi-tenant isolation at the retrieval layer

**Example scores for query "Q1 invoice for Apex":**

| Score | Document | Why ranked here |
|-------|----------|-----------------|
| 0.924 | billing_invoice_INV-2024-GB-0892.pdf | Invoice header — direct match |
| 0.871 | billing_invoice_INV-2024-GB-0892.pdf | Invoice line items — same file, different chunk |
| 0.743 | billing_valuation_VAL-2024-GB-0331.pdf | Portfolio statement — related but wrong type |
| 0.681 | custody_billing_corporate_actions.pdf | Corporate billing — partially related |
| 0.612 | tax_reclaim_RECLAIM-2024-0042.pdf | Tax document — least relevant |

---

### 3. LLM Re-ranking — `SNOWFLAKE.CORTEX.COMPLETE`

```sql
-- Re-rank top-5 chunks using an LLM to pick the definitive answer
SELECT SNOWFLAKE.CORTEX.COMPLETE(
    'mistral-large',
    [{
        'role': 'user',
        'content': 'User searched for: "show me the Q1 invoice for Apex"
                    
                    [Candidate 1] billing_invoice_INV-2024-GB-0892.pdf
                    Invoice No. INV-2024-GB-0892  Period: Q1 2024
                    Bill To: Apex Pension Fund LLC  Total Due: $5,729.30...
                    
                    [Candidate 2] billing_valuation_VAL-2024-GB-0331.pdf
                    Portfolio Valuation Statement  Total NAV $6,333,460...
                    
                    Return JSON: { "best_candidate_index": N,
                                   "confidence": "high|medium|low",
                                   "reason": "one sentence" }'
    }],
    { 'temperature': 0, 'max_tokens': 300 }
):choices[0]:messages::VARCHAR AS RESPONSE;
```

**Why it matters:**
- Vector search is fast but purely mathematical — it finds *similar* text, not necessarily the *best answer*
- The LLM reads all 5 excerpts like a human and reasons about which actually answers the question
- `temperature: 0` ensures deterministic output — same query always picks the same document
- Returns a human-readable `reason` explaining the selection — full explainability
- Only 5 chunks sent to the LLM → fast and cost-efficient

**Why `mistral-large`:**
- Strong instruction-following → reliably returns valid JSON every time
- Deep understanding of financial and legal document terminology
- Runs natively in Snowflake Cortex — zero latency, zero data egress
- Right cost/capability balance (vs. smaller models that hallucinate format, vs. larger models that are overkill)

---

### 4. Zero-Shot Document Classification — `SNOWFLAKE.CORTEX.COMPLETE`

```sql
-- Classify document type without any training data or fine-tuning
SELECT SNOWFLAKE.CORTEX.COMPLETE(
    'mistral-large',
    [{
        'role': 'user',
        'content': 'Classify this custody banking document.
                    Filename: custody_tax_document_TAX-CUST-2024-0001.pdf
                    Content: Annual Custody Tax Summary Tax Year 2023.
                    Dividend income $21,872. Capital gains $7,100...
                    
                    Choose from: CUSTODY_BILLING_INVOICE | PORTFOLIO_VALUATION |
                    CUSTODY_TAX_SUMMARY | TAX_RECLAIM_APPLICATION |
                    CORPORATE_ACTIONS_BILLING | TRANSACTION_REPORT |
                    SURCHARGE_STATEMENT | INCOME_REPORT | TAX_PROFILE | UNKNOWN
                    
                    Return JSON: { "document_type": "...",
                                   "confidence": "high|medium|low",
                                   "key_indicators": [...],
                                   "summary": "one sentence" }'
    }],
    { 'temperature': 0, 'max_tokens': 500 }
):choices[0]:messages::VARCHAR AS CLASSIFICATION;

-- Returns:
-- {
--   "document_type":  "CUSTODY_TAX_SUMMARY",
--   "confidence":     "high",
--   "key_indicators": ["1099-DIV", "1099-B", "capital gains", "tax year 2023"],
--   "summary":        "Annual custody tax summary for Apex Pension Fund LLC
--                      covering dividend income and capital gains for tax year 2023."
-- }
```

**Why it matters:**
- No labelled training data required — the LLM reasons from document content alone
- Controlled vocabulary validation — if the LLM returns an unknown type, it is forced to `UNKNOWN`
- Key indicators extracted alongside the label — full audit trail of *why* it was classified that way
- Runs as a Snowflake Task, automatically triggered after embedding completes

---

## End-to-End Architecture

```
AWS (Ingestion & Validation)                  Snowflake (AI Processing & Search)
─────────────────────────────                 ──────────────────────────────────────
                                              
S3 /incoming/<AccountId>/file.pdf             
    │                                         
    ▼ S3 Event                                
EventBridge → SQS                             
    │                                         
    ▼                                         
Lambda Validation Handler                     
  ├─ SHA-256 integrity check (filehash)       
  ├─ DynamoDB CBDCCollection lookup           
  │    PK: cbdccollectioncountry              
  │    SK: cbdccollectionlegalentityid        
  │    validates: destination + active_flag   
  │    + allowed_formats                      
  ├─ Valid   → S3 /validated/<AccountId>/     
  └─ Invalid → S3 /quarantine/ + SNS alert   
                   │                          
                   ▼ S3 Event (auto-ingest)   
              Snowpipe                        
                   │                          
                   ▼                          
              LANDING_STAGE (external S3)     
              LANDING_FILES table             
                   │                          
                   ▼ CDC Stream               
              LANDING_STREAM                  
                   │                          
                   ▼ ROUTING_TASK (1 min)     
              ROUTE_TO_CLIENT_STAGE()         
                ├─ JOIN CLIENT_MAPPING        
                │    ACCOUNT_ID + BRANCH_ID   
                │    → resolves CLIENT_ID     
                │    → resolves STAGE_NAME    
                ├─ COPY FILES → @APEX_STAGE   
                └─ DOCUMENT_REGISTRY: STAGED  
                        │                     
                        ▼ EXTRACT_TASK        
                   EXTRACT_TEXT_PROC()        
                   (Snowpark Python)          
                   pdfplumber / openpyxl / csv
                   → DOCUMENT_TEXT: TEXT_EXTRACTED
                        │                     
                        ▼ EMBED_TASK          
                   CHUNK_AND_EMBED_PROC()     
                   500 tokens / 50 overlap    
                   EMBED_TEXT_1024 per chunk  
                   → DOCUMENT_CHUNKS: EMBEDDED
                        │                     
                        ▼ CLASSIFY_TASK       
                   CLASSIFY_DOCUMENT_PROC()   
                   COMPLETE(mistral-large)    
                   → DOCUMENT_CLASSIFICATION  
                   → DOCUMENT_REGISTRY: AVAILABLE
                        │                     
                        ▼ Search              
                   CALL CLIENT_SEARCH(query, client_id)
                   ├─ EMBED_TEXT_1024(query)  
                   ├─ VECTOR_COSINE_SIMILARITY top-5
                   ├─ COMPLETE rerank         
                   └─ return result + audit   
```

---

## Document Status Lifecycle

Every document transitions through these statuses, each written to `DOCUMENT_PROCESSING_AUDIT`:

```
RECEIVED → VALIDATED → STAGED → TEXT_EXTRACTED → EMBEDDED → CLASSIFIED → AVAILABLE → ARCHIVED
   │            │         │            │               │            │
Lambda       Lambda    Routing      Snowpark        Cortex      Cortex
validates    inserts   Task         Extract      EMBED_TEXT   COMPLETE
DynamoDB     Registry  (COPY FILES) Proc         _1024        classify
```

---

## Data Model

### Core AI Table — `DOCUMENT_CHUNKS`
Powers all vector search. One row per 500-token chunk.

```sql
CREATE TABLE DOCUMENT_CHUNKS (
    CHUNK_ID     NUMBER IDENTITY,
    FILE_ID      VARCHAR(36),
    CLIENT_ID    VARCHAR(64),          -- isolation filter on every search
    CHUNK_INDEX  NUMBER,               -- ordering within document
    CHUNK_TEXT   VARCHAR(8000),        -- the actual text sent to the LLM
    EMBEDDING    VECTOR(FLOAT, 1024),  -- Snowflake native vector type
    TOKEN_COUNT  NUMBER,
    CREATED_TS   TIMESTAMP_NTZ
);
```

### Classification Table — `DOCUMENT_CLASSIFICATION`
```sql
CREATE TABLE DOCUMENT_CLASSIFICATION (
    FILE_ID                   VARCHAR(36),
    DOCUMENT_TYPE             VARCHAR(64),   -- LLM-assigned label
    CLASSIFICATION_CONFIDENCE VARCHAR(16),   -- high | medium | low
    KEY_INDICATORS            VARCHAR(2000), -- JSON array: why the LLM chose this label
    DOC_SUMMARY               VARCHAR(2000), -- one-sentence description
    MODEL_NAME                VARCHAR(64),   -- mistral-large
    CLASSIFIED_TS             TIMESTAMP_NTZ
);
```

### Client Routing Table — `CLIENT_MAPPING`
```sql
CREATE TABLE CLIENT_MAPPING (
    ACCOUNT_ID        VARCHAR(64),  -- PK: from metadata
    BRANCH_ID         VARCHAR(64),  -- PK: from metadata
    CLIENT_ID         VARCHAR(64),  -- resolved at routing time
    CLIENT_NAME       VARCHAR(256),
    CLIENT_STAGE_NAME VARCHAR(128), -- @APEX_STAGE, @MERIDIAN_STAGE etc.
    ACTIVE_FLAG       BOOLEAN
);
```

---

## Supported Document Types

IntelliDoc classifies custody banking documents into 9 types using zero-shot LLM classification:

| Document Type | Description | Example File |
|---|---|---|
| `CUSTODY_BILLING_INVOICE` | Periodic invoice for safekeeping, settlement, tax reclaim filing | `billing_invoice_INV-2024-GB-0892.pdf` |
| `PORTFOLIO_VALUATION` | NAV statement with equity and fixed income holdings at market value | `billing_valuation_VAL-2024-GB-0331.pdf` |
| `CUSTODY_TAX_SUMMARY` | Annual 1099-DIV/B/INT summary of dividends, capital gains, interest | `custody_tax_document_TAX-CUST-2024-0001.pdf` |
| `TAX_RECLAIM_APPLICATION` | Withholding tax reclaim under double tax treaty (WHT rates, reclaimable amounts) | `tax_reclaim_RECLAIM-2024-0042.pdf` |
| `CORPORATE_ACTIONS_BILLING` | Billing for corporate event processing (dividends, splits, tender offers) | `custody_billing_corporate_actions_Q1_2024.pdf` |
| `TRANSACTION_REPORT` | BUY/SELL trade listing with CUSIP, settlement dates, commissions | `billing_transactions_Q1_2024.csv` |
| `SURCHARGE_STATEMENT` | Late settlement penalties, FX conversion fees, custody minimums | `billing_surcharges_Q1_2024.csv` |
| `INCOME_REPORT` | Dividend and interest income with WHT deducted and processing fees | `billing_corporate_actions_income_Q1_2024.csv` |
| `TAX_PROFILE` | Client W-8/W-9, FATCA/CRS status, treaty rates, QI classification | `tax_profile_GB-CUST-00421.csv` |

---

## Security & Multi-Tenancy

Data isolation is enforced at every layer:

| Layer | Mechanism |
|---|---|
| **Ingestion** | DynamoDB `CBDCCollection` validates `cbdccollectioncountry` + `cbdccollectionlegalentityid` + `destination` before any file enters Snowflake |
| **Storage** | Per-client internal Snowflake stages (`@APEX_STAGE`, `@MERIDIAN_STAGE`) — files physically separated |
| **Processing** | `CLIENT_ID` written to `DOCUMENT_CHUNKS` at routing time — every embedding carries the owner's identity |
| **Search** | `WHERE CLIENT_ID = ?` on every vector search — mathematically impossible to return another client's chunks |
| **Query access** | Snowflake Secure Views filter by session context — client users see only their own documents |
| **Credentials** | Lambda uses IAM execution roles; Snowflake accesses S3 via Storage Integration (IAM role trust) — zero static keys anywhere |

---

## Example Search Query → Result

```sql
-- Run inside Snowflake as CLIENT_APEX user
CALL CLIENT_SEARCH(
    'show me the Q1 billing invoice for Apex Pension Fund',
    'CLIENT_APEX'
);
```

```json
{
  "file_id":       "602cc5a9-2c7d-54b5-8ab0-22a2acd14d7f",
  "file_name":     "billing_invoice_INV-2024-GB-0892.pdf",
  "document_type": "CUSTODY_BILLING_INVOICE",
  "confidence":    "high",
  "reason":        "This is the Q1 2024 custody services billing invoice INV-2024-GB-0892
                    issued to Apex Pension Fund LLC for $5,729.30 covering safekeeping,
                    settlement, corporate actions and tax reclaim filing services.",
  "summary":       "Q1 2024 custody billing invoice for Apex Pension Fund LLC account
                    GB-CUST-00421 totalling $5,729.30.",
  "stage_path":    "@APEX_STAGE/billing_invoice_INV-2024-GB-0892.pdf",
  "top_scores":    [0.924, 0.871, 0.743, 0.681, 0.612],
  "execution_ms":  1243
}
```

---

## Tech Stack

### Snowflake (AI + Data Platform)
| Component | Purpose |
|---|---|
| `CORTEX.EMBED_TEXT_1024('e5-base-v2')` | Dense text embeddings — 1024 dimensions |
| `CORTEX.COMPLETE('mistral-large')` | LLM re-ranking + zero-shot classification |
| `VECTOR_COSINE_SIMILARITY` | Sub-second semantic similarity search |
| `VECTOR(FLOAT, 1024)` | Native vector storage — no external vector DB |
| Snowpipe (auto-ingest) | Event-driven file ingestion from S3 |
| Streams + Tasks | CDC-driven processing pipeline (no Airflow needed) |
| Snowpark Python | In-warehouse PDF/CSV/XLSX text extraction |
| Secure Views | Client-scoped query isolation |
| Internal Stages | Per-client encrypted document storage |

### AWS (Serverless Ingestion)
| Component | Purpose |
|---|---|
| S3 | Document storage (`/incoming/`, `/validated/`, `/quarantine/`) |
| Lambda (Python 3.11) | Document validation, SHA-256 integrity, DynamoDB lookup |
| DynamoDB `CBDCCollection` | Client registry keyed by country + legal entity ID |
| EventBridge + SQS | Event-driven Lambda trigger on S3 PutObject |
| SNS | Quarantine alerts |

### Python Libraries
| Library | Purpose |
|---|---|
| `pdfplumber` | PDF text extraction inside Snowpark |
| `openpyxl` | Excel text extraction inside Snowpark |
| `snowflake-snowpark-python` | Snowpark stored procedures |
| `boto3` | AWS S3 / DynamoDB / SNS from Lambda |

---

## Quickstart

### 1 — Clone and configure
```bash
git clone https://github.com/yourname/intellidoc.git
cd intellidoc
pip install -r requirements.txt
cp .env.example .env
# Fill in SNOWFLAKE_ACCOUNT, S3_BUCKET_NAME etc.
```

### 2 — Deploy Snowflake DDL
```sql
-- Run scripts in order in Snowflake Worksheet:
-- 01_database_schema.sql  → database, schema, warehouse, roles
-- 02_tables.sql           → all tables including VECTOR(FLOAT,1024) column
-- 03_stages.sql           → storage integration + landing + per-client stages
-- 04_snowpipe.sql         → auto-ingest pipe (1 row per file)
-- 05_streams_tasks.sql    → CDC stream + 4-task processing DAG
-- 06_secure_views.sql     → client-scoped secure views
```

### 3 — Deploy Snowpark procedures
```sql
CREATE STAGE INTELLIDOC_PYTHON_STAGE;
PUT file://pipeline/processing/extract_text.py    @INTELLIDOC_PYTHON_STAGE auto_compress=false;
PUT file://pipeline/search/client_search_proc.py  @INTELLIDOC_PYTHON_STAGE auto_compress=false;

-- Then run contents of:
-- pipeline/processing/chunk_and_embed.sql      → CHUNK_AND_EMBED_PROC()
-- pipeline/processing/classify_document.sql    → CLASSIFY_DOCUMENT_PROC()
-- pipeline/ingest/route_to_client_stage.sql    → ROUTE_TO_CLIENT_STAGE()
-- pipeline/search/client_search_proc.py        → CLIENT_SEARCH()

ALTER TASK CLASSIFY_TASK RESUME;
ALTER TASK EMBED_TASK    RESUME;
ALTER TASK EXTRACT_TASK  RESUME;
ALTER TASK ROUTING_TASK  RESUME;
```

### 4 — Seed DynamoDB and load documents
```bash
aws dynamodb create-table --cli-input-json file://infra/dynamodb/CBDCCollection_table.json
python scripts/generate_sample_data.py
python scripts/load_samples_to_s3.py
```

### 5 — Run a search
```sql
CALL CLIENT_SEARCH('withholding tax reclaim Q1 2024', 'CLIENT_APEX');
```

---

## Running Tests

```bash
pytest tests/ -v
# 57 tests: validation (10), chunking (22), search (17), source file integration (6)
```

---

## What This Project Demonstrates

### Snowflake Cortex AI
- **In-warehouse RAG** — full Retrieval-Augmented Generation pipeline running inside Snowflake with no external LLM API calls
- **Native vector operations** — `VECTOR(FLOAT, 1024)` storage and `VECTOR_COSINE_SIMILARITY` search without a separate vector database (Pinecone, Weaviate, etc.)
- **Prompt engineering** — structured JSON output enforcement, temperature control, controlled vocabulary validation
- **Snowpark Python** — deploying Python libraries (pdfplumber, openpyxl) as in-warehouse stored procedures
- **Task DAG orchestration** — chained Snowflake Tasks with CDC Streams replacing an external orchestrator

### AI / ML Engineering
- **RAG pipeline design** — why vector search alone is insufficient and how LLM re-ranking improves precision
- **Embedding model consistency** — same `e5-base-v2` model at ingestion and query time ensures comparable vector spaces
- **Zero-shot classification** — labelling documents with no training data using prompt engineering and controlled vocabularies
- **Explainable AI** — every search result includes a human-readable reason, not just a score

### Data Engineering
- **Event-driven serverless ingestion** — S3 → EventBridge → SQS → Lambda → Snowpipe with zero servers to manage
- **Multi-tenant data isolation** — enforced at storage (stages), compute (CLIENT_ID filter), and query (Secure Views) layers
- **Idempotent processing** — every step checks existing status before re-processing; safe to retry
- **Full audit trail** — every status transition written to `DOCUMENT_PROCESSING_AUDIT`

---

## Repository Structure

```
intellidoc/
├── infra/
│   ├── snowflake/
│   │   ├── 01_database_schema.sql    # warehouse, roles
│   │   ├── 02_tables.sql             # VECTOR(FLOAT,1024), all tables
│   │   ├── 03_stages.sql             # S3 integration, per-client stages
│   │   ├── 04_snowpipe.sql           # auto-ingest pipe
│   │   ├── 05_streams_tasks.sql      # CDC stream + task DAG
│   │   └── 06_secure_views.sql       # client-scoped views
│   ├── lambda/
│   │   └── validation_handler.py     # SHA-256 + DynamoDB CBDCCollection validation
│   └── dynamodb/
│       └── client_registry_table.json
├── pipeline/
│   ├── ingest/
│   │   └── route_to_client_stage.sql # COPY FILES + CLIENT_ID resolution
│   ├── processing/
│   │   ├── extract_text.py           # Snowpark: PDF/XLSX/CSV → text
│   │   ├── chunk_and_embed.sql       # 500/50 chunking + EMBED_TEXT_1024
│   │   └── classify_document.sql     # COMPLETE zero-shot classification
│   └── search/
│       └── client_search_proc.py     # RAG: embed → retrieve → rerank
├── Source Files/
│   ├── metadata/                     # CBDC metadata JSONs (one per document)
│   └── *.pdf / *.csv                 # Real Global Bank N.A. custody documents
├── scripts/
│   ├── generate_sample_data.py       # synthetic custody banking documents
│   ├── load_samples_to_s3.py         # upload with CBDC metadata
│   ├── monitor_pipeline.py           # end-to-end pipeline health monitor (Python)
│   └── monitor_pipeline.sql          # monitoring queries for Snowflake Worksheet
└── tests/
    ├── test_validation.py            # Lambda + DynamoDB CBDCCollection tests
    ├── test_chunking.py              # PDF/CSV/XLSX extraction tests
    └── test_search.py                # RAG pipeline unit tests
```

---

## Pipeline Monitoring

IntelliDoc ships with a built-in end-to-end monitoring tool that checks every stage of the pipeline in order — from S3 through Lambda, Snowpipe, Tasks, AI processing, and search.

### Python monitor (recommended)

```bash
# Full pipeline health check (last 24 hours)
python scripts/monitor_pipeline.py

# Summary dashboard only
python scripts/monitor_pipeline.py --dashboard

# Check a specific stage only
python scripts/monitor_pipeline.py --stage 6

# Custom look-back window
python scripts/monitor_pipeline.py --hours 48
```

**Stages checked:**

| Stage | What is monitored |
|---|---|
| 1 — S3 | Object counts in `/incoming/`, `/validated/`, `/quarantine/` |
| 2 — Lambda | CloudWatch error counts for the validation function |
| 3 — Snowpipe | Pipe execution state, pending file count, copy history + errors |
| 4 — Stream + Routing | `LANDING_STREAM` pending data, `ROUTING_TASK` run history + return values |
| 5 — Task DAG | `EXTRACT_TASK`, `EMBED_TASK`, `CLASSIFY_TASK` — state + last run result |
| 6 — Audit | `DOCUMENT_REGISTRY` status breakdown, stuck files (>30 min), failed steps |
| 7 — AI Quality | Classification type/confidence distribution, chunk count + token stats per file |
| 8 — Search | Recent queries, confidence breakdown, average execution time |
| Dashboard | One-line health summary — HEALTHY / NEEDS ATTENTION / WARNING |

**Example dashboard output:**

```
IntelliDoc Pipeline Monitor  |  look-back: 24h  |  2024-06-24 10:30:00

  Component                      Status       Detail
  ──────────────────────────────────────────────────────────────────────
  Snowpipe                       OK RUNNING   pending=0
  Landing Stream                 OK EMPTY     unprocessed rows
  Files Available to Search      OK 9
  Files Failed                   OK 0
  Files Stuck >30min             OK 0
  Searches (last 24h)            OK 3

  Overall: HEALTHY
```

**Warning signs and fixes:**

| Symptom | Cause | Fix |
|---|---|---|
| `pipe_state ≠ RUNNING` | Pipe suspended | `ALTER PIPE INTELLIDOC_LANDING_PIPE SET PIPE_EXECUTION_PAUSED = FALSE` |
| `pipe_pending_files > 0` for 5+ min | SQS event notification not configured | Re-wire S3 event notification with pipe's `notification_channel` ARN |
| `stream_has_data = TRUE` for 5+ min | `ROUTING_TASK` suspended | `EXECUTE TASK ROUTING_TASK` |
| `files_stuck_30min > 0` | Extract/Embed/Classify task failed | Check `DOCUMENT_PROCESSING_AUDIT` for `STATUS = 'FAILED'` |
| `files_failed > 0` | Processing error | Run Stage 6 check — `ERROR_MESSAGE` shows exact cause |

### Snowflake Worksheet monitor

For a quick check directly inside Snowflake without running Python:

```sql
-- Run scripts/monitor_pipeline.sql in Snowflake Worksheet
-- Each section is clearly labelled — run individual blocks as needed

-- One-shot health dashboard (last section of the file):
-- Returns: pipe_state, pending_files, stream_has_data,
--          files_available, files_failed, files_stuck, searches_last_hour
```

### Manual pipeline interventions

```sql
-- Force Snowpipe to pick up files already on stage (bypasses SQS)
ALTER PIPE INTELLIDOC_DB.INTELLIDOC.INTELLIDOC_LANDING_PIPE REFRESH;

-- Trigger routing task immediately (no waiting for 1-minute schedule)
EXECUTE TASK INTELLIDOC_DB.INTELLIDOC.ROUTING_TASK;

-- Re-process a specific file that failed (reset its status)
UPDATE INTELLIDOC_DB.INTELLIDOC.DOCUMENT_REGISTRY
SET PROCESSING_STATUS = 'STAGED', UPDATED_TS = SYSDATE()
WHERE FILE_ID = '<your-file-id>';

-- Resume all tasks if they were suspended
ALTER TASK INTELLIDOC_DB.INTELLIDOC.CLASSIFY_TASK RESUME;
ALTER TASK INTELLIDOC_DB.INTELLIDOC.EMBED_TASK     RESUME;
ALTER TASK INTELLIDOC_DB.INTELLIDOC.EXTRACT_TASK   RESUME;
ALTER TASK INTELLIDOC_DB.INTELLIDOC.ROUTING_TASK   RESUME;
```

---

*Built with Snowflake Cortex AI — embeddings, vector search, and LLM inference running natively inside the data warehouse.*
