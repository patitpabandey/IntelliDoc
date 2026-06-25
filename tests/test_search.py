"""
test_search.py
Tests for the CLIENT_SEARCH RAG proc helpers and the end-to-end
search result shape.  Uses mocked Snowpark sessions — no live
Snowflake connection required.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline" / "search"))

from client_search_proc import _rerank_prompt, _parse_rerank, run


# ── Fixtures ───────────────────────────────────────────────────────────────────

FAKE_CANDIDATES = [
    {"FILE_ID": "file-aaa", "CHUNK_TEXT": "Custody Services Billing Invoice INV-2024-GB-0892. Apex Pension Fund LLC GB-CUST-00421. Q1 2024. Total Due USD $5,729.30.", "SCORE": 0.92},
    {"FILE_ID": "file-bbb", "CHUNK_TEXT": "Portfolio Valuation Statement VAL-2024-GB-0331. Total NAV $6,333,460.00. Apple Inc. 037833100 $1,800,540.00.", "SCORE": 0.81},
    {"FILE_ID": "file-ccc", "CHUNK_TEXT": "Annual Custody Tax Summary Tax Year 2023. 1099-DIV total dividends $21,872.00. Net Capital Gain $7,100.00.", "SCORE": 0.77},
    {"FILE_ID": "file-ddd", "CHUNK_TEXT": "Withholding Tax Reclaim Application RECLAIM-2024-0042. Total Reclaimable $1,534.50. Nestle SA ADR Switzerland 35% WHT.", "SCORE": 0.65},
    {"FILE_ID": "file-eee", "CHUNK_TEXT": "Custody Billing Statement Corporate Actions CA-BILL-2024-0331. Apple Inc. Cash Dividend $125.00. Total Billed $1,600.00.", "SCORE": 0.60},
]

GOOD_LLM_RESPONSE = json.dumps({
    "best_candidate_index": 1,
    "confidence": "high",
    "reason": "The document is the Q1 2024 custody services billing invoice INV-2024-GB-0892 for Apex Pension Fund LLC matching the user query exactly.",
})

MALFORMED_LLM_RESPONSE = "Sorry, I cannot determine the best document."


# ── _rerank_prompt ─────────────────────────────────────────────────────────────

class TestRerankPrompt:
    def test_contains_query(self):
        prompt = _rerank_prompt("show me the Q1 custody billing invoice", FAKE_CANDIDATES)
        assert "show me the Q1 custody billing invoice" in prompt

    def test_contains_all_candidate_ids(self):
        prompt = _rerank_prompt("custody tax summary 2023", FAKE_CANDIDATES)
        for c in FAKE_CANDIDATES:
            assert c["FILE_ID"] in prompt

    def test_contains_chunk_text_snippets(self):
        prompt = _rerank_prompt("portfolio valuation", FAKE_CANDIDATES)
        assert "Custody Services Billing Invoice" in prompt

    def test_json_schema_instruction_present(self):
        prompt = _rerank_prompt("anything", FAKE_CANDIDATES)
        assert "best_candidate_index" in prompt
        assert "confidence" in prompt
        assert "reason" in prompt

    def test_single_candidate(self):
        prompt = _rerank_prompt("query", [FAKE_CANDIDATES[0]])
        assert "Candidate 1" in prompt


# ── _parse_rerank ──────────────────────────────────────────────────────────────

class TestParseRerank:
    def test_parses_valid_json(self):
        result = _parse_rerank(GOOD_LLM_RESPONSE, FAKE_CANDIDATES)
        assert result["file_id"]    == "file-aaa"
        assert result["confidence"] == "high"
        assert "invoice" in result["reason"].lower()

    def test_falls_back_on_bad_json(self):
        result = _parse_rerank(MALFORMED_LLM_RESPONSE, FAKE_CANDIDATES)
        assert result["file_id"]    == "file-aaa"   # index 0 fallback
        assert result["confidence"] == "low"

    def test_clamps_out_of_range_index(self):
        llm = json.dumps({"best_candidate_index": 999, "confidence": "high", "reason": "test"})
        result = _parse_rerank(llm, FAKE_CANDIDATES)
        assert result["file_id"] in [c["FILE_ID"] for c in FAKE_CANDIDATES]

    def test_reason_truncated_to_500_chars(self):
        long_reason = "A" * 1000
        llm = json.dumps({"best_candidate_index": 1, "confidence": "medium", "reason": long_reason})
        result = _parse_rerank(llm, FAKE_CANDIDATES)
        assert len(result["reason"]) <= 500

    def test_strips_markdown_fences(self):
        llm = "```json\n" + GOOD_LLM_RESPONSE + "\n```"
        result = _parse_rerank(llm, FAKE_CANDIDATES)
        assert result["file_id"] == "file-aaa"

    def test_picks_correct_index(self):
        for i, candidate in enumerate(FAKE_CANDIDATES):
            llm = json.dumps({"best_candidate_index": i + 1, "confidence": "high", "reason": "test"})
            result = _parse_rerank(llm, FAKE_CANDIDATES)
            assert result["file_id"] == candidate["FILE_ID"]


# ── run() — Snowpark integration (mocked session) ─────────────────────────────

def _make_mock_session(candidates=None, meta=None, llm_response=None):
    """Build a minimal mock Snowpark Session that returns preset data."""
    candidates = candidates or FAKE_CANDIDATES
    meta = meta or {
        "FILE_ID": "file-aaa",
        "FILE_NAME": "apex_reader_billing_invoice_INV-2024-GB-0892.pdf",
        "FILE_FORMAT": "PDF",
        "DOCUMENT_TYPE": "CUSTODY_BILLING_INVOICE",
        "CLIENT_ACCOUNT_ID": "APEX_READER",
        "CURRENT_LOCATION": "@APEX_STAGE/apex_reader_billing_invoice_INV-2024-GB-0892.pdf",
        "CLASSIFICATION_CONFIDENCE": "high",
        "DOC_SUMMARY": "Q1 2024 custody services billing invoice INV-2024-GB-0892 issued to Apex Pension Fund LLC.",
    }
    llm_response = llm_response or GOOD_LLM_RESPONSE

    session = MagicMock()

    # Build mock Row objects
    def _row(d):
        r = MagicMock()
        r.as_dict.return_value = d
        r.__getitem__ = lambda s, k: d[k]
        return r

    embed_df = MagicMock()
    embed_df.collect.return_value = [_row({"Q_EMBEDDING": "[0.1, 0.2]"})]
    embed_df.__getitem__ = lambda s, k: "[0.1, 0.2]"

    chunk_rows = [_row(c) for c in candidates]
    chunk_df   = MagicMock()
    chunk_df.collect.return_value = chunk_rows

    llm_row = MagicMock()
    llm_row.__getitem__ = lambda s, k: llm_response
    llm_df  = MagicMock()
    llm_df.collect.return_value = [llm_row]

    meta_row = _row(meta)
    meta_df  = MagicMock()
    meta_df.collect.return_value = [meta_row]

    audit_df = MagicMock()
    audit_df.collect.return_value = []

    # Mock CURRENT_ACCOUNT() response
    acct_df = MagicMock()
    acct_df.collect.return_value = [_row({"CA": "APEX_READER"})]

    # Dispatch SQL calls by content
    def sql_dispatch(sql_text, params=None):
        sql_text = sql_text.strip().upper()
        if "CURRENT_ACCOUNT" in sql_text:
            return acct_df
        if "EMBED_TEXT_1024" in sql_text and "DOCUMENT_CHUNKS" not in sql_text:
            return embed_df
        if "DOCUMENT_CHUNKS" in sql_text:
            return chunk_df
        if "COMPLETE" in sql_text:
            return llm_df
        if "DOCUMENT_REGISTRY" in sql_text and "DOCUMENT_CLASSIFICATION" in sql_text:
            return meta_df
        return audit_df

    session.sql.side_effect = sql_dispatch
    return session, meta


class TestRunProc:
    def test_happy_path_returns_correct_file(self):
        session, meta = _make_mock_session()
        result = run(session, "show me the Q1 custody billing invoice for Apex")
        assert result["file_id"]       == "file-aaa"
        assert result["document_type"] == "CUSTODY_BILLING_INVOICE"
        assert result["confidence"]    == "high"

    def test_returns_top5_scores(self):
        session, _ = _make_mock_session()
        result = run(session, "find an invoice")
        assert "top_scores" in result
        assert len(result["top_scores"]) == len(FAKE_CANDIDATES)

    def test_no_documents_returns_error(self):
        session, _ = _make_mock_session(candidates=[])
        chunk_df = MagicMock()
        chunk_df.collect.return_value = []
        def sql_dispatch(sql_text, params=None):
            if "DOCUMENT_CHUNKS" in sql_text.upper():
                return chunk_df
            df = MagicMock()
            df.collect.return_value = [MagicMock()]
            return df
        session.sql.side_effect = sql_dispatch
        result = run(session, "find anything")
        assert "error" in result

    def test_llm_rerank_failure_falls_back_to_top_vector(self):
        session, _ = _make_mock_session(llm_response="not valid json {{{")
        result = run(session, "custody tax summary 2023")
        # Should still return a result (fallback)
        assert result["file_id"] is not None
        assert result["confidence"] == "low"

    def test_client_isolation_query_includes_client_account_id(self):
        session, _ = _make_mock_session()
        run(session, "find the Q1 billing invoice")
        # The DOCUMENT_CHUNKS SQL call must include CLIENT_ACCOUNT_ID
        chunk_calls = [
            c for c in session.sql.call_args_list
            if "DOCUMENT_CHUNKS" in str(c)
        ]
        assert any("CLIENT_ACCOUNT_ID" in str(c) for c in chunk_calls), \
            "CLIENT_ACCOUNT_ID not found in vector search query — isolation broken!"

    def test_execution_time_recorded(self):
        session, _ = _make_mock_session()
        result = run(session, "balance sheet 2023")
        assert "execution_ms" in result
        assert isinstance(result["execution_ms"], int)
        assert result["execution_ms"] >= 0


# ── End-to-end shape test ──────────────────────────────────────────────────────

class TestResultShape:
    REQUIRED_KEYS = [
        "file_id", "file_name", "file_format", "document_type",
        "confidence", "reason", "stage_path", "result_count",
        "top_scores", "execution_ms",
    ]

    def test_all_required_keys_present(self):
        session, _ = _make_mock_session()
        result = run(session, "show me Q1 invoice")
        for key in self.REQUIRED_KEYS:
            assert key in result, f"Missing key: '{key}' in result"

    def test_confidence_valid_value(self):
        session, _ = _make_mock_session()
        result = run(session, "anything")
        assert result["confidence"] in ("high", "medium", "low")
