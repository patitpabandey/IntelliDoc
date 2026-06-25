-- ============================================================
-- chunk_and_embed.sql
-- Snowflake stored procedure: CHUNK_AND_EMBED_PROC()
--
-- For each document in status TEXT_EXTRACTED:
--   1. Splits extracted text into ~500-token chunks (50-token overlap).
--   2. Calls EMBED_TEXT_1024('e5-base-v2', chunk) per chunk.
--   3. Inserts into DOCUMENT_CHUNKS with CLIENT_ACCOUNT_ID from
--      DOCUMENT_REGISTRY (set by routing task).
--   4. Advances status: TEXT_EXTRACTED → EMBEDDED.
-- ============================================================

USE DATABASE INTELLIDOC_DB;
USE SCHEMA   INTELLIDOC_DB.INTELLIDOC;
USE WAREHOUSE INTELLIDOC_WH;

CREATE OR REPLACE PROCEDURE CHUNK_AND_EMBED_PROC()
RETURNS VARCHAR
LANGUAGE JAVASCRIPT
EXECUTE AS CALLER
AS
$$
var TARGET_TOKENS  = 500;
var OVERLAP_TOKENS = 50;
var CHARS_PER_TOK  = 4;
var TARGET_CHARS   = TARGET_TOKENS  * CHARS_PER_TOK;   -- 2000
var OVERLAP_CHARS  = OVERLAP_TOKENS * CHARS_PER_TOK;   -- 200
var embedded = 0, failed = 0;

function splitIntoChunks(text) {
    var paragraphs = text.split(/\n{2,}/);
    var chunks = [];
    var current = "";

    for (var pi = 0; pi < paragraphs.length; pi++) {
        var para = paragraphs[pi].trim();
        if (!para) continue;

        if (current.length + para.length + 1 <= TARGET_CHARS) {
            current = current ? current + "\n\n" + para : para;
        } else {
            if (current) {
                chunks.push(current);
                var overlap_start = Math.max(0, current.length - OVERLAP_CHARS);
                current = current.slice(overlap_start) + "\n\n" + para;
            } else {
                var sentences = para.split(/(?<=[.!?])\s+/);
                for (var si = 0; si < sentences.length; si++) {
                    var sent = sentences[si].trim();
                    if (current.length + sent.length + 1 <= TARGET_CHARS) {
                        current = current ? current + " " + sent : sent;
                    } else {
                        if (current) chunks.push(current);
                        var prev_end = current ? current.slice(-OVERLAP_CHARS) : "";
                        current = prev_end ? prev_end + " " + sent : sent;
                    }
                }
            }
        }
    }
    if (current.trim()) chunks.push(current.trim());
    return chunks;
}

var pending = snowflake.execute({ sqlText: `
    SELECT dr.FILE_ID, dr.CLIENT_ACCOUNT_ID, dt.EXTRACTED_TEXT
    FROM   DOCUMENT_REGISTRY dr
    JOIN   DOCUMENT_TEXT dt ON dr.FILE_ID = dt.FILE_ID
    WHERE  dr.PROCESSING_STATUS = 'TEXT_EXTRACTED'
`});

while (pending.next()) {
    var file_id           = pending.getColumnValue(1);
    var client_account_id = pending.getColumnValue(2);
    var text              = pending.getColumnValue(3) || "";

    snowflake.execute({ sqlText: `
        INSERT INTO DOCUMENT_PROCESSING_AUDIT (FILE_ID, STEP_NAME, STATUS, START_TS)
        VALUES ('${file_id}', 'CHUNK', 'STARTED', SYSDATE())
    `});

    try {
        var chunks = splitIntoChunks(text);

        snowflake.execute({ sqlText: `
            DELETE FROM DOCUMENT_CHUNKS WHERE FILE_ID = '${file_id}'
        `});

        for (var ci = 0; ci < chunks.length; ci++) {
            var chunk_text = chunks[ci].replace(/'/g, "''");
            var token_est  = Math.round(chunk_text.length / CHARS_PER_TOK);

            snowflake.execute({ sqlText: `
                INSERT INTO DOCUMENT_CHUNKS
                    (FILE_ID, CLIENT_ACCOUNT_ID, CHUNK_INDEX, CHUNK_TEXT, EMBEDDING, TOKEN_COUNT)
                VALUES (
                    '${file_id}',
                    '${client_account_id}',
                    ${ci},
                    '${chunk_text}',
                    SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', '${chunk_text}'),
                    ${token_est}
                )
            `});
        }

        snowflake.execute({ sqlText: `
            UPDATE DOCUMENT_REGISTRY
            SET PROCESSING_STATUS = 'EMBEDDED', UPDATED_TS = SYSDATE()
            WHERE FILE_ID = '${file_id}'
        `});

        snowflake.execute({ sqlText: `
            INSERT INTO DOCUMENT_PROCESSING_AUDIT (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS)
            VALUES ('${file_id}', 'EMBED', 'COMPLETED', SYSDATE(), SYSDATE())
        `});

        embedded++;

    } catch (err) {
        var err_msg = err.message.replace(/'/g, "''").substring(0, 4000);
        snowflake.execute({ sqlText: `
            INSERT INTO DOCUMENT_PROCESSING_AUDIT
                (FILE_ID, STEP_NAME, STATUS, START_TS, END_TS, ERROR_MESSAGE)
            VALUES ('${file_id}', 'EMBED', 'FAILED', SYSDATE(), SYSDATE(), '${err_msg}')
        `});
        snowflake.execute({ sqlText: `
            UPDATE DOCUMENT_REGISTRY
            SET PROCESSING_STATUS = 'FAILED', UPDATED_TS = SYSDATE()
            WHERE FILE_ID = '${file_id}'
        `});
        failed++;
    }
}

return JSON.stringify({ embedded: embedded, failed: failed });
$$;

-- CALL CHUNK_AND_EMBED_PROC();
