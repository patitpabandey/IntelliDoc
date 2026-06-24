-- ============================================================
-- 02_tables.sql
-- Creates all IntelliDoc tables exactly as specified in §4.
-- Run after 01_database_schema.sql.
-- ============================================================

USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

-- ── DOCUMENT_REGISTRY ────────────────────────────────────────
-- One row per file; tracks lifecycle from RECEIVED → AVAILABLE → ARCHIVED.
CREATE TABLE IF NOT EXISTS DOCUMENT_REGISTRY (
    FILE_ID            VARCHAR(36)   NOT NULL,   -- UUID primary key
    FILE_NAME          VARCHAR(512)  NOT NULL,
    FILE_FORMAT        VARCHAR(10)   NOT NULL,   -- PDF | XLSX | CSV
    CLIENT_ID          VARCHAR(64)   ,
    ACCOUNT_ID         VARCHAR(64)   NOT NULL,
    BRANCH_ID          VARCHAR(64)   NOT NULL,
    DOCUMENT_TYPE      VARCHAR(64),              -- LLM-assigned after classification
    FILE_HASH          VARCHAR(64)   NOT NULL,   -- SHA-256
    FILE_SIZE          NUMBER        NOT NULL,   -- bytes
    CURRENT_LOCATION   VARCHAR(256),             -- stage path
    PROCESSING_STATUS  VARCHAR(32)   NOT NULL DEFAULT 'RECEIVED',
    SOURCE_SYSTEM      VARCHAR(64),
    CREATED_TS         TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    UPDATED_TS         TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    CONSTRAINT PK_DOCUMENT_REGISTRY PRIMARY KEY (FILE_ID)
);

-- ── DOCUMENT_CHUNKS ──────────────────────────────────────────
-- One row per ~500-token chunk; EMBEDDING column powers vector search.
CREATE TABLE IF NOT EXISTS DOCUMENT_CHUNKS (
    CHUNK_ID     NUMBER IDENTITY(1,1) NOT NULL,
    FILE_ID      VARCHAR(36)    NOT NULL,    -- FK → DOCUMENT_REGISTRY
    CLIENT_ID    VARCHAR(64)    NOT NULL,    -- denormalised for fast filter
    CHUNK_INDEX  NUMBER         NOT NULL,    -- 0-based ordering
    CHUNK_TEXT   VARCHAR(8000)  NOT NULL,
    EMBEDDING    VECTOR(FLOAT, 1024),        -- EMBED_TEXT_1024 output
    TOKEN_COUNT  NUMBER,
    CREATED_TS   TIMESTAMP_NTZ  NOT NULL DEFAULT SYSDATE(),
    CONSTRAINT PK_DOCUMENT_CHUNKS PRIMARY KEY (CHUNK_ID),
    CONSTRAINT FK_CHUNKS_REGISTRY FOREIGN KEY (FILE_ID)
        REFERENCES DOCUMENT_REGISTRY(FILE_ID)
);

-- ── DOCUMENT_CLASSIFICATION ───────────────────────────────────
-- One row per file; populated by the Cortex COMPLETE classification step.
CREATE TABLE IF NOT EXISTS DOCUMENT_CLASSIFICATION (
    FILE_ID                    VARCHAR(36)   NOT NULL,
    DOCUMENT_TYPE              VARCHAR(64)   NOT NULL,
    CLASSIFICATION_CONFIDENCE  VARCHAR(16),          -- high | medium | low
    KEY_INDICATORS             VARCHAR(2000),         -- JSON array string
    DOC_SUMMARY                VARCHAR(2000),         -- one-sentence summary
    MODEL_NAME                 VARCHAR(64),
    CLASSIFIED_TS              TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    CONSTRAINT PK_DOCUMENT_CLASSIFICATION PRIMARY KEY (FILE_ID),
    CONSTRAINT FK_CLASS_REGISTRY FOREIGN KEY (FILE_ID)
        REFERENCES DOCUMENT_REGISTRY(FILE_ID)
);

-- ── CLIENT_MAPPING ────────────────────────────────────────────
-- Maps ACCOUNT_ID + BRANCH_ID → CLIENT_ID; drives stage routing.
CREATE TABLE IF NOT EXISTS CLIENT_MAPPING (
    ACCOUNT_ID         VARCHAR(64)   NOT NULL,
    BRANCH_ID          VARCHAR(64)   NOT NULL,
    CLIENT_ID          VARCHAR(64)   NOT NULL,
    CLIENT_NAME        VARCHAR(256),
    CLIENT_STAGE_NAME  VARCHAR(128),             -- internal stage name
    ACTIVE_FLAG        BOOLEAN       NOT NULL DEFAULT TRUE,
    CREATED_TS         TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    CONSTRAINT PK_CLIENT_MAPPING PRIMARY KEY (ACCOUNT_ID, BRANCH_ID)
);

-- ── DOCUMENT_PROCESSING_AUDIT ─────────────────────────────────
-- Append-only audit trail; one row per step transition.
CREATE TABLE IF NOT EXISTS DOCUMENT_PROCESSING_AUDIT (
    AUDIT_ID      NUMBER IDENTITY(1,1) NOT NULL,
    FILE_ID       VARCHAR(36)   NOT NULL,
    STEP_NAME     VARCHAR(64)   NOT NULL,    -- EXTRACT | EMBED | CLASSIFY | ROUTE
    STATUS        VARCHAR(32)   NOT NULL,    -- STARTED | COMPLETED | FAILED
    START_TS      TIMESTAMP_NTZ,
    END_TS        TIMESTAMP_NTZ,
    ERROR_MESSAGE VARCHAR(4000),
    CONSTRAINT PK_AUDIT PRIMARY KEY (AUDIT_ID)
);

-- ── SEARCH_AUDIT ──────────────────────────────────────────────
-- One row per CLIENT_SEARCH invocation for observability.
CREATE TABLE IF NOT EXISTS SEARCH_AUDIT (
    SEARCH_ID         NUMBER IDENTITY(1,1) NOT NULL,
    CLIENT_ID         VARCHAR(64)   NOT NULL,
    SEARCH_TERM       VARCHAR(2000) NOT NULL,
    RESULT_FILE_ID    VARCHAR(36),
    SEARCH_CONFIDENCE VARCHAR(16),
    RESULT_COUNT      NUMBER,
    EXECUTION_TIME_MS NUMBER,
    SEARCH_TS         TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE(),
    CONSTRAINT PK_SEARCH_AUDIT PRIMARY KEY (SEARCH_ID)
);

-- ── CLIENT_MAPPING rows — Global Bank N.A. custody clients ────
-- Real source-file client: Apex Pension Fund LLC (account GB-CUST-00421)
-- Two additional synthetic clients for multi-tenant isolation testing.
INSERT INTO CLIENT_MAPPING (ACCOUNT_ID, BRANCH_ID, CLIENT_ID, CLIENT_NAME, CLIENT_STAGE_NAME, ACTIVE_FLAG)
VALUES
    ('GB-CUST-00421', 'BRANCH-GLOBALBANK', 'CLIENT_APEX',     'Apex Pension Fund LLC',        'APEX_STAGE',     TRUE),
    ('GB-CUST-00532', 'BRANCH-GLOBALBANK', 'CLIENT_MERIDIAN', 'Meridian Asset Management',    'MERIDIAN_STAGE', TRUE),
    ('GB-CUST-00615', 'BRANCH-GLOBALBANK', 'CLIENT_SUMMIT',   'Summit Endowment Fund',        'SUMMIT_STAGE',   TRUE);

-- Verify
DESCRIBE TABLE DOCUMENT_REGISTRY;
DESCRIBE TABLE DOCUMENT_CHUNKS;
DESCRIBE TABLE DOCUMENT_CLASSIFICATION;
DESCRIBE TABLE CLIENT_MAPPING;
DESCRIBE TABLE DOCUMENT_PROCESSING_AUDIT;
DESCRIBE TABLE SEARCH_AUDIT;
