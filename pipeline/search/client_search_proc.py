"""
client_search_proc.py
Snowpark Python stored procedure: CLIENT_SEARCH(query VARCHAR)

The calling session's reader account is identified via CURRENT_ACCOUNT().
No client_id parameter needed — isolation is enforced by the account identity.

RAG pipeline (§7.3):
  a. Embed query using EMBED_TEXT_1024('e5-base-v2', query)
  b. VECTOR_COSINE_SIMILARITY top-5 filtered by CLIENT_ACCOUNT_ID = CURRENT_ACCOUNT()
  c. LLM re-rank: COMPLETE → best FILE_ID + confidence + reason
  d. Fetch metadata, write SEARCH_AUDIT, return JSON result
  e. Fallback: if COMPLETE fails → return top vector hit

Deploy:
    CREATE OR REPLACE PROCEDURE CLIENT_SEARCH(QUERY VARCHAR)
    RETURNS VARIANT
    LANGUAGE PYTHON
    RUNTIME_VERSION = '3.11'
    PACKAGES = ('snowflake-snowpark-python')
    IMPORTS = ('@INTELLIDOC_PYTHON_STAGE/client_search_proc.py')
    HANDLER = 'client_search_proc.run'
    EXECUTE AS CALLER;
"""

from __future__ import annotations
import json
import time


def _rerank_prompt(query: str, candidates: list[dict]) -> str:
    chunks_text = "\n\n".join(
        f"[Candidate {i+1}] FILE_ID={c['FILE_ID']}\n{c['CHUNK_TEXT'][:800]}"
        for i, c in enumerate(candidates)
    )
    return f"""You are a document retrieval assistant for a custody banking platform.

A user has searched for: "{query}"

Below are {len(candidates)} candidate document excerpts. Identify which single
candidate best answers the user's query.

{chunks_text}

Respond ONLY with a JSON object:
{{
  "best_candidate_index": <1-based integer>,
  "confidence": "<high | medium | low>",
  "reason": "<one sentence explaining why this document best matches the query>"
}}

Return only the JSON object with no markdown or explanation."""


def _parse_rerank(llm_output: str, candidates: list[dict]) -> dict:
    try:
        clean = llm_output.replace("```json", "").replace("```", "").strip()
        obj   = json.loads(clean)
        idx   = max(0, min(int(obj.get("best_candidate_index", 1)) - 1, len(candidates) - 1))
        return {
            "file_id":    candidates[idx]["FILE_ID"],
            "confidence": obj.get("confidence", "medium"),
            "reason":     str(obj.get("reason", ""))[:500],
        }
    except Exception:
        return {
            "file_id":    candidates[0]["FILE_ID"],
            "confidence": "low",
            "reason":     "LLM rerank parse failed — returning top vector result.",
        }


def run(session, query: str) -> dict:
    """
    Snowpark handler. client_account_id resolved from CURRENT_ACCOUNT()
    so no client identifier needs to be passed by the caller.
    """
    t0 = time.time()

    # ── Resolve caller's reader account ──────────────────────────────────────
    acct_row          = session.sql("SELECT CURRENT_ACCOUNT() AS CA").collect()
    client_account_id = acct_row[0]["CA"]

    # ── a. Embed the query ────────────────────────────────────────────────────
    embed_row = session.sql(
        "SELECT SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', ?) AS Q_EMBEDDING",
        params=[query]
    ).collect()
    query_vec = embed_row[0]["Q_EMBEDDING"]
    vec_str   = str(query_vec)

    # ── b. Vector similarity retrieval (top-5, reader account scoped) ─────────
    top_chunks = session.sql(f"""
        SELECT
            dc.FILE_ID,
            dc.CHUNK_TEXT,
            dc.CHUNK_INDEX,
            VECTOR_COSINE_SIMILARITY(
                dc.EMBEDDING,
                {vec_str}::VECTOR(FLOAT, 1024)
            ) AS SCORE
        FROM DOCUMENT_CHUNKS dc
        JOIN DOCUMENT_REGISTRY dr ON dc.FILE_ID = dr.FILE_ID
        WHERE dc.CLIENT_ACCOUNT_ID  = ?
          AND dr.PROCESSING_STATUS  = 'AVAILABLE'
        ORDER BY SCORE DESC
        LIMIT 5
    """, params=[client_account_id]).collect()

    if not top_chunks:
        return {
            "error":   "No documents found for this account.",
            "file_id": None,
            "query":   query,
        }

    candidates = [dict(row.as_dict()) for row in top_chunks]

    # ── c. LLM re-rank ────────────────────────────────────────────────────────
    try:
        prompt    = _rerank_prompt(query, candidates).replace("'", "\\'")
        llm_row   = session.sql(f"""
            SELECT SNOWFLAKE.CORTEX.COMPLETE(
                'mistral-large',
                [{{ 'role': 'user', 'content': '{prompt}' }}],
                {{ 'temperature': 0, 'max_tokens': 300 }}
            ):choices[0]:messages::VARCHAR AS response
        """).collect()
        llm_output    = llm_row[0]["RESPONSE"] if llm_row else ""
        rerank_result = _parse_rerank(llm_output, candidates)
    except Exception as llm_err:
        rerank_result = {
            "file_id":    candidates[0]["FILE_ID"],
            "confidence": "low",
            "reason":     f"LLM rerank unavailable ({llm_err}); returning top vector result.",
        }

    best_file_id = rerank_result["file_id"]

    # ── d. Fetch metadata for winning file ────────────────────────────────────
    meta_rows = session.sql("""
        SELECT
            dr.FILE_ID, dr.FILE_NAME, dr.FILE_FORMAT,
            dr.DOCUMENT_TYPE, dr.CLIENT_ACCOUNT_ID, dr.CURRENT_LOCATION,
            dc.CLASSIFICATION_CONFIDENCE, dc.DOC_SUMMARY
        FROM DOCUMENT_REGISTRY dr
        LEFT JOIN DOCUMENT_CLASSIFICATION dc ON dr.FILE_ID = dc.FILE_ID
        WHERE dr.FILE_ID = ?
    """, params=[best_file_id]).collect()

    meta    = dict(meta_rows[0].as_dict()) if meta_rows else {}
    exec_ms = int((time.time() - t0) * 1000)

    # ── e. Write SEARCH_AUDIT ─────────────────────────────────────────────────
    try:
        session.sql("""
            INSERT INTO SEARCH_AUDIT
                (CLIENT_ACCOUNT_ID, SEARCH_TERM, RESULT_FILE_ID, SEARCH_CONFIDENCE,
                 RESULT_COUNT, EXECUTION_TIME_MS)
            VALUES (?, ?, ?, ?, ?, ?)
        """, params=[
            client_account_id, query, best_file_id,
            rerank_result["confidence"], len(candidates), exec_ms
        ]).collect()
    except Exception:
        pass

    # ── f. Return result ──────────────────────────────────────────────────────
    return {
        "file_id":            best_file_id,
        "file_name":          meta.get("FILE_NAME"),
        "file_format":        meta.get("FILE_FORMAT"),
        "document_type":      meta.get("DOCUMENT_TYPE"),
        "client_account_id":  client_account_id,
        "confidence":         rerank_result["confidence"],
        "reason":             rerank_result["reason"],
        "summary":            meta.get("DOC_SUMMARY"),
        "stage_path":         meta.get("CURRENT_LOCATION"),
        "result_count":       len(candidates),
        "top_scores":         [round(float(c["SCORE"]), 4) for c in candidates],
        "execution_ms":       exec_ms,
    }


# ── Deployment DDL ─────────────────────────────────────────────────────────────
DEPLOY_SQL = """
PUT file://pipeline/search/client_search_proc.py @INTELLIDOC_PYTHON_STAGE auto_compress=false;

CREATE OR REPLACE PROCEDURE CLIENT_SEARCH(QUERY VARCHAR)
    RETURNS VARIANT
    LANGUAGE PYTHON
    RUNTIME_VERSION = '3.11'
    PACKAGES = ('snowflake-snowpark-python')
    IMPORTS = ('@INTELLIDOC_PYTHON_STAGE/client_search_proc.py')
    HANDLER = 'client_search_proc.run'
    EXECUTE AS CALLER;

-- Test (connect as reader account first):
-- CALL CLIENT_SEARCH('show me the Q1 billing invoice for Apex Pension Fund');
"""


# ── Local unit test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    fake_candidates = [
        {"FILE_ID": "aaa-111", "CHUNK_TEXT": "Q1 2024 Custody Billing Invoice INV-2024-GB-0892. Total Due $5,729.30 Apex Pension Fund LLC."},
        {"FILE_ID": "bbb-222", "CHUNK_TEXT": "Portfolio Valuation Statement Total NAV $6,333,460. Apple Inc. $1,800,540."},
        {"FILE_ID": "ccc-333", "CHUNK_TEXT": "Annual Custody Tax Summary 2023. Dividend income $21,872. Capital gains $7,100."},
    ]

    prompt = _rerank_prompt("show me the Q1 invoice", fake_candidates)
    print("Prompt snippet:", prompt[:300])

    good = '{"best_candidate_index":1,"confidence":"high","reason":"Q1 2024 billing invoice directly matches the query."}'
    result = _parse_rerank(good, fake_candidates)
    assert result["file_id"] == "aaa-111"
    assert result["confidence"] == "high"

    result2 = _parse_rerank("not json {{", fake_candidates)
    assert result2["file_id"] == "aaa-111"
    assert result2["confidence"] == "low"

    print("All local tests PASSED.")
