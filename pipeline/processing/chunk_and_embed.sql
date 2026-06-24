-- ============================================================
-- chunk_and_embed.sql
-- Snowflake stored procedure: CHUNK_AND_EMBED_PROC()
--
-- For each document in status TEXT_EXTRACTED:
--   1. Splits extracted text into ~500-token chunks with 50-token overlap,
--      honouring paragraph/sentence boundaries where possible.
--   2. Calls SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', chunk)
--      to produce a 1024-dimensional VECTOR(FLOAT, 1024) embedding.
--   3. Inserts one row per chunk into DOCUMENT_CHUNKS.
--   4. Advances status: TEXT_EXTRACTED → CHUNKED → EMBEDDED.
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
var CHARS_PER_TOK  = 4;   // ~4 chars per token for English text
var TARGET_CHARS   = TARGET_TOKENS  * CHARS_PER_TOK;  // 2000
var OVERLAP_CHARS  = OVERLAP_TOKENS * CHARS_PER_TOK;  // 200
var chunked = 0, embedded = 0, failed = 0;

function splitIntoChunks(text) {
    // Split on double-newline (paragraphs) first, then sentences if paragraphs are huge
    var paragraphs = text.split(/\n{2,}/);
    var chunks = [];
    var current = "";

    for (var pi = 0; pi < paragraphs.length; pi++) {
        var para = paragraphs[pi].trim();
        if (!para) continue;

        if (current.length + para.length + 1 <= TARGET_CHARS) {
            current = current ? current + "\n\n" + para : para;
        } else {
            // Flush current chunk
            if (current) {
                chunks.push(current);
                // Overlap: carry last OVERLAP_CHARS from the current chunk
                var overlap_start = Math.max(0, current.length - OVERLAP_CHARS);
                current = current.slice(overlap_start) + "\n\n" + para;
            } else {
                // Single paragraph exceeds target — split on sentences
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

// ── Find documents ready to chunk ───────────────────────────────────────────
var pending = snowflake.execute({ sqlText: `
    SELECT dr.FILE_ID, dr.CLIENT_ID, dt.EXTRACTED_TEXT
    FROM   DOCUMENT_REGISTRY dr
    JOIN   DOCUMENT_TEXT dt ON dr.FILE_ID = dt.FILE_ID
    WHERE  dr.PROCESSING_STATUS = 'TEXT_EXTRACTED'
`});

while (pending.next()) {
    var file_id   = pending.getColumnValue(1);
    var client_id = pending.getColumnValue(2);
    var text      = pending.getColumnValue(3) || "";

    // Audit start
    snowflake.execute({ sqlText: `
        INSERT INTO DOCUMENT_PROCESSING_AUDIT (FILE_ID, STEP_NAME, STATUS, START_TS)
        VALUES ('${file_id}', 'CHUNK', 'STARTED', SYSDATE())
    `});

    try {
        var chunks = splitIntoChunks(text);

        // Delete any pre-existing chunks (idempotent re-run support)
        snowflake.execute({ sqlText: `
            DELETE FROM DOCUMENT_CHUNKS WHERE FILE_ID = '${file_id}'
        `});

        for (var ci = 0; ci < chunks.length; ci++) {
            var chunk_text  = chunks[ci].replace(/'/g, "''");   // escape SQL
            var token_est   = Math.round(chunk_text.length / CHARS_PER_TOK);

            snowflake.execute({ sqlText: `
                INSERT INTO DOCUMENT_CHUNKS
                    (FILE_ID, CLIENT_ID, CHUNK_INDEX, CHUNK_TEXT, EMBEDDING, TOKEN_COUNT)
                VALUES (
                    '${file_id}',
                    '${client_id}',
                    ${ci},
                    '${chunk_text}',
                    SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', '${chunk_text}'),
                    ${token_est}
                )
            `});
        }

        // Status: CHUNKED → EMBEDDED (same pass)
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

-- Register as a Snowflake task (called by EMBED_TASK in 05_streams_tasks.sql)
-- CALL CHUNK_AND_EMBED_PROC();
