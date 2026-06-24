-- ============================================================
-- 04_snowpipe.sql
-- Single auto-ingest Snowpipe that loads from LANDING_STAGE into
-- a raw staging table.  S3 → EventBridge → SNS (Snowpipe listener)
-- triggers the pipe automatically on new S3 PutObject events.
--
-- After running this file, note the notification_channel from:
--   SHOW PIPES;
-- Configure that SQS ARN as an S3 event notification on the bucket.
-- ============================================================

USE ROLE INTELLIDOC_ROLE;
USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

-- ── Staging table (raw file paths ingested by Snowpipe) ───────────────────────
-- Snowpipe writes the relative path of each file it sees on the stage.
-- The routing task (05_streams_tasks.sql) reads from the stream on this table.
CREATE TABLE IF NOT EXISTS LANDING_FILES (
    RELATIVE_PATH  VARCHAR(1024) NOT NULL,   -- e.g. CLIENT_ACME/invoice.pdf
    FILE_SIZE      NUMBER,
    LAST_MODIFIED  TIMESTAMP_NTZ,
    LOADED_TS      TIMESTAMP_NTZ NOT NULL DEFAULT SYSDATE()
);

-- ── Single Snowpipe ───────────────────────────────────────────────────────────
CREATE PIPE IF NOT EXISTS INTELLIDOC_LANDING_PIPE
    AUTO_INGEST = TRUE
    COMMENT     = 'Auto-ingest pipe: S3 /validated/ → LANDING_FILES'
AS
COPY INTO LANDING_FILES (RELATIVE_PATH, FILE_SIZE, LAST_MODIFIED)
FROM (
    SELECT
        METADATA$FILENAME,
        METADATA$FILE_ROW_NUMBER,   -- reused as approximate size placeholder
        METADATA$FILE_LAST_MODIFIED
    FROM @LANDING_STAGE
)
FILE_FORMAT = (TYPE = 'CSV' SKIP_HEADER = 0 NULL_IF = (''))
ON_ERROR    = 'CONTINUE';   -- bad files logged, never block the pipe

-- Show the SQS ARN to wire into S3 event notifications
SHOW PIPES LIKE 'INTELLIDOC_LANDING_PIPE';
-- Copy notification_channel value → S3 → Properties → Event notifications → Add notification
