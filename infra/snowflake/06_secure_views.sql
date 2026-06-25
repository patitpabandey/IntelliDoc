-- ============================================================
-- 06_secure_views.sql
-- Client-scoped Secure Views using CURRENT_ACCOUNT() isolation.
--
-- Isolation mechanism:
--   Every view JOINs to CLIENT_MAPPING on ACCOUNT_ID + BRANCH_ID
--   and filters WHERE cm.CLIENT_ACCOUNT_ID = CURRENT_ACCOUNT().
--   CURRENT_ACCOUNT() returns the Snowflake reader account name
--   of the session — so each reader account sees only its own files.
--
-- No session variables needed. Isolation is enforced by the
-- reader account identity itself.
-- ============================================================

USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

-- ── Client document list ──────────────────────────────────────
CREATE OR REPLACE SECURE VIEW V_MY_DOCUMENTS AS
SELECT
    dr.FILE_ID,
    dr.FILE_NAME,
    dr.FILE_FORMAT,
    dr.DOCUMENT_TYPE,
    dr.PROCESSING_STATUS,
    dr.FILE_SIZE,
    dr.CREATED_TS,
    dr.UPDATED_TS
FROM DOCUMENT_REGISTRY dr
JOIN CLIENT_MAPPING cm
    ON  dr.ACCOUNT_ID = cm.ACCOUNT_ID
    AND dr.BRANCH_ID  = cm.BRANCH_ID
WHERE cm.CLIENT_ACCOUNT_ID  = CURRENT_ACCOUNT()   -- reader account isolation
  AND cm.ACTIVE_FLAG         = TRUE
  AND dr.PROCESSING_STATUS  IN ('AVAILABLE', 'ARCHIVED');

-- ── Client document classification results ───────────────────
CREATE OR REPLACE SECURE VIEW V_MY_DOCUMENT_CLASSIFICATIONS AS
SELECT
    dr.FILE_ID,
    dr.FILE_NAME,
    dc.DOCUMENT_TYPE,
    dc.CLASSIFICATION_CONFIDENCE,
    dc.DOC_SUMMARY,
    dc.KEY_INDICATORS,
    dc.CLASSIFIED_TS
FROM DOCUMENT_REGISTRY dr
JOIN DOCUMENT_CLASSIFICATION dc ON dr.FILE_ID = dc.FILE_ID
JOIN CLIENT_MAPPING cm
    ON  dr.ACCOUNT_ID = cm.ACCOUNT_ID
    AND dr.BRANCH_ID  = cm.BRANCH_ID
WHERE cm.CLIENT_ACCOUNT_ID = CURRENT_ACCOUNT()
  AND cm.ACTIVE_FLAG        = TRUE
  AND dr.PROCESSING_STATUS IN ('AVAILABLE', 'ARCHIVED');

-- ── Client search history ─────────────────────────────────────
CREATE OR REPLACE SECURE VIEW V_MY_SEARCH_HISTORY AS
SELECT
    SEARCH_ID,
    SEARCH_TERM,
    RESULT_FILE_ID,
    SEARCH_CONFIDENCE,
    RESULT_COUNT,
    EXECUTION_TIME_MS,
    SEARCH_TS
FROM SEARCH_AUDIT
WHERE CLIENT_ACCOUNT_ID = CURRENT_ACCOUNT()
ORDER BY SEARCH_TS DESC;

-- ── Chunk-level view (debug / power users) ───────────────────
-- EMBEDDING column excluded — no vector data egress to client
CREATE OR REPLACE SECURE VIEW V_MY_DOCUMENT_CHUNKS AS
SELECT
    dc.CHUNK_ID,
    dc.FILE_ID,
    dc.CHUNK_INDEX,
    dc.CHUNK_TEXT,
    dc.TOKEN_COUNT,
    dc.CREATED_TS
FROM DOCUMENT_CHUNKS dc
JOIN CLIENT_MAPPING cm
    ON  dc.CLIENT_ACCOUNT_ID = cm.CLIENT_ACCOUNT_ID
WHERE cm.CLIENT_ACCOUNT_ID = CURRENT_ACCOUNT()
  AND cm.ACTIVE_FLAG        = TRUE;

-- ── Grant views to reader account roles ──────────────────────
-- Run once per reader account after creating the account.
-- Example for Apex Pension Fund reader account:
--
--   CREATE ROLE APEX_READER_ROLE;
--   GRANT SELECT ON VIEW V_MY_DOCUMENTS               TO ROLE APEX_READER_ROLE;
--   GRANT SELECT ON VIEW V_MY_DOCUMENT_CLASSIFICATIONS TO ROLE APEX_READER_ROLE;
--   GRANT SELECT ON VIEW V_MY_SEARCH_HISTORY           TO ROLE APEX_READER_ROLE;
--   GRANT SELECT ON VIEW V_MY_DOCUMENT_CHUNKS          TO ROLE APEX_READER_ROLE;
--   GRANT USAGE  ON WAREHOUSE INTELLIDOC_WH            TO ROLE APEX_READER_ROLE;
--   GRANT USAGE  ON DATABASE  INTELLIDOC_DB            TO ROLE APEX_READER_ROLE;
--   GRANT USAGE  ON SCHEMA    INTELLIDOC_DB.INTELLIDOC TO ROLE APEX_READER_ROLE;
--
-- The reader account's CURRENT_ACCOUNT() = 'APEX_READER' automatically
-- filters the views — no additional session setup needed.

-- ── Isolation test ────────────────────────────────────────────
-- Connect as APEX_READER account → SELECT * FROM V_MY_DOCUMENTS → only Apex files
-- Connect as MERIDIAN_READER account → SELECT * FROM V_MY_DOCUMENTS → only Meridian files

SHOW VIEWS IN SCHEMA INTELLIDOC_DB.INTELLIDOC;
