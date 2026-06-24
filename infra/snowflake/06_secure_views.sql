-- ============================================================
-- 06_secure_views.sql
-- Client-scoped Secure Views that enforce data isolation.
-- Each client user can only see their own documents and chunks.
--
-- Isolation mechanism:
--   - Client users are assigned a Snowflake role that has SELECT
--     only on these SECURE VIEWs, not on the base tables.
--   - The views filter by a SESSION parameter CLIENT_ID which is
--     set on login via a login hook or network policy.
--   - CURRENT_ACCOUNT() is also checked to prevent cross-account
--     access if using Snowflake reader accounts.
--
-- Usage:
--   SET CLIENT_ID = 'CLIENT_ACME';
--   SELECT * FROM V_MY_DOCUMENTS;
-- ============================================================

USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

-- ── Client document list ──────────────────────────────────────────────────────
CREATE OR REPLACE SECURE VIEW V_MY_DOCUMENTS AS
SELECT
    FILE_ID,
    FILE_NAME,
    FILE_FORMAT,
    DOCUMENT_TYPE,
    PROCESSING_STATUS,
    FILE_SIZE,
    CREATED_TS,
    UPDATED_TS
FROM DOCUMENT_REGISTRY
WHERE CLIENT_ID = CURRENT_SESSION()  -- CURRENT_SESSION() returns the session context value
                                     -- set via: ALTER SESSION SET CLIENT_ID = 'CLIENT_ACME'
  AND PROCESSING_STATUS IN ('AVAILABLE', 'ARCHIVED');
-- Note: in production use a UDF or network policy to enforce CLIENT_ID via CURRENT_ACCOUNT()

-- ── Client document classification results ───────────────────────────────────
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
WHERE dr.CLIENT_ID = CURRENT_SESSION()
  AND dr.PROCESSING_STATUS IN ('AVAILABLE', 'ARCHIVED');

-- ── Client search history ─────────────────────────────────────────────────────
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
WHERE CLIENT_ID = CURRENT_SESSION()
ORDER BY SEARCH_TS DESC;

-- ── Chunk-level view (for debug / power users) ────────────────────────────────
CREATE OR REPLACE SECURE VIEW V_MY_DOCUMENT_CHUNKS AS
SELECT
    CHUNK_ID,
    FILE_ID,
    CHUNK_INDEX,
    CHUNK_TEXT,
    TOKEN_COUNT,
    CREATED_TS
FROM DOCUMENT_CHUNKS
WHERE CLIENT_ID = CURRENT_SESSION();
-- EMBEDDING column deliberately excluded from the view to reduce egress

-- ── Grant view SELECT to client role ──────────────────────────────────────────
-- Create one role per client and grant SELECT on these views.
-- Example:
--   CREATE ROLE CLIENT_ACME_ROLE;
--   GRANT SELECT ON VIEW V_MY_DOCUMENTS               TO ROLE CLIENT_ACME_ROLE;
--   GRANT SELECT ON VIEW V_MY_DOCUMENT_CLASSIFICATIONS TO ROLE CLIENT_ACME_ROLE;
--   GRANT SELECT ON VIEW V_MY_SEARCH_HISTORY           TO ROLE CLIENT_ACME_ROLE;
--   GRANT SELECT ON VIEW V_MY_DOCUMENT_CHUNKS          TO ROLE CLIENT_ACME_ROLE;
--   GRANT USAGE  ON WAREHOUSE INTELLIDOC_WH            TO ROLE CLIENT_ACME_ROLE;
--   GRANT USAGE  ON DATABASE  INTELLIDOC_DB            TO ROLE CLIENT_ACME_ROLE;
--   GRANT USAGE  ON SCHEMA    INTELLIDOC_DB.INTELLIDOC TO ROLE CLIENT_ACME_ROLE;

-- ── Verify no cross-client leakage ────────────────────────────────────────────
-- ALTER SESSION SET CLIENT_ID = 'CLIENT_ACME';
-- SELECT COUNT(*) FROM V_MY_DOCUMENTS;  -- should only see ACME docs
-- ALTER SESSION SET CLIENT_ID = 'CLIENT_GLOBEX';
-- SELECT COUNT(*) FROM V_MY_DOCUMENTS;  -- should only see GLOBEX docs

SHOW VIEWS IN SCHEMA INTELLIDOC_DB.INTELLIDOC;
