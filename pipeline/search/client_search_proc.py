"""
client_search_proc.py
Snowpark Python stored procedure: CLIENT_SEARCH(query VARCHAR, client_id VARCHAR)

RAG search pipeline (§7.3 of spec):
  a. Embed the query using EMBED_TEXT_1024('e5-base-v2', query)
  b. VECTOR_COSINE_SIMILARITY retrieval — top-5 chunks filtered by CLIENT_ID
  c. LLM re-rank: send top-5 chunks + query to COMPLETE → best FILE_ID
  d. Fetch metadata for winning FILE_ID
  e. Write SEARCH_AUDIT row
  f. Return JSON result
  Fallback: if COMPLETE fails, return the top vector hit directly.

Deploy via:
    CREATE OR REPLACE PROCEDURE CLIENT_SEARCH(QUERY VARCHAR, CLIENT_ID VARCHAR)
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
from typing import Optional


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _rerank_prompt(query: str, candidates: list[dict]) -> str:
    chunks_text = "\n\n".join(
        f"[Candidate {i+1}] FILE_ID={c['FILE_ID']}\n{c['CHUNK_TEXT'][:800]}"
        for i, c in enumerate(candidates)
    )
    return f"""You are a document retrieval assistant for a tax and billing platform.

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
        idx   = int(obj.get("best_candidate_index", 1)) - 1
        idx   = max(0, min(idx, len(candidates) - 1))
        return {
            "file_id":    candidates[idx]["FILE_ID"],
            "confidence": obj.get("confidence", "medium"),
            "reason":     str(obj.get("reason", ""))[:500],
        }
    except Exception:
        # Fallback: best vector hit
        return {
            "file_id":    candidates[0]["FILE_ID"],
            "confidence": "low",
            "reason":     "LLM rerank parse failed — returning top vector result.",
        }


# ── Snowpark entry point ──────────────────────────────────────────────────────

def run(session, query: str, client_id: str) -> dict:
    """
    Main handler for Snowpark stored procedure CLIENT_SEARCH.
    session is injected automatically by Snowpark.
    """
    t0 = time.time()

    # ── a. Embed the query ──────────────────────────────────────────────────────
    embed_row = session.sql(
        "SELECT SNOWFLAKE.CORTEX.EMBED_TEXT_1024('e5-base-v2', ?) AS Q_EMBEDDING",
        params=[query]
    ).collect()
    query_vec = embed_row[0]["Q_EMBEDDING"]   # VECTOR(FLOAT, 1024) object

    # ── b. Vector similarity retrieval (top-5, client-scoped) ──────────────────
    #
    # Snowflake VECTOR_COSINE_SIMILARITY works inline as a column expression;
    # we pass the already-computed embedding literal as a bind.
    #
    # NOTE: Snowpark Python doesn't support binding VECTOR literals directly;
    # we serialise to the TO_VECTOR string representation.
    vec_str = str(query_vec)   # '[0.123, -0.456, ...]'

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
        WHERE dc.CLIENT_ID       = ?
          AND dr.PROCESSING_STATUS = 'AVAILABLE'
        ORDER BY SCORE DESC
        LIMIT 5
    """, params=[client_id]).collect()

    if not top_chunks:
        return {
            "error":   "No documents found for this client.",
            "file_id": None,
            "query":   query,
        }

    candidates = [dict(row.as_dict()) for row in top_chunks]

    # ── c. LLM re-rank ─────────────────────────────────────────────────────────
    rerank_result: dict
    try:
        prompt        = _rerank_prompt(query, candidates).replace("'", "\\'")
        llm_row       = session.sql(f"""
            SELECT SNOWFLAKE.CORTEX.COMPLETE(
                'mistral-large',
                [{{ 'role': 'user', 'content': '{prompt}' }}],
                {{ 'temperature': 0, 'max_tokens': 300 }}
            ):choices[0]:messages::VARCHAR AS response
        """).collect()
        llm_output    = llm_row[0]["RESPONSE"] if llm_row else ""
        rerank_result = _parse_rerank(llm_output, candidates)
    except Exception as llm_err:
        # Graceful fallback: top vector hit
        rerank_result = {
            "file_id":    candidates[0]["FILE_ID"],
            "confidence": "low",
            "reason":     f"LLM rerank unavailable ({llm_err}); returning top vector result.",
        }

    best_file_id = rerank_result["file_id"]

    # ── d. Fetch metadata for the winning file ──────────────────────────────────
    meta_rows = session.sql("""
        SELECT
            dr.FILE_ID, dr.FILE_NAME, dr.FILE_FORMAT,
            dr.DOCUMENT_TYPE, dr.CLIENT_ID, dr.CURRENT_LOCATION,
            dc.CLASSIFICATION_CONFIDENCE, dc.DOC_SUMMARY
        FROM DOCUMENT_REGISTRY dr
        LEFT JOIN DOCUMENT_CLASSIFICATION dc ON dr.FILE_ID = dc.FILE_ID
        WHERE dr.FILE_ID = ?
    """, params=[best_file_id]).collect()

    meta = dict(meta_rows[0].as_dict()) if meta_rows else {}

    exec_ms = int((time.time() - t0) * 1000)

    # ── e. Write SEARCH_AUDIT ───────────────────────────────────────────────────
    try:
        safe_term = query.replace("'", "''")[:2000]
        session.sql(f"""
            INSERT INTO SEARCH_AUDIT
                (CLIENT_ID, SEARCH_TERM, RESULT_FILE_ID, SEARCH_CONFIDENCE,
                 RESULT_COUNT, EXECUTION_TIME_MS)
            VALUES (
                ?, ?, ?, ?, ?, ?
            )
        """, params=[
            client_id, query, best_file_id,
            rerank_result["confidence"], len(candidates), exec_ms
        ]).collect()
    except Exception:
        pass  # audit failure must not break the search response

    # ── f. Return result ────────────────────────────────────────────────────────
    return {
        "file_id":       best_file_id,
        "file_name":     meta.get("FILE_NAME"),
        "file_format":   meta.get("FILE_FORMAT"),
        "document_type": meta.get("DOCUMENT_TYPE") or rerank_result.get("document_type"),
        "confidence":    rerank_result["confidence"],
        "reason":        rerank_result["reason"],
        "summary":       meta.get("DOC_SUMMARY"),
        "stage_path":    meta.get("CURRENT_LOCATION"),
        "result_count":  len(candidates),
        "top_scores":    [round(float(c["SCORE"]), 4) for c in candidates],
        "execution_ms":  exec_ms,
    }


# ── Deployment DDL ─────────────────────────────────────────────────────────────
DEPLOY_SQL = """
-- Upload this file first:
--   PUT file://pipeline/search/client_search_proc.py @INTELLIDOC_PYTHON_STAGE auto_compress=false;

CREATE OR REPLACE PROCEDURE CLIENT_SEARCH(QUERY VARCHAR, CLIENT_ID VARCHAR)
    RETURNS VARIANT
    LANGUAGE PYTHON
    RUNTIME_VERSION = '3.11'
    PACKAGES = ('snowflake-snowpark-python')
    IMPORTS = ('@INTELLIDOC_PYTHON_STAGE/client_search_proc.py')
    HANDLER = 'client_search_proc.run'
    EXECUTE AS CALLER;

-- Test:
-- CALL CLIENT_SEARCH('show me the Q1 invoice for Acme', 'CLIENT_ACME');
"""


# ── Local unit test (no Snowflake connection needed) ──────────────────────────
if __name__ == "__main__":
    # Test the pure-Python helpers only
    fake_candidates = [
        {"FILE_ID": "aaa-111", "CHUNK_TEXT": "Q1 2024 Invoice for Acme Corp. Total due: $12,500."},
        {"FILE_ID": "bbb-222", "CHUNK_TEXT": "Annual Tax Return 2023. Federal income tax withheld."},
        {"FILE_ID": "ccc-333", "CHUNK_TEXT": "W-2 Wage and Tax Statement. Wages: $85,000."},
    ]
    prompt = _rerank_prompt("show me the Q1 invoice", fake_candidates)
    print("Prompt snippet:", prompt[:300])

    # Simulate a well-formed LLM response
    fake_llm = '{"best_candidate_index": 1, "confidence": "high", "reason": "Contains Q1 invoice details for Acme Corp."}'
    result = _parse_rerank(fake_llm, fake_candidates)
    print("Rerank result:", result)
    assert result["file_id"] == "aaa-111"
    assert result["confidence"] == "high"

    # Simulate malformed LLM response → fallback to top hit
    result2 = _parse_rerank("not json at all", fake_candidates)
    assert result2["file_id"] == "aaa-111"  # fallback = index 0
    print("Fallback result:", result2)
    print("All local tests PASSED.")
