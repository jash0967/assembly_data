"""Batch API classifier for Korean domestic news.

Async sibling of classify_news_kr.py. Uses OpenAI Batch API → 50% input/output discount.
24h SLA, suitable for production runs (≥1000 rows). Use the sync script for smoke tests.

Input  : data/news/news_analysis.duckdb, news_articles (Stage 1+2 적용본)
Output : data/news/news_analysis.duckdb, news_classifications (+ cleaning_version)

Workflow (분리된 commands; idempotent · resume 가능):
  submit   build JSONL from todo rows, chunk into ≤150 MB files, upload, create batches
  status   show progress of all in-flight batches
  collect  for any completed batch: download output, parse, insert to news_classifications
  full     submit → poll until all done → collect (한 번에)
  cancel   cancel an in-flight batch

State file: data/news/batch_state.json — preserves batch_id list across script runs.
Restart-safe: state lives outside the process so polling can resume after Ctrl-C.

Usage:
  python analyze/classify_news_kr_batch.py submit
  python analyze/classify_news_kr_batch.py status
  python analyze/classify_news_kr_batch.py collect
  python analyze/classify_news_kr_batch.py full
  python analyze/classify_news_kr_batch.py submit --limit 200  # half-test
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import duckdb
from openai import OpenAI
from dotenv import load_dotenv

import _bootstrap  # noqa: F401

import config
from prompts import SYSTEM_PROMPT

load_dotenv()
client = OpenAI()

PROMPT_VERSION = "v2_en_20260418"
MODEL = "gpt-4.1-mini"
BODY_CHAR_CAP = 30000
MAX_FILE_BYTES = 150 * 1024 * 1024   # 150 MB per JSONL (limit is 200 MB)

STATE_PATH = os.path.join(config.NEWS_DIR, "batch_state.json")


# ─────────────────────────── state helpers ───────────────────────────
def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"prompt_version": PROMPT_VERSION, "batches": []}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────── DB helpers ───────────────────────────
_CLASSIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS news_classifications (
    news_id          VARCHAR NOT NULL,
    prompt_version   VARCHAR NOT NULL,
    primary_attr     VARCHAR,
    secondary_attr   VARCHAR,
    tertiary_attr    VARCHAR,
    classified_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    error            VARCHAR,
    cleaning_version VARCHAR NOT NULL,
    PRIMARY KEY (news_id, prompt_version)
)
"""

_PROMPT_VERSIONS_DDL = """
CREATE TABLE IF NOT EXISTS news_prompt_versions (
    version    VARCHAR PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes      VARCHAR
)
"""


def _ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    """classify 스크립트가 분류 테이블의 소유자 — cleaning은 안 만든다."""
    con.execute(_CLASSIFICATIONS_DDL)
    con.execute(_PROMPT_VERSIONS_DDL)
    con.execute(
        "INSERT OR IGNORE INTO news_prompt_versions (version, notes) VALUES (?, ?)",
        [PROMPT_VERSION, "auto-registered by classify_news_kr_batch.py"],
    )


def _active_cleaning_version(con: duckdb.DuckDBPyConnection) -> str:
    """news_cleaning_runs의 가장 최근 활성 버전."""
    r = con.execute("""
        SELECT cleaning_version FROM news_cleaning_runs
        ORDER BY built_at DESC LIMIT 1
    """).fetchone()
    if r is None:
        raise SystemExit(
            "news_cleaning_runs에 활성 cleaning_version이 없습니다. "
            "먼저 python analyze/news_cleaning.py 를 실행하세요."
        )
    return r[0]


def fetch_todo(limit: int | None) -> list[tuple[str, str, str]]:
    """Stage 1+2 적용된 행 중 현재 prompt_version에서 미분류된 것만."""
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH)
    con.execute("PRAGMA disable_progress_bar")
    _ensure_tables(con)
    sql = f"""
      SELECT s.news_id, s.title, SUBSTR(s.content, 1, {BODY_CHAR_CAP})
      FROM news_articles s
      LEFT JOIN news_classifications c
        ON c.news_id = s.news_id
       AND c.prompt_version = '{PROMPT_VERSION}'
       AND c.error IS NULL
      WHERE c.news_id IS NULL
      ORDER BY s.news_id
    """
    if limit:
        sql += f" LIMIT {limit}"
    rows = con.execute(sql).fetchall()
    con.close()
    return [(r[0], r[1] or "", r[2] or "") for r in rows]


def insert_rows(rows: list[dict]) -> None:
    if not rows:
        return
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH)
    con.execute("PRAGMA disable_progress_bar")
    _ensure_tables(con)
    cleaning_v = _active_cleaning_version(con)
    con.executemany(
        """
        INSERT OR REPLACE INTO news_classifications
          (news_id, prompt_version, primary_attr, secondary_attr, tertiary_attr, error, cleaning_version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["news_id"], PROMPT_VERSION,
                r.get("primary_attr"), r.get("secondary_attr"),
                r.get("tertiary_attr"), r.get("error"),
                cleaning_v,
            )
            for r in rows
        ],
    )
    con.close()


# ─────────────────────────── JSONL builder ───────────────────────────
def _build_line(news_id: str, title: str, body: str) -> bytes:
    req = {
        "custom_id": news_id,
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Title: {title}\n\nBody: {body}"},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
    }
    return (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")


def chunk_lines(todo: list[tuple[str, str, str]]) -> list[bytes]:
    """Pack JSONL lines into <=MAX_FILE_BYTES buffers."""
    chunks: list[bytes] = []
    buf = io.BytesIO()
    for nid, t, b in todo:
        line = _build_line(nid, t, b)
        if buf.tell() + len(line) > MAX_FILE_BYTES and buf.tell() > 0:
            chunks.append(buf.getvalue())
            buf = io.BytesIO()
        buf.write(line)
    if buf.tell() > 0:
        chunks.append(buf.getvalue())
    return chunks


# ─────────────────────────── commands ───────────────────────────
def cmd_submit(args: argparse.Namespace) -> None:
    state = load_state()
    todo = fetch_todo(args.limit)
    print(f"todo rows: {len(todo):,}", flush=True)
    if not todo:
        print("nothing to submit", flush=True)
        return

    chunks = chunk_lines(todo)
    print(f"split into {len(chunks)} JSONL chunk(s)", flush=True)
    for i, data in enumerate(chunks, 1):
        size_mb = len(data) / 1024 / 1024
        n_req = data.count(b"\n")
        print(f"  chunk {i}/{len(chunks)}: {size_mb:.1f} MB, {n_req:,} requests", flush=True)

        f = client.files.create(file=("input.jsonl", data), purpose="batch")
        b = client.batches.create(
            input_file_id=f.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
            metadata={"label": f"news_kr_{PROMPT_VERSION}_{i}of{len(chunks)}"},
        )
        state["batches"].append({
            "batch_id": b.id,
            "input_file_id": f.id,
            "output_file_id": None,
            "error_file_id": None,
            "status": b.status,
            "n_requests": n_req,
            "collected": False,
            "submitted_at": _now(),
        })
        save_state(state)
        print(f"    → batch_id={b.id} (status={b.status})", flush=True)
    print("all submitted. run `status` to monitor.", flush=True)


def cmd_status(_args: argparse.Namespace) -> None:
    state = load_state()
    if not state["batches"]:
        print("no batches in state", flush=True)
        return
    for i, b in enumerate(state["batches"], 1):
        if b["collected"]:
            print(f"  [{i}] {b['batch_id']}  collected ✓", flush=True)
            continue
        live = client.batches.retrieve(b["batch_id"])
        rc = live.request_counts
        b["status"] = live.status
        b["output_file_id"] = live.output_file_id
        b["error_file_id"] = live.error_file_id
        print(
            f"  [{i}] {b['batch_id']}  status={live.status}  "
            f"total={rc.total} done={rc.completed} fail={rc.failed}",
            flush=True,
        )
    save_state(state)


def _parse_output(text: str) -> tuple[list[dict], int, int]:
    rows: list[dict] = []
    ok = err = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        nid = rec["custom_id"]
        if rec.get("error") or rec["response"]["status_code"] != 200:
            err += 1
            msg = json.dumps(rec.get("error") or rec["response"], ensure_ascii=False)[:200]
            rows.append({"news_id": nid, "error": msg})
            continue
        try:
            body = rec["response"]["body"]
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
            rows.append({
                "news_id": nid,
                "primary_attr": result.get("primary"),
                "secondary_attr": result.get("secondary"),
                "tertiary_attr": result.get("tertiary"),
                "error": None,
            })
            ok += 1
        except Exception as e:
            err += 1
            rows.append({"news_id": nid, "error": f"parse: {e}"[:200]})
    return rows, ok, err


def cmd_collect(args: argparse.Namespace) -> None:
    """DB에 쓰는 유일한 서브커맨드 — 여기만 감사 대상.

    `full` 도 마지막에 이 함수를 부르므로 두 경로가 함께 커버된다. main() 을
    통째로 감싸지 않는 이유: `full` 은 Batch 폴링으로 24h+ 열려 있고 Ctrl-C
    재개가 정상 운용이라(모듈 docstring), run 하나가 며칠 열린 채로 남는다.
    """
    import db_audit
    with db_audit.audit_run(__file__, config.NEWS_ANALYSIS_DB_PATH,
                            argv=sys.argv[1:]):
        _collect_batches(args)


def _collect_batches(_args: argparse.Namespace) -> None:
    state = load_state()
    refreshed_any = False
    for i, b in enumerate(state["batches"], 1):
        if b["collected"]:
            continue
        live = client.batches.retrieve(b["batch_id"])
        b["status"] = live.status
        b["output_file_id"] = live.output_file_id
        b["error_file_id"] = live.error_file_id
        if live.status != "completed":
            print(f"  [{i}] {b['batch_id']} not ready (status={live.status})", flush=True)
            continue
        out_id = live.output_file_id
        if not out_id:
            print(f"  [{i}] {b['batch_id']} completed but no output_file_id", flush=True)
            continue
        print(f"  [{i}] {b['batch_id']} downloading output…", flush=True)
        text = client.files.content(out_id).text
        rows, ok, err = _parse_output(text)
        print(f"      parsed: {ok:,} ok / {err:,} err", flush=True)
        insert_rows(rows)
        b["collected"] = True
        refreshed_any = True
        save_state(state)
        print(f"      ✓ inserted into news_classifications", flush=True)
    if not refreshed_any:
        print("nothing new to collect", flush=True)


def cmd_full(args: argparse.Namespace) -> None:
    cmd_submit(args)
    print("\npolling every 5 min until all batches completed…", flush=True)
    while True:
        state = load_state()
        outstanding = [b for b in state["batches"] if not b["collected"]]
        if not outstanding:
            break
        for b in outstanding:
            live = client.batches.retrieve(b["batch_id"])
            b["status"] = live.status
        save_state(state)
        statuses = [b["status"] for b in outstanding]
        print(f"  {_now()} — {statuses}", flush=True)
        if all(s in ("completed", "failed", "expired", "cancelled") for s in statuses):
            break
        time.sleep(300)
    cmd_collect(args)


def cmd_cancel(args: argparse.Namespace) -> None:
    state = load_state()
    for b in state["batches"]:
        if args.batch_id and b["batch_id"] != args.batch_id:
            continue
        if b["collected"]:
            continue
        live = client.batches.cancel(b["batch_id"])
        b["status"] = live.status
        print(f"  cancelled {b['batch_id']} → {live.status}", flush=True)
    save_state(state)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("submit")
    sp.add_argument("--limit", type=int, default=None)
    sub.add_parser("status")
    sub.add_parser("collect")
    fp = sub.add_parser("full")
    fp.add_argument("--limit", type=int, default=None)
    cp = sub.add_parser("cancel")
    cp.add_argument("--batch-id", default=None)

    args = p.parse_args()
    {"submit": cmd_submit, "status": cmd_status, "collect": cmd_collect,
     "full": cmd_full, "cancel": cmd_cancel}[args.cmd](args)


if __name__ == "__main__":
    main()
