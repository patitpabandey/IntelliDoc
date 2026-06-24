-- ============================================================
-- 01_database_schema.sql
-- Creates the IntelliDoc database, schema, and compute warehouse.
-- Run as SYSADMIN (or equivalent) with USAGE on the ACCOUNTADMIN role
-- granted for the storage integration step in 03_stages.sql.
-- ============================================================

USE ROLE SYSADMIN;

-- ── Warehouse ────────────────────────────────────────────────
CREATE WAREHOUSE IF NOT EXISTS INTELLIDOC_WH
    WAREHOUSE_SIZE        = 'X-SMALL'
    AUTO_SUSPEND          = 60
    AUTO_RESUME           = TRUE
    INITIALLY_SUSPENDED   = TRUE
    COMMENT               = 'IntelliDoc processing warehouse';

-- ── Database ─────────────────────────────────────────────────
CREATE DATABASE IF NOT EXISTS INTELLIDOC_DB
    COMMENT = 'IntelliDoc document-intelligence platform';

-- ── Schema ───────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS INTELLIDOC_DB.INTELLIDOC
    COMMENT = 'Core IntelliDoc schema';

-- ── Application role ─────────────────────────────────────────
USE ROLE SECURITYADMIN;

CREATE ROLE IF NOT EXISTS INTELLIDOC_ROLE
    COMMENT = 'Service role for IntelliDoc pipeline';

GRANT USAGE  ON WAREHOUSE INTELLIDOC_WH          TO ROLE INTELLIDOC_ROLE;
GRANT USAGE  ON DATABASE  INTELLIDOC_DB           TO ROLE INTELLIDOC_ROLE;
GRANT USAGE  ON SCHEMA    INTELLIDOC_DB.INTELLIDOC TO ROLE INTELLIDOC_ROLE;
GRANT ALL    ON ALL TABLES IN SCHEMA INTELLIDOC_DB.INTELLIDOC TO ROLE INTELLIDOC_ROLE;
GRANT ALL    ON FUTURE TABLES IN SCHEMA INTELLIDOC_DB.INTELLIDOC TO ROLE INTELLIDOC_ROLE;

-- Grant to SYSADMIN so it can manage objects
GRANT ROLE INTELLIDOC_ROLE TO ROLE SYSADMIN;

USE ROLE SYSADMIN;
USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;
