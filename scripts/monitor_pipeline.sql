-- ============================================================
-- monitor_pipeline.sql
-- IntelliDoc end-to-end pipeline monitoring queries.
-- Run individual sections in Snowflake Worksheet as needed.
-- ============================================================

USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

-- ═══════════════════════════════════════════════════════════════════════════
-- STAGE 3 — Snowpipe status
-- ═══════════════════════════════════════════════════════════════════════════

-- Is the pipe running? Any files waiting?
SELECT PARSE_JSON(
    SYSTEM$PIPE_STATUS('INTELLIDOC_DB.INTELLIDOC.INTELLIDOC_LANDING_PIPE')
) AS pipe_status;

-- Copy history: what did the pipe load? Any errors?
SELECT
    FILE_NAME,
    STATUS,
    ROW_COUNT,
    FIRST_ERROR_MESSAGE,
    LAST_LOAD_TIME
FROM TABLE(INFORMATION_SCHEMA.COPY_HISTORY(
    TABLE_NAME => 'INTELLIDOC_DB.INTELLIDOC.LANDING_FILES',
    START_TIME => DATEADD(HOURS, -24, CURRENT_TIMESTAMP())
))
ORDER BY LAST_LOAD_TIME DESC;

-- What files are currently in LANDING_FILES?
SELECT RELATIVE_PATH, FILE_SIZE, LAST_MODIFIED, LOADED_TS
FROM   LANDING_FILES
ORDER BY LOADED_TS DESC
LIMIT 20;


-- ═══════════════════════════════════════════════════════════════════════════
-- STAGE 4 — Stream + Routing Task
-- ═══════════════════════════════════════════════════════════════════════════

-- Does the stream have unprocessed data?
SELECT SYSTEM$STREAM_HAS_DATA('INTELLIDOC_DB.INTELLIDOC.LANDING_STREAM') AS has_data;

-- See raw stream contents
SELECT * FROM INTELLIDOC_DB.INTELLIDOC.LANDING_STREAM;

-- Routing task run history (last 24h)
SELECT
    NAME,
    STATE,
    SCHEDULED_TIME,
    COMPLETED_TIME,
    DATEDIFF(SECOND, SCHEDULED_TIME, COMPLETED_TIME) AS duration_secs,
    RETURN_VALUE,      -- { routed: X, skipped: Y, failed: Z }
    ERROR_MESSAGE
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
    SCHEDULED_TIME_RANGE_START => DATEADD(HOURS, -24, CURRENT_TIMESTAMP()),
    TASK_NAME => 'ROUTING_TASK'
))
ORDER BY SCHEDULED_TIME DESC
LIMIT 20;


-- ═══════════════════════════════════════════════════════════════════════════
-- STAGE 5 — Full Task DAG
-- ═══════════════════════════════════════════════════════════════════════════

-- All 4 tasks — state + last run result
SELECT
    NAME,
    STATE,
    SCHEDULED_TIME,
    COMPLETED_TIME,
    DATEDIFF(SECOND, SCHEDULED_TIME, COMPLETED_TIME) AS duration_secs,
    STATE        AS run_state,
    RETURN_VALUE,
    ERROR_MESSAGE
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
    SCHEDULED_TIME_RANGE_START => DATEADD(HOURS, -24, CURRENT_TIMESTAMP())
))
WHERE NAME IN ('ROUTING_TASK','EXTRACT_TASK','EMBED_TASK','CLASSIFY_TASK')
QUALIFY ROW_NUMBER() OVER (PARTITION BY NAME ORDER BY SCHEDULED_TIME DESC) = 1
ORDER BY
    CASE NAME
        WHEN 'ROUTING_TASK'  THEN 1
        WHEN 'EXTRACT_TASK'  THEN 2
        WHEN 'EMBED_TASK'    THEN 3
        WHEN 'CLASSIFY_TASK' THEN 4
    END;

-- Any task failures?
SELECT NAME, STATE, SCHEDULED_TIME, ERROR_CODE, ERROR_MESSAGE
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
    SCHEDULED_TIME_RANGE_START => DATEADD(HOURS, -24, CURRENT_TIMESTAMP())
))
WHERE STATE = 'FAILED'
ORDER BY SCHEDULED_TIME DESC;


-- ═══════════════════════════════════════════════════════════════════════════
-- STAGE 6 — Document Processing Audit
-- ═══════════════════════════════════════════════════════════════════════════

-- Status summary: how many files at each stage right now?
SELECT
    PROCESSING_STATUS,
    FILE_FORMAT,
    COUNT(*)        AS file_count,
    MIN(CREATED_TS) AS earliest,
    MAX(UPDATED_TS) AS latest_update
FROM DOCUMENT_REGISTRY
GROUP BY PROCESSING_STATUS, FILE_FORMAT
ORDER BY
    CASE PROCESSING_STATUS
        WHEN 'RECEIVED'        THEN 1  WHEN 'VALIDATED'      THEN 2
        WHEN 'STAGED'          THEN 3  WHEN 'TEXT_EXTRACTED'  THEN 4
        WHEN 'EMBEDDED'        THEN 5  WHEN 'AVAILABLE'       THEN 6
        WHEN 'FAILED'          THEN 7  WHEN 'ARCHIVED'        THEN 8
        ELSE 9
    END;

-- Full audit trail per file (every step)
SELECT
    dr.FILE_NAME,
    dr.FILE_FORMAT,
    dr.PROCESSING_STATUS,
    a.STEP_NAME,
    a.STATUS,
    a.START_TS,
    a.END_TS,
    DATEDIFF(SECOND, a.START_TS, a.END_TS) AS duration_secs,
    a.ERROR_MESSAGE
FROM DOCUMENT_PROCESSING_AUDIT a
JOIN DOCUMENT_REGISTRY dr ON a.FILE_ID = dr.FILE_ID
ORDER BY dr.FILE_ID, a.AUDIT_ID;

-- Files stuck in a non-terminal status for more than 30 minutes
SELECT
    FILE_ID,
    FILE_NAME,
    PROCESSING_STATUS,
    UPDATED_TS,
    DATEDIFF(MINUTE, UPDATED_TS, SYSDATE()) AS mins_stuck
FROM DOCUMENT_REGISTRY
WHERE PROCESSING_STATUS NOT IN ('AVAILABLE','ARCHIVED','FAILED')
  AND UPDATED_TS < DATEADD(MINUTE, -30, SYSDATE())
ORDER BY mins_stuck DESC;

-- Failed files with error detail
SELECT
    dr.FILE_ID,
    dr.FILE_NAME,
    dr.PROCESSING_STATUS,
    a.STEP_NAME,
    a.ERROR_MESSAGE,
    a.START_TS
FROM DOCUMENT_REGISTRY dr
JOIN DOCUMENT_PROCESSING_AUDIT a ON dr.FILE_ID = a.FILE_ID
WHERE a.STATUS = 'FAILED'
ORDER BY a.START_TS DESC;


-- ═══════════════════════════════════════════════════════════════════════════
-- STAGE 7 — AI Processing Quality
-- ═══════════════════════════════════════════════════════════════════════════

-- Classification results: what types were assigned and with what confidence?
SELECT
    DOCUMENT_TYPE,
    CLASSIFICATION_CONFIDENCE,
    COUNT(*)    AS file_count,
    MODEL_NAME
FROM DOCUMENT_CLASSIFICATION
GROUP BY DOCUMENT_TYPE, CLASSIFICATION_CONFIDENCE, MODEL_NAME
ORDER BY file_count DESC;

-- Detailed classification with summary
SELECT
    dr.FILE_NAME,
    dc.DOCUMENT_TYPE,
    dc.CLASSIFICATION_CONFIDENCE,
    dc.DOC_SUMMARY,
    dc.KEY_INDICATORS,
    dc.CLASSIFIED_TS
FROM DOCUMENT_CLASSIFICATION dc
JOIN DOCUMENT_REGISTRY dr ON dc.FILE_ID = dr.FILE_ID
ORDER BY dc.CLASSIFIED_TS DESC;

-- Chunk stats: how many chunks per file, token distribution
SELECT
    dr.FILE_NAME,
    dr.FILE_FORMAT,
    COUNT(dc.CHUNK_ID)      AS total_chunks,
    MIN(dc.TOKEN_COUNT)     AS min_tokens,
    ROUND(AVG(dc.TOKEN_COUNT))  AS avg_tokens,
    MAX(dc.TOKEN_COUNT)     AS max_tokens
FROM DOCUMENT_REGISTRY dr
JOIN DOCUMENT_CHUNKS dc ON dr.FILE_ID = dc.FILE_ID
GROUP BY dr.FILE_NAME, dr.FILE_FORMAT
ORDER BY total_chunks DESC;

-- Files fully available for search
SELECT
    dr.FILE_ID,
    dr.FILE_NAME,
    dr.FILE_FORMAT,
    dr.CLIENT_ID,
    dcl.DOCUMENT_TYPE,
    dcl.CLASSIFICATION_CONFIDENCE,
    COUNT(dch.CHUNK_ID) AS chunks,
    dr.UPDATED_TS       AS available_since
FROM DOCUMENT_REGISTRY dr
JOIN DOCUMENT_CLASSIFICATION dcl ON dr.FILE_ID = dcl.FILE_ID
JOIN DOCUMENT_CHUNKS         dch ON dr.FILE_ID = dch.FILE_ID
WHERE dr.PROCESSING_STATUS = 'AVAILABLE'
GROUP BY ALL
ORDER BY dr.UPDATED_TS DESC;


-- ═══════════════════════════════════════════════════════════════════════════
-- STAGE 8 — Search Audit
-- ═══════════════════════════════════════════════════════════════════════════

-- Recent searches
SELECT
    CLIENT_ID,
    SEARCH_TERM,
    RESULT_FILE_ID,
    SEARCH_CONFIDENCE,
    RESULT_COUNT,
    EXECUTION_TIME_MS,
    SEARCH_TS
FROM SEARCH_AUDIT
ORDER BY SEARCH_TS DESC
LIMIT 20;

-- Performance by confidence level
SELECT
    SEARCH_CONFIDENCE,
    COUNT(*)                       AS total_searches,
    ROUND(AVG(EXECUTION_TIME_MS))  AS avg_ms,
    MAX(EXECUTION_TIME_MS)         AS max_ms
FROM SEARCH_AUDIT
GROUP BY SEARCH_CONFIDENCE;


-- ═══════════════════════════════════════════════════════════════════════════
-- ONE-SHOT HEALTH DASHBOARD
-- ═══════════════════════════════════════════════════════════════════════════

WITH pipe AS (
    SELECT PARSE_JSON(
        SYSTEM$PIPE_STATUS('INTELLIDOC_DB.INTELLIDOC.INTELLIDOC_LANDING_PIPE')
    ) AS p
),
stream_check AS (
    SELECT SYSTEM$STREAM_HAS_DATA('INTELLIDOC_DB.INTELLIDOC.LANDING_STREAM') AS stream_has_data
),
available_files AS (
    SELECT COUNT(*) AS available_count
    FROM DOCUMENT_REGISTRY WHERE PROCESSING_STATUS = 'AVAILABLE'
),
failed_files AS (
    SELECT COUNT(*) AS failed_count
    FROM DOCUMENT_REGISTRY WHERE PROCESSING_STATUS = 'FAILED'
),
stuck_files AS (
    SELECT COUNT(*) AS stuck_count
    FROM DOCUMENT_REGISTRY
    WHERE PROCESSING_STATUS NOT IN ('AVAILABLE','ARCHIVED','FAILED')
      AND UPDATED_TS < DATEADD(MINUTE, -30, SYSDATE())
),
search_last_hour AS (
    SELECT COUNT(*) AS searches
    FROM SEARCH_AUDIT
    WHERE SEARCH_TS > DATEADD(HOUR, -1, SYSDATE())
)
SELECT
    p.p:executionState::VARCHAR    AS pipe_state,
    p.p:pendingFileCount::NUMBER   AS pipe_pending_files,
    s.stream_has_data              AS stream_has_data,
    a.available_count              AS files_available_to_search,
    f.failed_count                 AS files_failed,
    k.stuck_count                  AS files_stuck_30min,
    q.searches                     AS searches_last_hour,
    SYSDATE()                      AS checked_at
FROM pipe p, stream_check s, available_files a,
     failed_files f, stuck_files k, search_last_hour q;
