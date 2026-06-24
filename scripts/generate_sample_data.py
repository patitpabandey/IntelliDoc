"""
generate_sample_data.py
Generates realistic custody banking sample documents that mirror the real
Global Bank N.A. source files in IntelliDoc/Source Files/.

Document types produced per client:
  PDF: CUSTODY_BILLING_INVOICE, PORTFOLIO_VALUATION, CUSTODY_TAX_SUMMARY,
       TAX_RECLAIM_APPLICATION, CORPORATE_ACTIONS_BILLING
  CSV: TRANSACTION_REPORT, SURCHARGE_STATEMENT, INCOME_REPORT, TAX_PROFILE

Run:
    python scripts/generate_sample_data.py

Outputs to ../samples/   (relative to this script)
"""

import csv
import io
import json
import random
from datetime import date, timedelta
from pathlib import Path

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors
    REPORTLAB_OK = True
except ImportError:
    REPORTLAB_OK = False
    print("WARNING: reportlab not installed — PDFs will be plain-text stubs")

SAMPLES_DIR = Path(__file__).parent.parent / "samples"
SAMPLES_DIR.mkdir(exist_ok=True)

# ── Client definitions (matching CLIENT_MAPPING in 02_tables.sql) ────────────
CLIENTS = [
    {
        "client_id":  "CLIENT_APEX",
        "name":       "Apex Pension Fund LLC",
        "account":    "GB-CUST-00421",
        "branch":     "BRANCH-GLOBALBANK",
        "address":    "200 Park Ave, New York, NY 10166",
        "client_type": "Institutional – Qualified Pension Fund",
        "tin":        "XX-XXXXXXX",
        "w_form":     "W-9",
        "fatca":      "Participating FFI",
    },
    {
        "client_id":  "CLIENT_MERIDIAN",
        "name":       "Meridian Asset Management",
        "account":    "GB-CUST-00532",
        "branch":     "BRANCH-GLOBALBANK",
        "address":    "1 State Street Plaza, New York, NY 10004",
        "client_type": "Institutional – Investment Manager",
        "tin":        "YY-YYYYYYY",
        "w_form":     "W-9",
        "fatca":      "Participating FFI",
    },
    {
        "client_id":  "CLIENT_SUMMIT",
        "name":       "Summit Endowment Fund",
        "account":    "GB-CUST-00615",
        "branch":     "BRANCH-GLOBALBANK",
        "address":    "500 Boylston Street, Boston, MA 02116",
        "client_type": "Institutional – Endowment",
        "tin":        "ZZ-ZZZZZZZ",
        "w_form":     "W-9",
        "fatca":      "Participating FFI",
    },
]

# ── Securities universe ───────────────────────────────────────────────────────
EQUITIES = [
    ("Apple Inc.",          "037833100", 171.48),
    ("Microsoft Corp.",     "594918104", 420.55),
    ("Exxon Mobil Corp.",   "30231G102", 112.15),
    ("Johnson & Johnson",   "478160104", 155.20),
    ("Berkshire Hathaway",  "084670702", 408.20),
    ("Amazon.com Inc.",     "023135106", 178.90),
    ("Alphabet Inc.",       "02079K305", 175.35),
    ("JPMorgan Chase",      "46625H100", 195.50),
]
FIXED_INCOME = [
    ("US Treasury 4.5% 2026",  "912828YK0"),
    ("Corp Bond GBK 5% 2027",  "38141GXZ2"),
    ("Muni Bond NY 3.8% 2028", "64966EAH5"),
]
FOREIGN_SECURITIES = [
    ("Nestle SA ADR",        "Switzerland", 35),
    ("LVMH Moet ADR",        "France",      30),
    ("SAP SE ADR",           "Germany",     26),
    ("Royal Dutch Shell ADR","Netherlands", 15),
    ("Toyota Motor ADR",     "Japan",       20),
]
CORP_ACTION_TYPES = ["Cash Dividend", "Stock Split", "Rights Issue", "Tender Offer", "Special Dividend"]
SURCHARGE_TYPES = [
    ("Late Settlement",      "Settlement delay >2 days",          "200%",  15.00),
    ("Currency Conversion",  "USD/EUR conversion fee on proceeds", "0.25%", 0.00),
    ("Custody Minimum",      "Below minimum asset threshold",      "flat",  150.00),
    ("Ad Hoc Reporting",     "Custom performance report requested","flat",  75.00),
    ("Regulatory Filing",    "SEC Form N-PX filing assistance",    "flat",  58.25),
]

BANK_NAME    = "Global Bank N.A."
BANK_ADDR    = "100 Financial Plaza, New York, NY 10005"
BANK_TEL     = "+1 (212) 555-0100 | globalbank.com"
BANK_ABA     = "021000021"
BANK_ACCT    = "987654321"

def rnd(lo, hi): return round(random.uniform(lo, hi), 2)

# ── PDF helpers ───────────────────────────────────────────────────────────────

def _pdf_doc(path, title):
    doc = SimpleDocTemplate(str(path), pagesize=letter,
                            topMargin=0.75*inch, bottomMargin=0.75*inch,
                            leftMargin=inch, rightMargin=inch)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("BankHeader", parent=styles["Normal"], fontSize=8))
    return doc, styles

HEADER_COLOR = colors.HexColor("#1F3864")
ALT_COLOR    = colors.HexColor("#EAF2FB")

def _tbl(data, col_widths=None):
    style = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), HEADER_COLOR),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 9),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, ALT_COLOR]),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.grey),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
    ])
    return Table(data, colWidths=col_widths, style=style)

def _bank_header(styles):
    return [
        Paragraph(f"<b>{BANK_NAME}</b>", styles["Normal"]),
        Paragraph("Custody & Securities Services", styles["BankHeader"]),
        Paragraph(BANK_ADDR, styles["BankHeader"]),
        Paragraph(f"Tel: {BANK_TEL}", styles["BankHeader"]),
        Spacer(1, 0.15*inch),
    ]

# ══════════════════════════════════════════════════════════════════════════════
# PDF generators
# ══════════════════════════════════════════════════════════════════════════════

def generate_custody_billing_invoice(client, year, quarter):
    inv_num   = f"INV-{year}-GB-{random.randint(1000,9999)}"
    q_end     = date(year, quarter * 3, 28)
    filename  = f"{client['client_id'].lower()}_billing_invoice_{inv_num}.pdf"
    path      = SAMPLES_DIR / filename

    if not REPORTLAB_OK:
        path.write_text(
            f"Global Bank N.A.\nCustody Services Billing Invoice\nDocument ID: {inv_num}\n"
            f"Period: Q{quarter} {year}\nBill To: {client['name']}\n"
            f"Account No.: {client['account']}\n"
        )
        return path

    doc, styles = _pdf_doc(path, "Custody Services Billing Invoice")
    items = [
        ("Safekeeping — Equity Securities",  random.randint(8,16),  125.00),
        ("Safekeeping — Fixed Income",        random.randint(4,10),  100.00),
        ("Settlement — DvP Transactions",     random.randint(30,70), 15.00),
        ("Corporate Action Processing",       random.randint(3,8),   200.00),
        ("Income Collection & Processing",    random.randint(10,25), 25.00),
        ("Tax Reclaim Filing",                random.randint(1,5),   150.00),
        ("Reporting & Account Maintenance",   1,                     500.00),
    ]
    line_amounts = [qty * price for _, qty, price in items]
    subtotal = sum(line_amounts)
    surchg   = round(rnd(200, 450), 2)
    total    = round(subtotal + surchg, 2)

    story = _bank_header(styles) + [
        Paragraph(f"Document Type: Custody Services – Billing Invoice  |  Document ID: {inv_num}  |  Period: Q{quarter} {year}  |  Generated: {q_end}", styles["BankHeader"]),
        Spacer(1, 0.15*inch),
        Paragraph("<b>Custody Services Billing Invoice</b>", styles["h2"]),
        Spacer(1, 0.1*inch),
        _tbl([["Field","Detail"],
              ["Invoice No.", inv_num], ["Invoice Date", str(q_end)],
              ["Due Date", str(q_end + timedelta(days=30))],
              ["Bill To", f"{client['name']}, {client['address']}"],
              ["Account No.", client['account']],
              ["Payment Terms", "Net 30 Days"],
              ["Remit To", f"{BANK_NAME}, ABA: {BANK_ABA}, Acct: {BANK_ACCT}"]],
             col_widths=[2.5*inch, 4*inch]),
        Spacer(1, 0.15*inch),
        Paragraph("<b>Invoice Line Items</b>", styles["h3"]),
        _tbl(
            [["#", "Service Description", "Units", "Unit Price (USD)", "Amount (USD)"]] +
            [[str(i+1), desc, str(qty), f"${price:,.2f}", f"${qty*price:,.2f}"]
             for i, (desc, qty, price) in enumerate(items)] +
            [["","","","Subtotal",    f"${subtotal:,.2f}"],
             ["","","","Surcharges & Misc.", f"${surchg:,.2f}"],
             ["","","","Tax (0%)",    "$0.00"],
             ["","","","<b>Total Due (USD)</b>", f"<b>${total:,.2f}</b>"]],
            col_widths=[0.3*inch, 3.2*inch, 0.5*inch, 1.2*inch, 1.3*inch]
        ),
        Spacer(1, 0.15*inch),
        Paragraph(f"<b>Payment Instructions</b>", styles["h3"]),
        Paragraph(f"Please remit payment to {BANK_NAME} via wire transfer referencing invoice number {inv_num}. "
                  "Late payments are subject to a 1.5% monthly finance charge. "
                  "For billing enquiries contact: custody.billing@globalbank.com | +1 (212) 555-0200.", styles["Normal"]),
    ]
    doc.build(story)
    return path


def generate_portfolio_valuation(client, year, quarter):
    val_date = date(year, quarter * 3, 31 if quarter in (1,2,3) else 30)
    val_id   = f"VAL-{year}-GB-{random.randint(1000,9999)}"
    filename = f"{client['client_id'].lower()}_portfolio_valuation_{val_id}.pdf"
    path     = SAMPLES_DIR / filename

    if not REPORTLAB_OK:
        path.write_text(
            f"Global Bank N.A.\nPortfolio Valuation Statement\nDocument ID: {val_id}\n"
            f"As of {val_date}\nClient: {client['name']}\nAccount: {client['account']}\n"
        )
        return path

    doc, styles = _pdf_doc(path, "Portfolio Valuation Statement")
    equities = random.sample(EQUITIES, 5)
    eq_rows  = []
    total_eq = 0.0
    for name, cusip, base_price in equities:
        shares = random.randint(1000, 12000)
        price  = round(base_price * random.uniform(0.92, 1.08), 2)
        mv     = round(shares * price, 2)
        total_eq += mv
        eq_rows.append([name, cusip, f"{shares:,}", f"${price:.2f}", f"${mv:,.2f}", ""])

    fi_rows = []
    total_fi = 0.0
    for name, cusip in FIXED_INCOME:
        face  = random.choice([100000, 150000, 200000])
        price = round(random.uniform(97, 101), 2)
        mv    = round(face * price / 100, 2)
        total_fi += mv
        fi_rows.append([name, cusip, f"${face:,}", f"{price:.2f}", f"${mv:,.2f}", ""])

    total_nav = round(total_eq + total_fi, 2)

    story = _bank_header(styles) + [
        Paragraph(f"Document Type: Custody Services – Portfolio Valuation Statement  |  Document ID: {val_id}  |  Period: As of {val_date}", styles["BankHeader"]),
        Spacer(1, 0.15*inch),
        Paragraph("<b>Portfolio Valuation Statement</b>", styles["h2"]),
        Paragraph(f"This statement provides the fair market valuation of assets held in custody by {BANK_NAME} on behalf of "
                  f"{client['name']} as of the close of business on {val_date}. Valuations are based on closing prices "
                  "sourced from primary exchanges and evaluated in base currency USD.", styles["Normal"]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Equity Holdings</b>", styles["h3"]),
        _tbl([["Security","CUSIP","Shares","Price (USD)","Market Value (USD)","% of Portfolio"]] +
             [[r[0],r[1],r[2],r[3],r[4], f"{round(float(r[4].replace('$','').replace(',',''))/total_nav*100,1)}%"] for r in eq_rows],
             col_widths=[2.0*inch,1.0*inch,0.7*inch,0.8*inch,1.3*inch,0.7*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Fixed Income Holdings</b>", styles["h3"]),
        _tbl([["Security","CUSIP","Face Value (USD)","Price","Market Value (USD)","% of Portfolio"]] +
             [[r[0],r[1],r[2],r[3],r[4], f"{round(float(r[4].replace('$','').replace(',',''))/total_nav*100,1)}%"] for r in fi_rows],
             col_widths=[2.0*inch,1.0*inch,1.0*inch,0.6*inch,1.3*inch,0.6*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Portfolio Summary</b>", styles["h3"]),
        _tbl([["Asset Class","Market Value (USD)","% of Portfolio"],
              ["Equities",     f"${total_eq:,.2f}",  f"{round(total_eq/total_nav*100,1)}%"],
              ["Fixed Income", f"${total_fi:,.2f}",  f"{round(total_fi/total_nav*100,1)}%"],
              ["Cash & Equiv.","$0.00",              "0.0%"],
              ["<b>Total NAV</b>", f"<b>${total_nav:,.2f}</b>", "<b>100.0%</b>"]],
             col_widths=[2.5*inch, 2.5*inch, 1.5*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("Market values are indicative and based on last available closing prices. This statement is for "
                  f"informational purposes only. {BANK_NAME} is not liable for any pricing discrepancies.", styles["BankHeader"]),
    ]
    doc.build(story)
    return path


def generate_custody_tax_summary(client, tax_year):
    doc_id   = f"TAX-CUST-{tax_year}-{random.randint(1000,9999)}"
    filename = f"{client['client_id'].lower()}_custody_tax_summary_{doc_id}.pdf"
    path     = SAMPLES_DIR / filename

    if not REPORTLAB_OK:
        path.write_text(
            f"Global Bank N.A.\nAnnual Custody Tax Summary — Tax Year {tax_year}\n"
            f"Document ID: {doc_id}\nClient: {client['name']}\nAccount: {client['account']}\n"
            f"Form Type: 1099-DIV / 1099-B / 1099-INT\n"
        )
        return path

    doc, styles = _pdf_doc(path, f"Annual Custody Tax Summary — Tax Year {tax_year}")
    eq_sample = random.sample(EQUITIES[:6], 4)
    div_rows  = []
    total_div = 0.0
    for name, cusip, _ in eq_sample:
        gross = rnd(2000, 9000)
        qualified = gross  # pension fund: 0% WHT
        div_rows.append([name, cusip, f"${gross:,.2f}", "$0.00", f"${qualified:,.2f}", "$0.00"])
        total_div += gross

    # Capital gains (1099-B)
    proceeds   = rnd(40000, 120000)
    cost_basis = rnd(35000, 110000)
    gain       = round(proceeds - cost_basis, 2)
    cap_rows   = [
        [eq_sample[0][0], f"${proceeds:,.2f}", f"${cost_basis:,.2f}", f"${gain:,.2f}", "Long-Term"],
        [eq_sample[1][0], f"${rnd(20000,50000):,.2f}", f"${rnd(22000,55000):,.2f}",
         f"${rnd(-5000,8000):,.2f}", "Short-Term"],
    ]

    story = _bank_header(styles) + [
        Paragraph(f"Document Type: Custody Tax Document – Annual Tax Summary  |  Document ID: {doc_id}  |  Period: Tax Year {tax_year}", styles["BankHeader"]),
        Spacer(1, 0.1*inch),
        Paragraph(f"<b>Annual Custody Tax Summary — Tax Year {tax_year}</b>", styles["h2"]),
        Paragraph(f"This document summarises all taxable events processed through the {BANK_NAME} custody account of "
                  f"{client['name']} during the tax year ended December 31, {tax_year}. "
                  "Amounts are in USD unless otherwise noted.", styles["Normal"]),
        Spacer(1, 0.1*inch),
        _tbl([["Field","Detail"],["Taxpayer Name", client['name']],["TIN / EIN", client['tin']],
              ["Account No.", client['account']],["Tax Year", f"{tax_year} (January 1 – December 31)"],
              ["Form Type", "1099-DIV / 1099-B / 1099-INT"]], col_widths=[2.0*inch,4.5*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Dividend Income (Form 1099-DIV)</b>", styles["h3"]),
        _tbl([["Security","CUSIP","Gross Dividend","Fed Tax Withheld","Qualified Div.","Ordinary Div."]] + div_rows +
             [["<b>Total</b>","",f"<b>${total_div:,.2f}</b>","<b>$0.00</b>",f"<b>${total_div:,.2f}</b>","<b>$0.00</b>"]],
             col_widths=[1.8*inch,0.9*inch,1.0*inch,1.0*inch,0.9*inch,0.9*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Capital Gains Summary (Form 1099-B)</b>", styles["h3"]),
        _tbl([["Security","Proceeds (USD)","Cost Basis (USD)","Gain/Loss (USD)","Holding Period"]] + cap_rows,
             col_widths=[2.0*inch,1.1*inch,1.1*inch,1.1*inch,1.2*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("This document is prepared for informational purposes and does not constitute tax advice. "
                  f"Consult a qualified tax advisor. {BANK_NAME} is not responsible for any errors.", styles["BankHeader"]),
    ]
    doc.build(story)
    return path


def generate_tax_reclaim_application(client, year, quarter):
    claim_ref = f"RECLAIM-{year}-{random.randint(1000,9999)}"
    filename  = f"{client['client_id'].lower()}_tax_reclaim_{claim_ref}.pdf"
    path      = SAMPLES_DIR / filename

    if not REPORTLAB_OK:
        path.write_text(
            f"Global Bank N.A.\nWithholding Tax Reclaim Application\nDocument ID: {claim_ref}\n"
            f"Period: Q{quarter} {year}\nClaimant: {client['name']}\nAccount: {client['account']}\n"
        )
        return path

    doc, styles = _pdf_doc(path, "Withholding Tax Reclaim Application")
    reclaims = random.sample(FOREIGN_SECURITIES, random.randint(3, 5))
    items = []
    total_wht = 0.0
    total_rec = 0.0
    for ref_i, (sec, country, wht_rate) in enumerate(reclaims, 1):
        gross    = rnd(1500, 5000)
        wht      = round(gross * wht_rate / 100, 2)
        treaty   = 15
        reclaim  = round(gross * max(0, (wht_rate - treaty)) / 100, 2)
        items.append([f"R-{ref_i:03d}", sec, country, "Dividend",
                      f"${gross:,.2f}", f"{wht_rate}%", f"${wht:,.2f}", f"{treaty}%", f"${reclaim:,.2f}"])
        total_wht += wht
        total_rec += reclaim

    q_end = date(year, quarter * 3, 31 if quarter in (1,3) else 30)
    story = _bank_header(styles) + [
        Paragraph(f"Document Type: Custody Tax Reclaim Application  |  Document ID: {claim_ref}  |  Period: Q{quarter} {year}", styles["BankHeader"]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Withholding Tax Reclaim Application</b>", styles["h2"]),
        Paragraph(f"This document constitutes a formal application for reclaim of withholding taxes deducted from income "
                  f"received on foreign securities held in custody at {BANK_NAME} on behalf of {client['name']}. "
                  "The reclaim is submitted pursuant to applicable double tax treaties.", styles["Normal"]),
        Spacer(1, 0.1*inch),
        _tbl([["Field","Detail"],["Claimant Name", client['name']],["Account No.", client['account']],
              ["Tax Jurisdiction","United States"],["EIN", client['tin']],
              ["Claim Reference", claim_ref],["Application Date", str(q_end)],
              ["Custodian", f"{BANK_NAME}, Custody Tax Operations"]], col_widths=[2.0*inch,4.5*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Reclaim Items</b>", styles["h3"]),
        _tbl([["Ref","Security","Source Country","Income Type","Gross Income","WHT Rate","WHT Deducted","Treaty Rate","Reclaimable"]] + items,
             col_widths=[0.4*inch,1.4*inch,0.9*inch,0.6*inch,0.7*inch,0.5*inch,0.7*inch,0.6*inch,0.7*inch]),
        Spacer(1, 0.1*inch),
        _tbl([["Description","Amount (USD)"],
              ["Total WHT Deducted", f"${total_wht:,.2f}"],
              ["Non-Reclaimable (treaty min)", f"${round(total_wht - total_rec, 2):,.2f}"],
              ["<b>Total Reclaimable</b>", f"<b>${total_rec:,.2f}</b>"]],
             col_widths=[4.0*inch,2.5*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("Supporting documentation submitted: (1) IRS Form 6166 – Certificate of US Tax Residency, "
                  f"(2) {client['w_form']} Form, (3) Dividend vouchers, (4) Proof of beneficial ownership. "
                  "Contact: custody.taxreclaims@globalbank.com.", styles["Normal"]),
    ]
    doc.build(story)
    return path


def generate_corporate_actions_billing(client, year, quarter):
    doc_id   = f"CA-BILL-{year}-{random.randint(1000,9999)}"
    filename = f"{client['client_id'].lower()}_corporate_actions_billing_{doc_id}.pdf"
    path     = SAMPLES_DIR / filename
    q_end    = date(year, quarter * 3, 28)

    if not REPORTLAB_OK:
        path.write_text(
            f"Global Bank N.A.\nCustody Billing Statement: Corporate Actions\nDocument ID: {doc_id}\n"
            f"Period: Q{quarter} {year}\nClient: {client['name']}\nAccount: {client['account']}\n"
        )
        return path

    doc, styles = _pdf_doc(path, "Custody Billing Statement: Corporate Actions")
    events = random.sample(EQUITIES[:7], random.randint(4, 6))
    ca_rows = []
    proc_fee_total = 0.0
    for i, (name, cusip, _) in enumerate(events, 1):
        evt_type  = random.choice(CORP_ACTION_TYPES)
        evt_date  = date(year, random.randint(1, quarter*3), random.randint(1, 28))
        shares    = random.randint(1000, 12000)
        fee       = random.choice([125, 150, 200, 250, 375, 500])
        ca_rows.append([f"CA-{i:03d}", name, cusip, evt_type, str(evt_date), f"{shares:,}", f"${fee:,.2f}"])
        proc_fee_total += fee

    notif_fee    = 200.00
    election_fee = 250.00
    total_billed = round(proc_fee_total + notif_fee + election_fee, 2)

    story = _bank_header(styles) + [
        Paragraph(f"Document Type: Custody Billing – Corporate Actions  |  Document ID: {doc_id}  |  Period: Q{quarter} {year}", styles["BankHeader"]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Custody Billing Statement: Corporate Actions</b>", styles["h2"]),
        Paragraph(f"This statement details custody billing charges associated with corporate action events processed on behalf of "
                  f"{BANK_NAME} clients during Q{quarter} {year}. Charges reflect processing, notification, and election services.", styles["Normal"]),
        Spacer(1, 0.1*inch),
        _tbl([["Field","Detail"],["Client Name", client['name']],["Account No.", client['account']],
              ["Base Currency","USD"],["Statement Date", str(q_end)],
              ["Billing Period", f"{year}-{'0'+str((quarter-1)*3+1) if (quarter-1)*3+1 < 10 else (quarter-1)*3+1}-01 to {q_end}"]],
             col_widths=[2.0*inch,4.5*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("<b>Corporate Action Events — Billing Detail</b>", styles["h3"]),
        _tbl([["Event ID","Security","CUSIP","Event Type","Event Date","Shares Affected","Fee (USD)"]] + ca_rows,
             col_widths=[0.7*inch,1.6*inch,0.9*inch,0.9*inch,0.8*inch,0.9*inch,0.7*inch]),
        Spacer(1, 0.1*inch),
        _tbl([["Fee Category","Amount (USD)"],
              ["Corporate Action Processing", f"${proc_fee_total:,.2f}"],
              ["Notification Services", f"${notif_fee:,.2f}"],
              ["Election Processing", f"${election_fee:,.2f}"],
              ["<b>Total Billed</b>", f"<b>${total_billed:,.2f}</b>"]],
             col_widths=[4.0*inch,2.5*inch]),
        Spacer(1, 0.1*inch),
        Paragraph("Fees charged per Global Bank Custody Fee Schedule v2023. Disputes must be submitted within 30 days.", styles["BankHeader"]),
    ]
    doc.build(story)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# CSV generators
# ══════════════════════════════════════════════════════════════════════════════

def _write_csv(path: Path, fieldnames: list, rows: list) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def generate_transaction_report_csv(client, year, quarter):
    filename = f"{client['client_id'].lower()}_billing_transactions_Q{quarter}_{year}.csv"
    path     = SAMPLES_DIR / filename
    rows = []
    for i in range(1, random.randint(5, 9)):
        name, cusip, base_price = random.choice(EQUITIES)
        q_month  = (quarter - 1) * 3 + random.randint(1, 3)
        t_date   = date(year, q_month, random.randint(1, 28))
        s_date   = t_date + timedelta(days=2)
        txn_type = random.choice(["BUY", "SELL"])
        qty      = random.randint(100, 1000)
        price    = round(base_price * random.uniform(0.9, 1.1), 2)
        gross    = round(qty * price, 2)
        comm     = round(gross * 0.001, 2)
        sett_fee = 15.00
        net      = round(gross + comm + sett_fee if txn_type == "BUY" else gross - comm - sett_fee, 2)
        rows.append({
            "Txn_ID":           f"TXN-{i:03d}",
            "Account_No":       client["account"],
            "Trade_Date":       str(t_date),
            "Settlement_Date":  str(s_date),
            "Security_Name":    name,
            "CUSIP":            cusip,
            "Txn_Type":         txn_type,
            "Quantity":         qty,
            "Price_USD":        price,
            "Gross_Value_USD":  f"{gross:,.2f}",
            "Commission_USD":   comm,
            "Settlement_Fee_USD": sett_fee,
            "Net_Value_USD":    f"{net:,.2f}",
        })
    return _write_csv(path, list(rows[0].keys()), rows)


def generate_surcharge_statement_csv(client, year, quarter):
    filename = f"{client['client_id'].lower()}_billing_surcharges_Q{quarter}_{year}.csv"
    path     = SAMPLES_DIR / filename
    inv_ref  = f"INV-{year}-GB-{random.randint(1000,9999)}"
    rows = []
    for i, (stype, desc, rate, flat) in enumerate(random.sample(SURCHARGE_TYPES, random.randint(3, 5)), 1):
        q_month   = (quarter - 1) * 3 + random.randint(1, 3)
        app_date  = date(year, q_month, random.randint(5, 25))
        base_amt  = rnd(5.0, 500.0) if flat == 0.00 else flat
        surchg    = flat if flat > 0 else round(base_amt * float(rate.rstrip("%")) / 100, 2)
        rows.append({
            "Surcharge_ID":       f"SUR-{i:03d}",
            "Account_No":         client["account"],
            "Invoice_Ref":        inv_ref,
            "Surcharge_Type":     stype,
            "Description":        f"{desc} — {app_date}",
            "Applied_Date":       str(app_date),
            "Base_Amount_USD":    f"{base_amt:,.2f}" if flat == 0.00 else "0.00",
            "Surcharge_Rate":     rate,
            "Surcharge_Amount_USD": f"{surchg:,.2f}",
        })
    return _write_csv(path, list(rows[0].keys()), rows)


def generate_income_report_csv(client, year, quarter):
    filename = f"{client['client_id'].lower()}_billing_corporate_actions_income_Q{quarter}_{year}.csv"
    path     = SAMPLES_DIR / filename
    rows = []
    for i, (name, cusip, _) in enumerate(random.sample(EQUITIES, random.randint(4, 6)), 1):
        q_month  = (quarter - 1) * 3 + random.randint(1, 3)
        ex_date  = date(year, q_month, random.randint(5, 15))
        pay_date = ex_date + timedelta(days=random.randint(2, 5))
        inc_type = random.choice(["Cash Dividend", "Special Dividend", "Interest Income"])
        gross    = rnd(1500, 10000)
        wht      = round(gross * random.choice([0, 0, 0.15]) , 2)   # pension fund mostly 0%
        net      = round(gross - wht, 2)
        proc_fee = round(gross * 0.01, 2)
        rows.append({
            "Income_ID":        f"INC-{i:03d}",
            "Account_No":       client["account"],
            "Security_Name":    name,
            "CUSIP":            cusip,
            "Income_Type":      inc_type,
            "Ex_Date":          str(ex_date),
            "Pay_Date":         str(pay_date),
            "Gross_Amount_USD": f"{gross:,.2f}",
            "Tax_Withheld_USD": f"{wht:,.2f}",
            "Net_Amount_USD":   f"{net:,.2f}",
            "Processing_Fee_USD": f"{proc_fee:,.2f}",
        })
    return _write_csv(path, list(rows[0].keys()), rows)


def generate_tax_profile_csv(client):
    filename = f"{client['client_id'].lower()}_tax_profile_{client['account']}.csv"
    path     = SAMPLES_DIR / filename
    rows = [
        {"Field": "Account_No",                  "Value": client["account"]},
        {"Field": "Client_Name",                 "Value": client["name"]},
        {"Field": "Client_Type",                 "Value": client["client_type"]},
        {"Field": "Country_of_Residence",        "Value": "United States"},
        {"Field": "Tax_Identification_Number",   "Value": client["tin"]},
        {"Field": "Tax_Residency",               "Value": "US Domestic"},
        {"Field": "W8_W9_Form_On_File",          "Value": client["w_form"]},
        {"Field": "W8_W9_Expiry",                "Value": "N/A"},
        {"Field": "FATCA_Status",                "Value": client["fatca"]},
        {"Field": "FATCA_GIIN",                  "Value": f"GBNA-US-{client['account'][-5:]}-FATCA"},
        {"Field": "CRS_Status",                  "Value": "Reportable – US"},
        {"Field": "Treaty_Country",              "Value": "N/A"},
        {"Field": "Treaty_Rate_Dividends",       "Value": "N/A – Domestic"},
        {"Field": "Treaty_Rate_Interest",        "Value": "N/A – Domestic"},
        {"Field": "Default_WHT_Rate_Dividends",  "Value": "0%"},
        {"Field": "Default_WHT_Rate_Interest",   "Value": "0%"},
        {"Field": "Exempt_From_Backup_Withholding", "Value": "Yes"},
        {"Field": "QI_Status",                   "Value": "Non-QI"},
        {"Field": "Qualified_Intermediary",      "Value": "No"},
        {"Field": "Profile_Last_Updated",        "Value": "2024-01-15"},
        {"Field": "Approved_By",                 "Value": "Global Bank Tax Operations"},
        {"Field": "Next_Review_Date",            "Value": "2025-01-15"},
    ]
    return _write_csv(path, ["Field", "Value"], rows)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    random.seed(42)
    generated: list[dict] = []

    for client in CLIENTS:
        cid = client["client_id"]
        print(f"\n-- {client['name']} ({client['account']}) --")

        # ── PDFs (one per type per client) ────────────────────────────────
        p = generate_custody_billing_invoice(client, 2024, 1)
        generated.append({"path": str(p), "type": "CUSTODY_BILLING_INVOICE",  "client": cid, "format": "PDF"})
        print(f"  CUSTODY_BILLING_INVOICE     {p.name}")

        p = generate_portfolio_valuation(client, 2024, 1)
        generated.append({"path": str(p), "type": "PORTFOLIO_VALUATION",       "client": cid, "format": "PDF"})
        print(f"  PORTFOLIO_VALUATION         {p.name}")

        p = generate_custody_tax_summary(client, 2023)
        generated.append({"path": str(p), "type": "CUSTODY_TAX_SUMMARY",       "client": cid, "format": "PDF"})
        print(f"  CUSTODY_TAX_SUMMARY         {p.name}")

        p = generate_tax_reclaim_application(client, 2024, 1)
        generated.append({"path": str(p), "type": "TAX_RECLAIM_APPLICATION",   "client": cid, "format": "PDF"})
        print(f"  TAX_RECLAIM_APPLICATION     {p.name}")

        p = generate_corporate_actions_billing(client, 2024, 1)
        generated.append({"path": str(p), "type": "CORPORATE_ACTIONS_BILLING", "client": cid, "format": "PDF"})
        print(f"  CORPORATE_ACTIONS_BILLING   {p.name}")

        # ── CSVs ──────────────────────────────────────────────────────────
        p = generate_transaction_report_csv(client, 2024, 1)
        generated.append({"path": str(p), "type": "TRANSACTION_REPORT",        "client": cid, "format": "CSV"})
        print(f"  TRANSACTION_REPORT          {p.name}")

        p = generate_surcharge_statement_csv(client, 2024, 1)
        generated.append({"path": str(p), "type": "SURCHARGE_STATEMENT",       "client": cid, "format": "CSV"})
        print(f"  SURCHARGE_STATEMENT         {p.name}")

        p = generate_income_report_csv(client, 2024, 1)
        generated.append({"path": str(p), "type": "INCOME_REPORT",             "client": cid, "format": "CSV"})
        print(f"  INCOME_REPORT               {p.name}")

        p = generate_tax_profile_csv(client)
        generated.append({"path": str(p), "type": "TAX_PROFILE",               "client": cid, "format": "CSV"})
        print(f"  TAX_PROFILE                 {p.name}")

    # ── Include the real Source Files in the manifest ─────────────────────
    source_dir = Path(__file__).parent.parent / "Source Files"
    FORMAT_MAP = {
        "billing_invoice":                   ("CUSTODY_BILLING_INVOICE",  "CLIENT_APEX", "PDF"),
        "billing_valuation":                 ("PORTFOLIO_VALUATION",       "CLIENT_APEX", "PDF"),
        "custody_tax_document":              ("CUSTODY_TAX_SUMMARY",       "CLIENT_APEX", "PDF"),
        "tax_reclaim":                       ("TAX_RECLAIM_APPLICATION",   "CLIENT_APEX", "PDF"),
        "custody_billing_corporate_actions": ("CORPORATE_ACTIONS_BILLING", "CLIENT_APEX", "PDF"),
        "billing_transactions":              ("TRANSACTION_REPORT",        "CLIENT_APEX", "CSV"),
        "billing_surcharges":                ("SURCHARGE_STATEMENT",       "CLIENT_APEX", "CSV"),
        "billing_corporate_actions_income":  ("INCOME_REPORT",             "CLIENT_APEX", "CSV"),
        "tax_profile":                       ("TAX_PROFILE",               "CLIENT_APEX", "CSV"),
    }
    if source_dir.exists():
        print("\n-- Real source files (Source Files/) --")
        for src_file in sorted(source_dir.iterdir()):
            if src_file.suffix.lower() not in (".pdf", ".csv", ".xlsx"):
                continue
            stem = src_file.stem.lower()
            doc_type, client_id, fmt = "UNKNOWN", "CLIENT_APEX", src_file.suffix.lstrip(".").upper()
            for prefix, (t, c, f) in FORMAT_MAP.items():
                if stem.startswith(prefix):
                    doc_type, client_id, fmt = t, c, f
                    break
            generated.append({"path": str(src_file), "type": doc_type, "client": client_id, "format": fmt})
            print(f"  {doc_type:<30} {src_file.name}")

    manifest_path = SAMPLES_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(generated, indent=2))
    print(f"\nManifest -> {manifest_path}")
    print(f"Total files: {len(generated)}")


if __name__ == "__main__":
    main()
