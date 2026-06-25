"""
monitor_pipeline.py
End-to-end IntelliDoc pipeline health monitor.

Checks every stage in order:
  Stage 1 — S3 bucket prefixes (incoming / validated / quarantine)
  Stage 2 — Lambda invocation errors (CloudWatch)
  Stage 3 — Snowpipe status + copy history
  Stage 4 — Landing Stream + Routing Task history
  Stage 5 — Full Task DAG (Extract → Embed → Classify)
  Stage 6 — Document Processing Audit (stuck / failed files)
  Stage 7 — AI Processing Quality (classification + chunk stats)
  Stage 8 — Search Audit (recent queries + performance)
  Dashboard — One-line health summary

Run:
    python scripts/monitor_pipeline.py
    python scripts/monitor_pipeline.py --stage 6       # single stage only
    python scripts/monitor_pipeline.py --hours 48      # look back 48 hours
    python scripts/monitor_pipeline.py --dashboard     # summary only
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from textwrap import indent
from typing import Optional

import boto3
import snowflake.connector
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Config from .env ──────────────────────────────────────────────────────────
S3_BUCKET         = os.environ.get("S3_BUCKET_NAME",       "intellidoc-documents-source")
AWS_REGION        = os.environ.get("AWS_REGION",            "us-east-1")
SF_ACCOUNT        = os.environ.get("SNOWFLAKE_ACCOUNT",     "")
SF_USER           = os.environ.get("SNOWFLAKE_USER",        "")
SF_PASSWORD       = os.environ.get("SNOWFLAKE_PASSWORD",    "")
SF_ROLE           = os.environ.get("SNOWFLAKE_ROLE",        "INTELLIDOC_ROLE")
SF_WH             = os.environ.get("SNOWFLAKE_WAREHOUSE",   "INTELLIDOC_WH")
SF_DB             = os.environ.get("SNOWFLAKE_DATABASE",    "INTELLIDOC_DB")
SF_SCHEMA         = os.environ.get("SNOWFLAKE_SCHEMA",      "INTELLIDOC")
PIPE_NAME         = "INTELLIDOC_DB.INTELLIDOC.INTELLIDOC_LANDING_PIPE"
STREAM_NAME       = "INTELLIDOC_DB.INTELLIDOC.LANDING_STREAM"
LAMBDA_LOG_GROUP  = "/aws/lambda/intellidoc-validation"

# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    return f"{GREEN}OK{RESET}    {msg}"
def warn(msg):  return f"{YELLOW}WARN{RESET}  {msg}"
def err(msg):   return f"{RED}ERROR{RESET} {msg}"
def hdr(msg):   return f"\n{BOLD}{CYAN}{'─'*60}{RESET}\n{BOLD}{CYAN}  {msg}{RESET}\n{'─'*60}"

# ── Connections ───────────────────────────────────────────────────────────────

def sf_conn():
    return snowflake.connector.connect(
        account=SF_ACCOUNT, user=SF_USER, password=SF_PASSWORD,
        role=SF_ROLE, warehouse=SF_WH, database=SF_DB, schema=SF_SCHEMA,
    )

def sf_query(sql: str, params=None) -> list[dict]:
    with sf_conn() as conn:
        with conn.cursor(snowflake.connector.DictCursor) as cur:
            cur.execute(sql, params or [])
            return cur.fetchall()

def sf_scalar(sql: str, default=None):
    rows = sf_query(sql)
    if not rows:
        return default
    return list(rows[0].values())[0]

# ══════════════════════════════════════════════════════════════════════════════
# Stage checkers
# ══════════════════════════════════════════════════════════════════════════════

def check_s3(hours: int):
    print(hdr("STAGE 1 — S3 Bucket Prefixes"))
    s3 = boto3.client("s3", region_name=AWS_REGION)
    prefixes = {
        "incoming/":    "Files awaiting Lambda validation",
        "validated/":   "Files passed validation (Snowpipe source)",
        "quarantine/":  "Files that failed validation",
    }
    for prefix, desc in prefixes.items():
        try:
            resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
            count = resp.get("KeyCount", 0)
            msg   = f"{prefix:<20} {count:>4} object(s)   {desc}"
            if prefix == "quarantine/" and count > 0:
                print(warn(msg))
            else:
                print(ok(msg) if count >= 0 else warn(msg))
        except Exception as e:
            print(err(f"{prefix:<20} {e}"))


def check_lambda(hours: int):
    print(hdr("STAGE 2 — Lambda Validation Errors"))
    try:
        logs = boto3.client("logs", region_name=AWS_REGION)
        start_ms = int((datetime.now(timezone.utc).timestamp() - hours * 3600) * 1000)
        for pattern, label in [("ERROR", "Errors"), ("Validated", "Successes"),
                                ("quarantine", "Quarantined")]:
            resp = logs.filter_log_events(
                logGroupName=LAMBDA_LOG_GROUP,
                startTime=start_ms,
                filterPattern=pattern,
            )
            count = len(resp.get("events", []))
            msg   = f"{label:<15} {count:>4} event(s) in last {hours}h"
            print(warn(msg) if label == "Errors" and count > 0 else ok(msg))
    except Exception as e:
        print(warn(f"CloudWatch unavailable (check AWS_PROFILE): {e}"))


def check_snowpipe(hours: int):
    print(hdr("STAGE 3 — Snowpipe Status + Copy History"))
    try:
        raw    = sf_scalar(f"SELECT SYSTEM$PIPE_STATUS('{PIPE_NAME}')::VARCHAR")
        status = json.loads(raw) if raw else {}
        state  = status.get("executionState", "UNKNOWN")
        pending = status.get("pendingFileCount", 0)
        last_ts = status.get("lastIngestedTimestamp", "never")

        print(ok(f"Pipe state       : {state}") if state == "RUNNING" else err(f"Pipe state       : {state}"))
        print(ok(f"Pending files    : {pending}") if pending == 0 else warn(f"Pending files    : {pending}"))
        print(ok(f"Last ingested    : {last_ts}"))
    except Exception as e:
        print(err(f"Pipe status error: {e}"))

    try:
        rows = sf_query(f"""
            SELECT FILE_NAME, STATUS, ROW_COUNT, FIRST_ERROR_MESSAGE,
                   LAST_LOAD_TIME
            FROM TABLE(INFORMATION_SCHEMA.COPY_HISTORY(
                TABLE_NAME => 'INTELLIDOC_DB.INTELLIDOC.LANDING_FILES',
                START_TIME => DATEADD(HOURS, -{hours}, CURRENT_TIMESTAMP())
            ))
            ORDER BY LAST_LOAD_TIME DESC
            LIMIT 10
        """)
        print(f"\n  Copy history (last {hours}h, max 10 rows):")
        if not rows:
            print(warn("  No copy history found"))
        for r in rows:
            status_icon = ok("") if r["STATUS"] == "LOADED" else warn("")
            print(f"  {status_icon} {r['STATUS']:<10} {r['FILE_NAME']:<60} rows={r['ROW_COUNT']}  {r['LAST_LOAD_TIME']}")
    except Exception as e:
        print(warn(f"Copy history error: {e}"))


def check_stream_and_routing(hours: int):
    print(hdr("STAGE 4 — Landing Stream + Routing Task"))
    try:
        has_data = sf_scalar(f"SELECT SYSTEM$STREAM_HAS_DATA('{STREAM_NAME}')")
        print(ok("Stream: no pending data") if not has_data else warn("Stream: has unprocessed data — ROUTING_TASK may not have run"))
        stream_rows = sf_query("SELECT COUNT(*) AS cnt FROM INTELLIDOC_DB.INTELLIDOC.LANDING_STREAM")
        print(ok(f"Stream rows waiting: {stream_rows[0]['CNT']}"))
    except Exception as e:
        print(err(f"Stream check error: {e}"))

    _print_task_history("ROUTING_TASK", hours)


def _print_task_history(task_name: str, hours: int):
    try:
        rows = sf_query(f"""
            SELECT STATE, SCHEDULED_TIME, COMPLETED_TIME,
                   DATEDIFF(SECOND, SCHEDULED_TIME, COMPLETED_TIME) AS SECS,
                   RETURN_VALUE, ERROR_MESSAGE
            FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
                SCHEDULED_TIME_RANGE_START => DATEADD(HOURS, -{hours}, CURRENT_TIMESTAMP()),
                TASK_NAME => '{task_name}'
            ))
            ORDER BY SCHEDULED_TIME DESC
            LIMIT 5
        """)
        print(f"\n  {task_name} history (last {hours}h, max 5 runs):")
        if not rows:
            print(warn(f"  No runs found — task may be SUSPENDED"))
            return
        for r in rows:
            state = r["STATE"]
            icon  = ok("") if state == "SUCCEEDED" else (warn("") if state == "SKIPPED" else err(""))
            rv    = f"  result={r['RETURN_VALUE']}" if r.get("RETURN_VALUE") else ""
            em    = f"  ERROR: {r['ERROR_MESSAGE']}" if r.get("ERROR_MESSAGE") else ""
            print(f"  {icon} {state:<12} {str(r['SCHEDULED_TIME']):<30} {r['SECS'] or 0:>4}s{rv}{em}")
    except Exception as e:
        print(warn(f"  {task_name} history error: {e}"))


def check_task_dag(hours: int):
    print(hdr("STAGE 5 — Full Task DAG (Extract → Embed → Classify)"))
    for task in ["EXTRACT_TASK", "EMBED_TASK", "CLASSIFY_TASK"]:
        _print_task_history(task, hours)


def check_processing_audit(hours: int):
    print(hdr("STAGE 6 — Document Processing Audit"))

    # Status summary
    rows = sf_query("""
        SELECT PROCESSING_STATUS, FILE_FORMAT, COUNT(*) AS CNT
        FROM DOCUMENT_REGISTRY
        GROUP BY PROCESSING_STATUS, FILE_FORMAT
        ORDER BY
            CASE PROCESSING_STATUS
                WHEN 'RECEIVED'       THEN 1 WHEN 'VALIDATED'     THEN 2
                WHEN 'STAGED'         THEN 3 WHEN 'TEXT_EXTRACTED' THEN 4
                WHEN 'EMBEDDED'       THEN 5 WHEN 'AVAILABLE'      THEN 6
                WHEN 'FAILED'         THEN 7 WHEN 'ARCHIVED'       THEN 8
                ELSE 9
            END
    """)
    print("\n  Document Registry — status summary:")
    for r in rows:
        icon = err("") if r["PROCESSING_STATUS"] == "FAILED" else ok("")
        print(f"  {icon} {r['PROCESSING_STATUS']:<18} {r['FILE_FORMAT']:<6} {r['CNT']:>4} file(s)")

    # Stuck files
    stuck = sf_query("""
        SELECT FILE_ID, FILE_NAME, PROCESSING_STATUS,
               DATEDIFF(MINUTE, UPDATED_TS, SYSDATE()) AS MINS_STUCK
        FROM DOCUMENT_REGISTRY
        WHERE PROCESSING_STATUS NOT IN ('AVAILABLE','ARCHIVED','FAILED')
          AND UPDATED_TS < DATEADD(MINUTE, -30, SYSDATE())
        ORDER BY MINS_STUCK DESC
    """)
    print(f"\n  Files stuck > 30 min: {len(stuck)}")
    for r in stuck:
        print(warn(f"  {r['FILE_NAME']:<60} status={r['PROCESSING_STATUS']}  stuck={r['MINS_STUCK']}min"))

    # Failed files
    failed = sf_query("""
        SELECT dr.FILE_NAME, a.STEP_NAME, a.ERROR_MESSAGE, a.START_TS
        FROM DOCUMENT_REGISTRY dr
        JOIN DOCUMENT_PROCESSING_AUDIT a ON dr.FILE_ID = a.FILE_ID
        WHERE a.STATUS = 'FAILED'
        ORDER BY a.START_TS DESC
        LIMIT 10
    """)
    print(f"\n  Failed steps (last 10):")
    if not failed:
        print(ok("  No failures"))
    for r in failed:
        print(err(f"  [{r['STEP_NAME']}] {r['FILE_NAME']} — {r['ERROR_MESSAGE'][:120] if r['ERROR_MESSAGE'] else 'no message'}"))


def check_ai_quality(hours: int):
    print(hdr("STAGE 7 — AI Processing Quality"))

    # Classification breakdown
    cls_rows = sf_query("""
        SELECT DOCUMENT_TYPE, CLASSIFICATION_CONFIDENCE,
               COUNT(*) AS CNT, MODEL_NAME
        FROM DOCUMENT_CLASSIFICATION
        GROUP BY DOCUMENT_TYPE, CLASSIFICATION_CONFIDENCE, MODEL_NAME
        ORDER BY CNT DESC
    """)
    print("\n  Classification results:")
    if not cls_rows:
        print(warn("  No classified documents yet"))
    for r in cls_rows:
        icon = warn("") if r["CLASSIFICATION_CONFIDENCE"] == "low" else ok("")
        print(f"  {icon} {r['DOCUMENT_TYPE']:<35} {r['CLASSIFICATION_CONFIDENCE']:<8} {r['CNT']:>3} file(s)  model={r['MODEL_NAME']}")

    # Chunk stats
    chunk_rows = sf_query("""
        SELECT dr.FILE_NAME, dr.FILE_FORMAT,
               COUNT(dc.CHUNK_ID)   AS CHUNKS,
               AVG(dc.TOKEN_COUNT)  AS AVG_TOKENS,
               MAX(dc.TOKEN_COUNT)  AS MAX_TOKENS
        FROM DOCUMENT_REGISTRY dr
        JOIN DOCUMENT_CHUNKS dc ON dr.FILE_ID = dc.FILE_ID
        GROUP BY dr.FILE_NAME, dr.FILE_FORMAT
        ORDER BY CHUNKS DESC
    """)
    print("\n  Chunk statistics per file:")
    if not chunk_rows:
        print(warn("  No chunks found — embedding may not have run"))
    for r in chunk_rows:
        avg = round(r["AVG_TOKENS"] or 0)
        print(ok(f"  {r['FILE_NAME']:<55} {r['FORMAT']:<5} chunks={r['CHUNKS']:>3}  avg_tokens={avg}"))


def check_search_audit(hours: int):
    print(hdr("STAGE 8 — Search Audit"))

    recent = sf_query(f"""
        SELECT CLIENT_ACCOUNT_ID, SEARCH_TERM, SEARCH_CONFIDENCE,
               RESULT_COUNT, EXECUTION_TIME_MS, SEARCH_TS
        FROM SEARCH_AUDIT
        WHERE SEARCH_TS > DATEADD(HOURS, -{hours}, CURRENT_TIMESTAMP())
        ORDER BY SEARCH_TS DESC
        LIMIT 10
    """)
    print(f"\n  Recent searches (last {hours}h, max 10):")
    if not recent:
        print(ok("  No searches yet"))
    for r in recent:
        icon = warn("") if r["SEARCH_CONFIDENCE"] == "low" else ok("")
        print(f"  {icon} [{r['CLIENT_ACCOUNT_ID']:<15}] {r['SEARCH_TERM'][:50]:<52} conf={r['SEARCH_CONFIDENCE']:<8} {r['EXECUTION_TIME_MS']}ms")

    perf = sf_query("""
        SELECT SEARCH_CONFIDENCE,
               COUNT(*)                AS TOTAL,
               ROUND(AVG(EXECUTION_TIME_MS)) AS AVG_MS,
               MAX(EXECUTION_TIME_MS)  AS MAX_MS
        FROM SEARCH_AUDIT
        GROUP BY SEARCH_CONFIDENCE
    """)
    if perf:
        print("\n  Performance by confidence:")
        for r in perf:
            print(ok(f"  {r['SEARCH_CONFIDENCE']:<10} total={r['TOTAL']:>3}  avg={r['AVG_MS']:>6}ms  max={r['MAX_MS']:>6}ms"))


def print_dashboard(hours: int):
    print(hdr("PIPELINE HEALTH DASHBOARD"))
    try:
        pipe_raw = sf_scalar(f"SELECT SYSTEM$PIPE_STATUS('{PIPE_NAME}')::VARCHAR")
        pipe     = json.loads(pipe_raw) if pipe_raw else {}

        has_data  = sf_scalar(f"SELECT SYSTEM$STREAM_HAS_DATA('{STREAM_NAME}')")
        available = sf_scalar("SELECT COUNT(*) FROM DOCUMENT_REGISTRY WHERE PROCESSING_STATUS='AVAILABLE'", 0)
        failed    = sf_scalar("SELECT COUNT(*) FROM DOCUMENT_REGISTRY WHERE PROCESSING_STATUS='FAILED'", 0)
        stuck     = sf_scalar("""
            SELECT COUNT(*) FROM DOCUMENT_REGISTRY
            WHERE PROCESSING_STATUS NOT IN ('AVAILABLE','ARCHIVED','FAILED')
              AND UPDATED_TS < DATEADD(MINUTE,-30,SYSDATE())""", 0)
        searches  = sf_scalar(f"""
            SELECT COUNT(*) FROM SEARCH_AUDIT
            WHERE SEARCH_TS > DATEADD(HOURS,-{hours},CURRENT_TIMESTAMP())""", 0)

        pipe_state   = pipe.get("executionState", "UNKNOWN")
        pending      = pipe.get("pendingFileCount", 0)

        print(f"""
  {'Component':<30} {'Status':<12} {'Detail'}
  {'─'*70}
  {'Snowpipe':<30} {(ok('RUNNING') if pipe_state=='RUNNING' else err(pipe_state)):<20} pending={pending}
  {'Landing Stream':<30} {(warn('HAS DATA') if has_data else ok('EMPTY')):<20} unprocessed rows
  {'Files Available to Search':<30} {ok(str(available)):<20}
  {'Files Failed':<30} {(err(str(failed)) if failed>0 else ok('0')):<20}
  {'Files Stuck >30min':<30} {(warn(str(stuck)) if stuck>0 else ok('0')):<20}
  {'Searches (last {hours}h)':<30} {ok(str(searches)):<20}
  {'Checked at':<30} {str(datetime.now().strftime('%Y-%m-%d %H:%M:%S')):<20}
        """)

        # Overall health
        if failed == 0 and stuck == 0 and pipe_state == "RUNNING":
            print(f"  {GREEN}{BOLD}Overall: HEALTHY{RESET}\n")
        elif failed > 0 or stuck > 0:
            print(f"  {RED}{BOLD}Overall: NEEDS ATTENTION — check stages 6/7{RESET}\n")
        else:
            print(f"  {YELLOW}{BOLD}Overall: WARNING — review pipe/stream status{RESET}\n")

    except Exception as e:
        print(err(f"Dashboard error: {e}"))


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="IntelliDoc Pipeline Monitor")
    parser.add_argument("--stage",     type=int, help="Run only a specific stage (1–8)")
    parser.add_argument("--hours",     type=int, default=24, help="Look-back window in hours (default: 24)")
    parser.add_argument("--dashboard", action="store_true", help="Print summary dashboard only")
    args = parser.parse_args()

    if not SF_ACCOUNT:
        print(err("SNOWFLAKE_ACCOUNT not set — copy .env.example to .env and fill in values"))
        sys.exit(1)

    print(f"\n{BOLD}IntelliDoc Pipeline Monitor{RESET}  |  look-back: {args.hours}h  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.dashboard:
        print_dashboard(args.hours)
        return

    stages = {
        1: lambda: check_s3(args.hours),
        2: lambda: check_lambda(args.hours),
        3: lambda: check_snowpipe(args.hours),
        4: lambda: check_stream_and_routing(args.hours),
        5: lambda: check_task_dag(args.hours),
        6: lambda: check_processing_audit(args.hours),
        7: lambda: check_ai_quality(args.hours),
        8: lambda: check_search_audit(args.hours),
    }

    if args.stage:
        if args.stage not in stages:
            print(err(f"Invalid stage {args.stage}. Must be 1–8."))
            sys.exit(1)
        stages[args.stage]()
    else:
        for fn in stages.values():
            try:
                fn()
            except Exception as e:
                print(err(f"Stage error: {e}"))

    print_dashboard(args.hours)


if __name__ == "__main__":
    main()
