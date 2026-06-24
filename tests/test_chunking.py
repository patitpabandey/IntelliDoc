"""
test_chunking.py
Unit tests for text extraction and chunking.
Domain: Global Bank N.A. Custody & Securities Services (PDF, XLSX, CSV).
No Snowflake or AWS connections needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline" / "processing"))

from extract_text import (
    extract_pdf_text, extract_xlsx_text, extract_csv_text,
    extract_text, clean_text,
)

# ── Python mirror of chunk_and_embed.sql splitIntoChunks ─────────────────────

TARGET_CHARS  = 500 * 4   # 2000
OVERLAP_CHARS = 50  * 4   # 200


def split_into_chunks(text: str) -> list[str]:
    import re
    paragraphs = re.split(r"\n{2,}", text)
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 1 <= TARGET_CHARS:
            current = (current + "\n\n" + para).lstrip("\n")
        else:
            if current:
                chunks.append(current)
                current = current[-OVERLAP_CHARS:] + "\n\n" + para
            else:
                import re as _re
                sentences = _re.split(r"(?<=[.!?])\s+", para)
                for sent in sentences:
                    sent = sent.strip()
                    if len(current) + len(sent) + 1 <= TARGET_CHARS:
                        current = (current + " " + sent).strip()
                    else:
                        if current:
                            chunks.append(current)
                        current = (current[-OVERLAP_CHARS:] + " " + sent).strip()
    if current.strip():
        chunks.append(current.strip())
    return chunks


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_pdf_bytes(text: str) -> bytes:
    try:
        import io
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph
        from reportlab.lib.styles import getSampleStyleSheet
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter)
        styles = getSampleStyleSheet()
        doc.build([Paragraph(line, styles["Normal"]) for line in text.split("\n") if line.strip()])
        return buf.getvalue()
    except ImportError:
        pytest.skip("reportlab not installed")


def make_xlsx_bytes(data: dict) -> bytes:
    try:
        import io, openpyxl
        wb = openpyxl.Workbook()
        for i, (sheet, rows) in enumerate(data.items()):
            ws = wb.active if i == 0 else wb.create_sheet(sheet)
            ws.title = sheet
            for row in rows:
                ws.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except ImportError:
        pytest.skip("openpyxl not installed")


def make_csv_bytes(rows: list[dict]) -> bytes:
    import csv, io
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ── CSV extraction tests (new for custody banking) ────────────────────────────

class TestCSVExtraction:
    def test_extracts_column_names(self):
        data = make_csv_bytes([
            {"Txn_ID": "TXN-001", "Account_No": "GB-CUST-00421", "Security_Name": "Apple Inc.", "Txn_Type": "BUY"},
            {"Txn_ID": "TXN-002", "Account_No": "GB-CUST-00421", "Security_Name": "Microsoft Corp.", "Txn_Type": "SELL"},
        ])
        text = extract_csv_text(data)
        assert "Txn_ID" in text
        assert "Account_No" in text
        assert "Security_Name" in text

    def test_extracts_row_values(self):
        data = make_csv_bytes([
            {"Txn_ID": "TXN-001", "Account_No": "GB-CUST-00421", "Security_Name": "Apple Inc."},
        ])
        text = extract_csv_text(data)
        assert "TXN-001" in text
        assert "GB-CUST-00421" in text
        assert "Apple Inc." in text

    def test_transaction_report_pattern(self):
        """Mirrors billing_transactions_Q1_2024.csv from Source Files."""
        data = make_csv_bytes([
            {"Txn_ID": "TXN-001", "Account_No": "GB-CUST-00421", "Trade_Date": "2024-01-08",
             "Settlement_Date": "2024-01-10", "Security_Name": "Apple Inc.", "CUSIP": "037833100",
             "Txn_Type": "BUY", "Quantity": "500", "Price_USD": "185.20",
             "Gross_Value_USD": "92600.00", "Commission_USD": "92.60",
             "Settlement_Fee_USD": "15.00", "Net_Value_USD": "92707.60"},
        ])
        text = extract_csv_text(data)
        assert "CUSIP" in text
        assert "037833100" in text
        assert "Settlement_Date" in text

    def test_surcharge_statement_pattern(self):
        """Mirrors billing_surcharges_Q1_2024.csv from Source Files."""
        data = make_csv_bytes([
            {"Surcharge_ID": "SUR-001", "Account_No": "GB-CUST-00421",
             "Invoice_Ref": "INV-2024-GB-0892", "Surcharge_Type": "Late Settlement",
             "Description": "Settlement delay >2 days", "Applied_Date": "2024-01-22",
             "Surcharge_Rate": "200%", "Surcharge_Amount_USD": "30.00"},
        ])
        text = extract_csv_text(data)
        assert "Late Settlement" in text
        assert "INV-2024-GB-0892" in text

    def test_tax_profile_pattern(self):
        """Mirrors tax_profile_GB-CUST-00421.csv from Source Files."""
        data = make_csv_bytes([
            {"Field": "Client_Name",    "Value": "Apex Pension Fund LLC"},
            {"Field": "W8_W9_Form_On_File", "Value": "W-9"},
            {"Field": "FATCA_Status",   "Value": "Participating FFI"},
            {"Field": "Default_WHT_Rate_Dividends", "Value": "0%"},
        ])
        text = extract_csv_text(data)
        assert "Apex Pension Fund LLC" in text
        assert "FATCA_Status" in text
        assert "0%" in text

    def test_income_report_pattern(self):
        """Mirrors billing_corporate_actions_income_Q1_2024.csv."""
        data = make_csv_bytes([
            {"Income_ID": "INC-001", "Account_No": "GB-CUST-00421",
             "Security_Name": "Apple Inc.", "CUSIP": "037833100",
             "Income_Type": "Cash Dividend", "Ex_Date": "2024-02-13",
             "Gross_Amount_USD": "5200.00", "Tax_Withheld_USD": "780.00",
             "Net_Amount_USD": "4420.00"},
        ])
        text = extract_csv_text(data)
        assert "Cash Dividend" in text
        assert "4420.00" in text

    def test_utf8_bom_handled(self):
        bom_csv = b"\xef\xbb\xbfField,Value\nClient_Name,Apex Pension Fund LLC\n"
        text = extract_csv_text(bom_csv)
        assert "Apex Pension Fund LLC" in text
        assert "Field" in text  # BOM stripped, header intact

    def test_empty_csv_returns_header_only(self):
        data = b"Txn_ID,Account_No,Security_Name\n"
        text = extract_csv_text(data)
        assert "Txn_ID" in text

    def test_dispatch_csv_format(self):
        data = make_csv_bytes([{"Col": "val"}])
        text = extract_text(data, "CSV")
        assert "Col" in text

    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            extract_text(b"data", "JSON")


# ── PDF extraction tests ───────────────────────────────────────────────────────

class TestPDFExtraction:
    def test_custody_billing_invoice_pdf(self):
        """Simulate the content of billing_invoice_INV-2024-GB-0892.pdf."""
        content = (
            "Global Bank N.A. Custody Services Billing Invoice\n"
            "Invoice No. INV-2024-GB-0892\n"
            "Bill To: Apex Pension Fund LLC\n"
            "Account No. GB-CUST-00421\n"
            "Safekeeping — Equity Securities  $1,500.00\n"
            "Total Due (USD) $5,729.30\n"
        )
        pdf_bytes = make_pdf_bytes(content)
        text = clean_text(extract_pdf_text(pdf_bytes))
        assert "INV-2024-GB-0892" in text or "Apex" in text or "Custody" in text

    def test_portfolio_valuation_pdf(self):
        content = (
            "Portfolio Valuation Statement\n"
            "Global Bank N.A. Custody & Securities Services\n"
            "Apple Inc. 037833100 10,500 $171.48 $1,800,540.00\n"
            "Total NAV $6,333,460.00\n"
        )
        pdf_bytes = make_pdf_bytes(content)
        text = clean_text(extract_pdf_text(pdf_bytes))
        assert len(text) > 20

    def test_clean_text_normalises_whitespace(self):
        raw = "  Hello   World  \n\n\n\n  Next paragraph  "
        assert "Hello World" in clean_text(raw)
        assert "\n\n\n" not in clean_text(raw)

    def test_clean_text_preserves_paragraph_breaks(self):
        raw = "Para one.\n\nPara two."
        assert "\n\n" in clean_text(raw)


# ── XLSX extraction tests ──────────────────────────────────────────────────────

class TestXLSXExtraction:
    def test_multiple_sheets(self):
        xlsx = make_xlsx_bytes({
            "Equities":    [["Security","CUSIP","Market Value"], ["Apple Inc.","037833100","1800540"]],
            "Fixed Income":[["Security","Face Value"], ["US Treasury 4.5% 2026","100000"]],
        })
        text = extract_xlsx_text(xlsx)
        assert "Equities" in text
        assert "Fixed Income" in text
        assert "037833100" in text

    def test_dispatch_xlsx_format(self):
        xlsx = make_xlsx_bytes({"Sheet1": [["H1","H2"],[1,2]]})
        text = extract_text(xlsx, "XLSX")
        assert "H1" in text


# ── Chunking tests ─────────────────────────────────────────────────────────────

class TestChunking:
    def test_short_text_single_chunk(self):
        text = "Custody billing invoice Q1 2024.\n\nClient: Apex Pension Fund LLC."
        assert len(split_into_chunks(text)) == 1

    def test_long_text_multiple_chunks(self):
        para  = "A " * 300 + "."
        text  = "\n\n".join([para] * 10)
        assert len(split_into_chunks(text)) >= 3

    def test_chunks_cover_content(self):
        entries = [f"CUSIP-{i:06d}" for i in range(100)]
        text = "\n\n".join(entries)
        combined = " ".join(split_into_chunks(text))
        for e in entries[::10]:
            assert e in combined

    def test_chunk_size_bounded(self):
        para  = "B " * 300 + "."
        text  = "\n\n".join([para] * 15)
        for chunk in split_into_chunks(text):
            assert len(chunk) <= TARGET_CHARS * 1.5

    def test_empty_returns_no_chunks(self):
        assert split_into_chunks("") == []

    def test_sentence_split_for_giant_paragraph(self):
        sentences = [f"Transaction {i} executed at market price with CUSIP 037833100." for i in range(200)]
        text = " ".join(sentences)
        assert len(split_into_chunks(text)) >= 2


# ── Real source file tests ─────────────────────────────────────────────────────

class TestSourceFiles:
    SOURCE = Path(__file__).parent.parent / "Source Files"

    @pytest.fixture(autouse=True)
    def require_source(self):
        if not self.SOURCE.exists():
            pytest.skip("Source Files/ directory not found")

    def test_real_pdf_files_extract_non_empty(self):
        pdfs = list(self.SOURCE.glob("*.pdf"))
        assert pdfs, "No PDF files in Source Files/"
        for pdf in pdfs:
            text = clean_text(extract_pdf_text(pdf.read_bytes()))
            assert len(text) > 50, f"{pdf.name} yielded near-empty text"

    def test_real_csv_files_extract_non_empty(self):
        csvs = list(self.SOURCE.glob("*.csv"))
        assert csvs, "No CSV files in Source Files/"
        for csv_path in csvs:
            text = clean_text(extract_csv_text(csv_path.read_bytes()))
            assert len(text) > 10, f"{csv_path.name} yielded near-empty text"

    def test_billing_invoice_pdf_contains_key_terms(self):
        inv = self.SOURCE / "billing_invoice_INV-2024-GB-0892.pdf"
        if not inv.exists():
            pytest.skip("billing_invoice not found")
        text = clean_text(extract_pdf_text(inv.read_bytes()))
        # Should contain custody billing terms
        assert any(t in text for t in ["Invoice", "Custody", "Global Bank", "Apex", "GB-CUST"]), \
            f"Expected custody billing terms not found. Got: {text[:300]}"

    def test_transaction_csv_has_cusip_column(self):
        txn = self.SOURCE / "billing_transactions_Q1_2024.csv"
        if not txn.exists():
            pytest.skip("billing_transactions CSV not found")
        text = extract_csv_text(txn.read_bytes())
        assert "CUSIP" in text

    def test_tax_profile_csv_has_fatca(self):
        tp = self.SOURCE / "tax_profile_GB-CUST-00421.csv"
        if not tp.exists():
            pytest.skip("tax_profile CSV not found")
        text = extract_csv_text(tp.read_bytes())
        assert "FATCA" in text

    def test_real_csv_produces_chunkable_text(self):
        for csv_path in self.SOURCE.glob("*.csv"):
            text  = clean_text(extract_csv_text(csv_path.read_bytes()))
            chunks = split_into_chunks(text)
            assert len(chunks) >= 1, f"{csv_path.name} produced no chunks"
