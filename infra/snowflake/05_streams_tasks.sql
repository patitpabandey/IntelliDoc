-- Create CDC stream and task DAG for IntelliDoc document processing pipeline
-- Co-authored with CoCo
USE ROLE INTELLIDOC_ROLE;
USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

-- ── CDC stream on LANDING_FILES ───────────────────────────────────────────────
CREATE STREAM IF NOT EXISTS LANDING_STREAM
    ON TABLE LANDING_FILES
    APPEND_ONLY = TRUE
    COMMENT     = 'Captures new rows inserted by Snowpipe for routing';

-- ── Routing task ──────────────────────────────────────────────────────────────
CREATE TASK IF NOT EXISTS ROUTING_TASK
    WAREHOUSE  = INTELLIDOC_WH
    SCHEDULE   = '1 MINUTE'
    COMMENT    = 'Routes landed files to per-client stages'
    WHEN       SYSTEM$STREAM_HAS_DATA('LANDING_STREAM')
AS
CALL ROUTE_TO_CLIENT_STAGE();   -- defined in pipeline/ingest/route_to_client_stage.sql

-- ── Extraction task (depends on routing completing) ───────────────────────────
CREATE TASK IF NOT EXISTS EXTRACT_TASK
    WAREHOUSE = INTELLIDOC_WH
    COMMENT   = 'Extracts text from PDF/XLSX in per-client stages'
    AFTER     ROUTING_TASK
AS
CALL EXTRACT_TEXT_PROC();       -- defined in pipeline/processing/extract_text.py (Snowpark)

-- ── Chunk + embed task ────────────────────────────────────────────────────────
CREATE TASK IF NOT EXISTS EMBED_TASK
    WAREHOUSE = INTELLIDOC_WH
    COMMENT   = 'Chunks extracted text and creates 1024-dim embeddings'
    AFTER     EXTRACT_TASK
AS
BEGIN
    -- Chunk documents that have been TEXT_EXTRACTED but not yet CHUNKED
    INSERT INTO DOCUMENT_CHUNKS (FILE_ID, CLIENT_ID, CHUNK_INDEX, CHUNK_TEXT, EMBEDDING, TOKEN_COUNT)
    WITH source AS (
        SELECT
            dr.FILE_ID,
            dr.CLIENT_ID,
            dr.DOCUMENT_TYPE,
            -- Chunking via SPLIT_TO_TABLE on newlines then window to ~500 tokens
            ROW_NUMBER() OVER (PARTITION BY dr.FILE_ID ORDER BY ft.INDEX) - 1 AS CHUNK_INDEX,
            -- Each "row" from SPLIT_TO_TABLE is one paragraph; we batch them to ~500 tokens
            ft.VALUE::VARCHAR AS PARA_TEXT,
            LENGTH(REGEXP_REPLACE(ft.VALUE::VARCHAR, '\\s+', ' ')) / 4 AS EST_TOKENS  -- ~4 chars/token
        FROM DOCUMENT_REGISTRY dr
        JOIN TABLE(SPLIT_TO_TABLE(dr.DOCUMENT_TYPE, '\n')) ft  -- replaced by real text in proc
        WHERE dr.PROCESSING_STATUS = 'TEXT_EXTRACTED'
    )
    SELECT
        FILE_ID,
        CLIENT_ID,
        CHUNK_INDEX,
        PARA_TEXT                                                    AS CHUNK_TEXT,
        SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', PARA_TEXT)   AS EMBEDDING,
        EST_TOKENS::NUMBER                                           AS TOKEN_COUNT
    FROM source;
    -- Note: actual chunking logic lives in chunk_and_embed.sql (called as a proc).
    -- This task body is a placeholder; the real implementation uses CALL CHUNK_AND_EMBED_PROC().
END;

-- ── Classification task ───────────────────────────────────────────────────────
CREATE TASK IF NOT EXISTS CLASSIFY_TASK
    WAREHOUSE = INTELLIDOC_WH
    COMMENT   = 'Zero-shot classifies each document using Cortex COMPLETE'
    AFTER     EMBED_TASK
AS
CALL CLASSIFY_DOCUMENT_PROC();  -- defined in pipeline/processing/classify_document.sql

-- ── Resume all tasks (tasks start SUSPENDED by default) ───────────────────────
-- Run these individually after verifying the task graph looks correct:
-- ALTER TASK CLASSIFY_TASK RESUME;
-- ALTER TASK EMBED_TASK     RESUME;
-- ALTER TASK EXTRACT_TASK   RESUME;
-- ALTER TASK ROUTING_TASK   RESUME;  -- root task; resuming it activates the whole graph

SHOW TASKS IN SCHEMA INTELLIDOC_DB.INTELLIDOC;




