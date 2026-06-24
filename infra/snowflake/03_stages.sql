-- ============================================================
-- 03_stages.sql
-- Creates the Snowflake storage integration, the LANDING_STAGE
-- (shared entry point for Snowpipe), and per-client internal stages.
--
-- Prerequisites:
--   1. Run 01_database_schema.sql and 02_tables.sql first.
--   2. The ACCOUNTADMIN must create the storage integration and
--      grant it to INTELLIDOC_ROLE.
--   3. After creating the integration, run:
--        DESC INTEGRATION INTELLIDOC_S3_INTEGRATION;
--      Copy the STORAGE_AWS_IAM_USER_ARN and STORAGE_AWS_EXTERNAL_ID
--      into the IAM trust policy of the S3 access role.
-- ============================================================

USE ROLE ACCOUNTADMIN;   -- storage integration requires ACCOUNTADMIN

-- ── Storage integration (links Snowflake → S3 via IAM role, no keys) ─────────
CREATE STORAGE INTEGRATION IF NOT EXISTS INTELLIDOC_S3_INTEGRATION
    TYPE                      = EXTERNAL_STAGE
    STORAGE_PROVIDER          = 'S3'
    ENABLED                   = TRUE
    STORAGE_AWS_ROLE_ARN      = 'arn:aws:iam::<YOUR_AWS_ACCOUNT_ID>:role/<your-iam-role-name>'  -- replace with your AWS account ID and IAM role name
    STORAGE_ALLOWED_LOCATIONS = ('s3://intellidoc-documents-source/');

-- Show the Snowflake IAM user to add to your trust policy
DESC INTEGRATION INTELLIDOC_S3_INTEGRATION;

GRANT USAGE ON INTEGRATION INTELLIDOC_S3_INTEGRATION TO ROLE INTELLIDOC_ROLE;

-- ── Switch to app role for stage creation ─────────────────────────────────────
USE ROLE INTELLIDOC_ROLE;
USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

-- ── LANDING_STAGE — single Snowpipe target ────────────────────────────────────
-- All validated documents land here first; the routing task redistributes them.
-- No FILE_FORMAT is set on the stage itself so that PDF, CSV, and XLSX files
-- are all accepted without parse errors. The COPY in the Snowpipe specifies
-- FORMAT_NAME => FILE_FORMAT via METADATA columns only (filenames, not content).
CREATE STAGE IF NOT EXISTS LANDING_STAGE
    STORAGE_INTEGRATION = INTELLIDOC_S3_INTEGRATION
    URL                 = 's3://intellidoc-documents-source/validated/'
    DIRECTORY           = (ENABLE = TRUE AUTO_REFRESH = TRUE)
    COMMENT             = 'Entry point for all validated documents (PDF, CSV, XLSX)';

-- ── Per-client internal stages ────────────────────────────────────────────────
-- Internal stages — data lives inside Snowflake, never egresses back to S3.
-- Add a new CREATE STAGE for each onboarded custody client.
CREATE STAGE IF NOT EXISTS APEX_STAGE
    COMMENT = 'Apex Pension Fund LLC — document stage (account GB-CUST-00421)';

CREATE STAGE IF NOT EXISTS MERIDIAN_STAGE
    COMMENT = 'Meridian Asset Management — document stage (account GB-CUST-00532)';

CREATE STAGE IF NOT EXISTS SUMMIT_STAGE
    COMMENT = 'Summit Endowment Fund — document stage (account GB-CUST-00615)';

-- Helper: verify stages exist
SHOW STAGES IN SCHEMA INTELLIDOC_DB.INTELLIDOC;
