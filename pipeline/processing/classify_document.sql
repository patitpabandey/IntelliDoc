-- ============================================================
-- classify_document.sql
-- Snowflake stored procedure: CLASSIFY_DOCUMENT_PROC()
--
-- Domain: Global Bank N.A. Custody & Securities Services
--
-- For each document in status EMBEDDED:
--   1. Fetches the first ~1500 chars of extracted text + filename.
--   2. Calls SNOWFLAKE.CORTEX.COMPLETE('mistral-large', prompt)
--      with temperature=0, requesting JSON output.
--   3. Parses the JSON and validates against the custody banking vocabulary.
--   4. Inserts/updates DOCUMENT_CLASSIFICATION.
--   5. Copies document_type back to DOCUMENT_REGISTRY.
--   6. Advances status: EMBEDDED → CLASSIFIED → AVAILABLE.
--
-- Custody Banking Document Type Vocabulary:
--   CUSTODY_BILLING_INVOICE     — Periodic invoice for custody services (safekeeping,
--                                  settlement, corporate actions, tax reclaim filing)
--   PORTFOLIO_VALUATION         — Portfolio valuation / NAV statement listing holdings
--                                  at market value (equity + fixed income)
--   CUSTODY_TAX_SUMMARY         — Annual tax summary (1099-DIV/B/INT) of dividends,
--                                  capital gains and interest for a custody account
--   TAX_RECLAIM_APPLICATION     — Withholding tax reclaim application submitted under
--                                  double tax treaty provisions
--   CORPORATE_ACTIONS_BILLING   — Billing statement for corporate action events
--                                  (dividends, splits, rights issues, tender offers)
--   TRANSACTION_REPORT          — Detailed trade/transaction listing (BUY/SELL,
--                                  settlement dates, CUSIP, commissions, fees)
--   SURCHARGE_STATEMENT         — Itemised surcharges (late settlement, FX conversion,
--                                  custody minimum, regulatory filings, ad-hoc reports)
--   INCOME_REPORT               — Corporate actions income report (dividends, interest,
--                                  special dividends with WHT and processing fees)
--   TAX_PROFILE                 — Client tax profile (W-8/W-9, FATCA, CRS, treaty rates,
--                                  WHT rates, qualified intermediary status)
--   UNKNOWN                     — Cannot be classified into any of the above
-- ============================================================

USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

CREATE OR REPLACE PROCEDURE CLASSIFY_DOCUMENT_PROC()
RETURNS VARCHAR
LANGUAGE JAVASCRIPT
EXECUTE AS CALLER
AS
$$
var VALID_TYPES = [
    'CUSTODY_BILLING_INVOICE',
    'PORTFOLIO_VALUATION',
    'CUSTODY_TAX_SUMMARY',
    'TAX_RECLAIM_APPLICATION',
    'CORPORATE_ACTIONS_BILLING',
    'TRANSACTION_REPORT',
    'SURCHARGE_STATEMENT',
    'INCOME_REPORT',
    'TAX_PROFILE',
    'UNKNOWN'
];
var MODEL = 'mistral-large';
var classified = 0, failed = 0;

function sanitise(s) { return (s || '').replace(/'/g, "''"); }

function buildPrompt(filename, textSample) {
    return `You are a document classification system for Global Bank N.A. Custody & Securities Services.
Classify the following document based on its filename and content excerpt.

Filename: ${filename}
Content (first 1500 chars):
${textSample}

Choose the SINGLE best document type from this controlled list:
- CUSTODY_BILLING_INVOICE: Invoice for custody services — safekeeping fees, settlement charges, corporate action processing, income collection, tax reclaim filing, reporting
- PORTFOLIO_VALUATION: Portfolio valuation/NAV statement showing holdings at market value (equity, fixed income, cash)
- CUSTODY_TAX_SUMMARY: Annual custody tax summary (1099-DIV / 1099-B / 1099-INT) — dividends, capital gains, interest income
- TAX_RECLAIM_APPLICATION: Withholding tax reclaim application under double tax treaty provisions; lists securities, source countries, WHT rates, reclaimable amounts
- CORPORATE_ACTIONS_BILLING: Billing statement for corporate action events (cash dividends, stock splits, rights issues, tender offers, special dividends) with processing fees
- TRANSACTION_REPORT: Trade/transaction listing with BUY/SELL activity, CUSIP codes, settlement dates, commissions and settlement fees
- SURCHARGE_STATEMENT: Itemised surcharge listing — late settlement penalties, FX conversion fees, custody minimums, ad-hoc reporting charges, regulatory filing fees
- INCOME_REPORT: Corporate actions income report — cash dividends, interest income, special dividends with gross/net amounts, WHT deducted, processing fees
- TAX_PROFILE: Client tax profile record — W-8/W-9 status, FATCA/CRS classification, treaty country, WHT rates, QI status
- UNKNOWN: Cannot be confidently classified into any of the above

Respond ONLY with a valid JSON object matching this exact schema (no markdown, no code fences):
{
  "document_type": "<one of the types listed above>",
  "confidence": "<high | medium | low>",
  "key_indicators": ["<indicator 1>", "<indicator 2>", "<indicator 3>"],
  "summary": "<one sentence describing what this specific document is>"
}`;
}

function parseClassification(llm_output) {
    try {
        var clean = llm_output.replace(/```json\n?/gi, '').replace(/```/g, '').trim();
        var obj = JSON.parse(clean);
        var doc_type = (obj.document_type || 'UNKNOWN').toUpperCase().trim();
        if (VALID_TYPES.indexOf(doc_type) < 0) doc_type = 'UNKNOWN';
        return {
            document_type:   doc_type,
            confidence:      obj.confidence || 'low',
            key_indicators:  JSON.stringify(obj.key_indicators || []),
            summary:         (obj.summary || '').substring(0, 2000),
        };
    } catch(e) {
        return { document_type: 'UNKNOWN', confidence: 'low',
                 key_indicators: '[]', summary: 'Parse error: ' + e.message };
    }
}

// ── Find documents ready to classify ────────────────────────────────────────
var pending = snowflake.execute({ sqlText: `
    SELECT dr.FILE_ID, dr.FILE_NAME, dr.CLIENT_ID,
           SUBSTRING(dt.EXTRACTED_TEXT, 1, 1500) AS TEXT_SAMPLE
    FROM   DOCUMENT_REGISTRY dr
    JOIN   DOCUMENT_TEXT dt ON dr.FILE_ID = dt.FILE_ID
    WHERE  dr.PROCESSING_STATUS = 'EMBEDDED'
      AND  NOT EXISTS (
           SELECT 1 FROM DOCUMENT_CLASSIFICATION dc WHERE dc.FILE_ID = dr.FILE_ID
      )
`});

while (pending.next()) {
    var file_id     = pending.getColumnValue(1);
    var file_name   = pending.getColumnValue(2);
    var client_id   = pending.getColumnValue(3);
    var text_sample = pending.getColumnValue(4) || '';

    snowflake.execute({ sqlText: `
        INSERT INTO DOCUMENT_PROCESSING_AUDIT (FILE_ID, STEP_NAME, STATUS, START_TS)
        VALUES ('${file_id}', 'CLASSIFY', 'STARTED', SYSDATE())
    `});

    try {
        var prompt = sanitise(buildPrompt(file_name, text_sample));
        var llm_result_row = snowflake.execute({ sqlText: `
            SELECT SNOWFLAKE.CORTEX.COMPLETE(
                '${MODEL}',
                [
                    { 'role': 'user', 'content': '${prompt}' }
                ],
                { 'temperature': 0, 'max_tokens': 500 }
            ):choices[0]:messages::VARCHAR AS response
        `});
        llm_result_row.next();
        var llm_output = llm_result_row.getColumnValue(1) || '';
        var cls = parseClassification(llm_output);

        snowflake.execute({ sqlText: `
            MERGE INTO DOCUMENT_CLASSIFICATION t
            USING (SELECT '${file_id}' AS FILE_ID) s ON t.FILE_ID = s.FILE_ID
            WHEN MATCHED THEN UPDATE SET
                DOCUMENT_TYPE             = '${sanitise(cls.document_type)}',
                CLASSIFICATION_CONFIDENCE = '${sanitise(cls.confidence)}',
                KEY_INDICATORS            = '${sanitise(cls.key_indicators)}',
                DOC_SUMMARY               = '${sanitise(cls.summary)}',
                MODEL_NAME                = '${MODEL}',
                CLASSIFIED_TS             = SYSDATE()
            WHEN NOT MATCHED THEN INSERT
                (FILE_ID, DOCUMENT_TYPE, CLASSIFICATION_CONFIDENCE,
                 KEY_INDICATORS, DOC_SUMMARY, MODEL_NAME, CLASSIFIED_TS)
            VALUES
                ('${file_id}', '${sanitise(cls.document_type)}',
                 '${sanitise(cls.confidence)}', '${sanitise(cls.key_indicators)}',
                 '${sanitise(cls.summary)}', '${MODEL}', SYSDATE())
        `});

        snowflake.execute({ sqlText: `
            UPDATE DOCUMENT_REGISTRY
            SET DOCUMENT_TYPE       = '${sanitise(cls.document_type)}',
                PROCESSING_STATUS   = 'AVAILABLE',
                UPDATED_TS          = SYSDATE()
            WHERE FILE_ID = '${file_id}'
        `});

        snowflake.execute({ sqlText: `
            INSERT INTO DOCUMENT_PROCESSING_AUDIT (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS)
            VALUES ('${file_id}', 'CLASSIFY', 'COMPLETED', SYSDATE(), SYSDATE())
        `});

        classified++;

    } catch (err) {
        var err_msg = sanitise(err.message.substring(0, 4000));
        snowflake.execute({ sqlText: `
            INSERT INTO DOCUMENT_PROCESSING_AUDIT
                (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS, ERROR_MESSAGE)
            VALUES ('${file_id}', 'CLASSIFY', 'FAILED', SYSDATE(), SYSDATE(), '${err_msg}')
        `});
        snowflake.execute({ sqlText: `
            UPDATE DOCUMENT_REGISTRY
            SET PROCESSING_STATUS = 'FAILED', UPDATED_TS = SYSDATE()
            WHERE FILE_ID = '${file_id}'
        `});
        failed++;
    }
}

return JSON.stringify({ classified: classified, failed: failed });
$$;

-- CALL CLASSIFY_DOCUMENT_PROC();
