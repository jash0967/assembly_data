"""Batch API classifier for Korean domestic news (Strict-filtered).

Async sibling of classify_news_kr.py. Uses OpenAI Batch API → 50% input/output discount.
24h SLA, suitable for production runs (≥1000 rows). Use the sync script for smoke tests.

Workflow (분리된 commands; idempotent · resume 가능):
  submit   build JSONL from todo rows, chunk into ≤150 MB files, upload, create batches
  status   show progress of all in-flight batches
  collect  for any completed batch: download output, parse, insert to news_classifications
  full     submit → poll until all done → collect (한 번에)
  cancel   cancel an in-flight batch

State file: data/news/batch_state.json — preserves batch_id list across script runs.
Restart-safe: state lives outside the process so polling can resume after Ctrl-C.

Usage:
  venv/Scripts/python.exe analyze/classify_news_kr_batch.py submit
  venv/Scripts/python.exe analyze/classify_news_kr_batch.py status
  venv/Scripts/python.exe analyze/classify_news_kr_batch.py collect
  venv/Scripts/python.exe analyze/classify_news_kr_batch.py full
  venv/Scripts/python.exe analyze/classify_news_kr_batch.py submit --limit 200  # half-test
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
from news_cleaning import STRICT_WHERE  # type: ignore[import-not-found]

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
def fetch_todo(limit: int | None) -> list[tuple[str, str, str]]:
    """Strict-pass rows not yet successfully classified at current prompt_version."""
    con = duckdb.connect(config.NEWS_DB_PATH, read_only=True)
    con.execute("PRAGMA disable_progress_bar")
    sql = f"""
      WITH strict_pass AS (
        SELECT news_id, title,
               REPLACE(REPLACE(content, '(AI학습 포함)', ''), '(AI 학습 포함)', '')
                 AS content
        FROM news_articles WHERE {STRICT_WHERE}
      )
      SELECT s.news_id, s.title, SUBSTR(s.content, 1, {BODY_CHAR_CAP})
      FROM strict_pass s
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
    con = duckdb.connect(config.NEWS_DB_PATH)
    con.execute("PRAGMA disable_progress_bar")
    con.executemany(
        """
        INSERT OR REPLACE INTO news_classifications
          (news_id, prompt_version, primary_attr, secondary_attr, tertiary_attr, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                r["news_id"], PROMPT_VERSION,
                r.get("primary_attr"), r.get("secondary_attr"),
                r.get("tertiary_attr"), r.get("error"),
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


def cmd_collect(_args: argparse.Namespace) -> None:
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
