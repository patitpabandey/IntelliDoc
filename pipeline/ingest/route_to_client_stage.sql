-- ============================================================
-- route_to_client_stage.sql
-- Snowflake stored procedure: ROUTE_TO_CLIENT_STAGE()
--
-- Reads new rows from LANDING_STREAM, then for each file:
--   1. JOINs CLIENT_MAPPING on ACCOUNT_ID + BRANCH_ID to resolve
--      CLIENT_ACCOUNT_ID (reader account name) and CLIENT_STAGE_NAME.
--   2. Sets DOCUMENT_REGISTRY.CLIENT_ACCOUNT_ID.
--   3. COPY FILES → per-client internal stage.
--   4. REMOVE file from LANDING_STAGE.
--   5. Updates status → STAGED, writes audit row.
-- ============================================================

USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

CREATE OR REPLACE PROCEDURE ROUTE_TO_CLIENT_STAGE()
RETURNS VARCHAR
LANGUAGE JAVASCRIPT
EXECUTE AS CALLER
AS
$$
var routed  = 0;
var skipped = 0;
var failed  = 0;

var stream_sql = `
    SELECT DISTINCT
        ls.RELATIVE_PATH,
        dr.FILE_ID,
        dr.ACCOUNT_ID,
        dr.BRANCH_ID,
        dr.PROCESSING_STATUS,
        cm.CLIENT_ACCOUNT_ID,
        cm.CLIENT_STAGE_NAME
    FROM LANDING_STREAM ls
    JOIN DOCUMENT_REGISTRY dr
        ON SPLIT_PART(ls.RELATIVE_PATH, '/', 2) = dr.FILE_NAME
    JOIN CLIENT_MAPPING cm
        ON  dr.ACCOUNT_ID = cm.ACCOUNT_ID
        AND dr.BRANCH_ID  = cm.BRANCH_ID
        AND cm.ACTIVE_FLAG = TRUE
    WHERE ls.METADATA$ACTION = 'INSERT'
`;

var rows = snowflake.execute({ sqlText: stream_sql });

while (rows.next()) {
    var path              = rows.getColumnValue(1);
    var file_id           = rows.getColumnValue(2);
    var account_id        = rows.getColumnValue(3);
    var branch_id         = rows.getColumnValue(4);
    var status            = rows.getColumnValue(5);
    var client_account_id = rows.getColumnValue(6);
    var stage_name        = rows.getColumnValue(7);

    var terminal = ['TEXT_EXTRACTED','CHUNKED','EMBEDDED','CLASSIFIED','INDEXED','AVAILABLE','ARCHIVED'];
    if (terminal.indexOf(status) >= 0) {
        skipped++;
        continue;
    }

    snowflake.execute({
        sqlText: `INSERT INTO DOCUMENT_PROCESSING_AUDIT
                      (FILE_ID, STEP_NAME, STATUS, START_TS)
                  VALUES (?, 'ROUTE', 'STARTED', SYSDATE())`,
        binds: [file_id]
    });

    try {
        // Step 1: Set CLIENT_ACCOUNT_ID from CLIENT_MAPPING
        snowflake.execute({
            sqlText: `UPDATE DOCUMENT_REGISTRY
                      SET CLIENT_ACCOUNT_ID = ?,
                          UPDATED_TS        = SYSDATE()
                      WHERE FILE_ID             = ?
                        AND CLIENT_ACCOUNT_ID IS NULL`,
            binds: [client_account_id, file_id]
        });

        // Step 2: COPY FILES → per-client internal stage
        snowflake.execute({
            sqlText: `COPY FILES
                      INTO @${stage_name}/
                      FROM @LANDING_STAGE/${path}`
        });

        // Step 3: Remove from landing
        snowflake.execute({ sqlText: `REMOVE @LANDING_STAGE/${path}` });

        // Step 4: Update registry status and location
        snowflake.execute({
            sqlText: `UPDATE DOCUMENT_REGISTRY
                      SET PROCESSING_STATUS = 'STAGED',
                          CURRENT_LOCATION  = '@${stage_name}/' || SPLIT_PART(?, '/', -1),
                          UPDATED_TS        = SYSDATE()
                      WHERE FILE_ID = ?`,
            binds: [path, file_id]
        });

        // Step 5: Propagate CLIENT_ACCOUNT_ID to any existing DOCUMENT_CHUNKS rows
        snowflake.execute({
            sqlText: `UPDATE DOCUMENT_CHUNKS
                      SET CLIENT_ACCOUNT_ID = ?
                      WHERE FILE_ID = ?`,
            binds: [client_account_id, file_id]
        });

        snowflake.execute({
            sqlText: `INSERT INTO DOCUMENT_PROCESSING_AUDIT
                          (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS)
                      VALUES (?, 'ROUTE', 'COMPLETED', SYSDATE(), SYSDATE())`,
            binds: [file_id]
        });

        routed++;

    } catch (err) {
        snowflake.execute({
            sqlText: `INSERT INTO DOCUMENT_PROCESSING_AUDIT
                          (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS, ERROR_MESSAGE)
                      VALUES (?, 'ROUTE', 'FAILED', SYSDATE(), SYSDATE(), ?)`,
            binds: [file_id, err.message.substring(0, 4000)]
        });
        failed++;
    }
}

return JSON.stringify({ routed: routed, skipped: skipped, failed: failed });
$$;

-- CALL ROUTE_TO_CLIENT_STAGE();
