"""GPT 10-attribute classification for Korean domestic news.

Companion to classify_articles.py (Guardian / NYT) and classify_bills.py.
Uses prompts.SYSTEM_PROMPT (Carvao v2 EN) so labels are cross-source comparable.

Input  : data/news/news_analysis.duckdb, news_articles
         (Stage 1+2 적용본 — Rule B1·B2 정화된 content, Stage 2 통과 행만)
Output : data/news/news_analysis.duckdb, news_classifications table
         PK = (news_id, prompt_version='v2_en_20260418')
         cleaning_version 컬럼 = news_cleaning_runs의 가장 최근 활성 버전

Usage:
  python analyze/classify_news_kr.py
  python analyze/classify_news_kr.py --limit 50      # smoke test
  python analyze/classify_news_kr.py --workers 30
  python analyze/classify_news_kr.py --force         # drop + reclassify all

Calibrated for OpenAI Tier 3 (gpt-4.1-mini: 5K RPM / 4M TPM).
Default workers=30 → ~3K-6K RPM effective with built-in retry on 429.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

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
# Full body — gpt-4.1-mini has 1M context, longest article is ~27K chars.
# Cap kept extremely high to guard against pathological outliers only.
BODY_CHAR_CAP = 30000

# Shared write connection — DuckDB serialises writes per process
_db_lock = threading.Lock()
_db_con: duckdb.DuckDBPyConnection | None = None
_active_cleaning_version: str | None = None


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


def _open_db() -> duckdb.DuckDBPyConnection:
    global _db_con
    if _db_con is None:
        _db_con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH)
        _db_con.execute("PRAGMA disable_progress_bar")
        # classify가 분류 테이블의 소유자 — cleaning은 안 만든다.
        _db_con.execute(_CLASSIFICATIONS_DDL)
        _db_con.execute(_PROMPT_VERSIONS_DDL)
        _db_con.execute(
            "INSERT OR IGNORE INTO news_prompt_versions (version, notes) VALUES (?, ?)",
            [PROMPT_VERSION, "auto-registered by classify_news_kr.py"],
        )
    return _db_con


def active_cleaning_version() -> str:
    """news_cleaning_runs의 가장 최근 활성 버전. 한 번 캐시."""
    global _active_cleaning_version
    if _active_cleaning_version is None:
        con = _open_db()
        r = con.execute("""
            SELECT cleaning_version FROM news_cleaning_runs
            ORDER BY built_at DESC LIMIT 1
        """).fetchone()
        if r is None:
            raise SystemExit(
                "news_cleaning_runs에 활성 cleaning_version이 없습니다. "
                "먼저 python analyze/news_cleaning.py 를 실행하세요."
            )
        _active_cleaning_version = r[0]
    return _active_cleaning_version


def fetch_todo(force: bool, limit: int | None) -> list[tuple[str, str, str]]:
    """Return [(news_id, title, content_clipped)] for rows needing classification.

    Reads from news_analysis.duckdb where news_articles.content is already
    Stage 1 sanitized (Rule B1·B2 applied at build time).
    """
    con = _open_db()
    if force:
        with _db_lock:
            con.execute(
                "DELETE FROM news_classifications WHERE prompt_version = ?",
                [PROMPT_VERSION],
            )
            print("--force: dropped all rows at current prompt_version", flush=True)

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
    return [(r[0], r[1] or "", r[2] or "") for r in rows]


def classify_one(news_id: str, title: str, body: str, max_retries: int = 6) -> dict:
    user_msg = f"Title: {title}\n\nBody: {body}"
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)
            return {
                "news_id": news_id,
                "primary_attr": result.get("primary"),
                "secondary_attr": result.get("secondary"),
                "tertiary_attr": result.get("tertiary"),
                "error": None,
            }
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate" in msg.lower() or "timeout" in msg.lower():
                time.sleep(2 ** attempt + 1)
                continue
            return {
                "news_id": news_id,
                "primary_attr": None,
                "secondary_attr": None,
                "tertiary_attr": None,
                "error": msg[:200],
            }
    return {
        "news_id": news_id,
        "primary_attr": None,
        "secondary_attr": None,
        "tertiary_attr": None,
        "error": "max retries exceeded (rate limit)",
    }


def insert_batch(rows: list[dict]) -> None:
    if not rows:
        return
    con = _open_db()
    cleaning_v = active_cleaning_version()
    with _db_lock:
        con.executemany(
            """
            INSERT OR REPLACE INTO news_classifications
              (news_id, prompt_version, primary_attr, secondary_attr, tertiary_attr, error, cleaning_version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["news_id"],
                    PROMPT_VERSION,
                    r["primary_attr"],
                    r["secondary_attr"],
                    r["tertiary_attr"],
                    r["error"],
                    cleaning_v,
                )
                for r in rows
            ],
        )


def run(workers: int, limit: int | None, force: bool) -> None:
    print(f"=== KR domestic news classification (model={MODEL}, v={PROMPT_VERSION}) ===", flush=True)
    todo = fetch_todo(force=force, limit=limit)
    total = len(todo)
    print(f"  to classify: {total:,}", flush=True)
    if not total:
        print("  nothing new", flush=True)
        return

    pending: list[dict] = []
    FLUSH_EVERY = 200
    done_n = 0
    err_n = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(classify_one, nid, t, b): nid for nid, t, b in todo
        }
        for fut in as_completed(futs):
            row = fut.result()
            if row["error"]:
                err_n += 1
            pending.append(row)
            done_n += 1
            if len(pending) >= FLUSH_EVERY:
                insert_batch(pending)
                pending = []
            if done_n % 500 == 0 or done_n == total:
                dt = time.time() - t0
                rate = done_n / dt if dt else 0
                eta = (total - done_n) / rate if rate else 0
                print(
                    f"    {done_n:>6,}/{total:,} "
                    f"({done_n/total*100:5.1f}%) "
                    f"errors={err_n} "
                    f"rate={rate*60:.0f}/min "
                    f"eta={eta/60:.1f} min",
                    flush=True,
                )

    insert_batch(pending)
    print(f"  done in {(time.time() - t0)/60:.1f} min, {err_n} errors", flush=True)


def summarize() -> None:
    con = _open_db()
    print("\n=== Final distribution ===", flush=True)
    dist = con.execute(
        f"""
        SELECT primary_attr, COUNT(*) AS n
        FROM news_classifications
        WHERE prompt_version = '{PROMPT_VERSION}' AND error IS NULL
        GROUP BY primary_attr
        ORDER BY n DESC
        """
    ).fetchall()
    tot = sum(n for _, n in dist) or 1
    for attr, n in dist:
        print(f"  {(attr or '-'):<55} {n:>7,} ({n/tot*100:5.1f}%)", flush=True)
    print(f"\n  total classified: {tot:,}", flush=True)
    err = con.execute(
        f"""
        SELECT COUNT(*) FROM news_classifications
        WHERE prompt_version = '{PROMPT_VERSION}' AND error IS NOT NULL
        """
    ).fetchone()[0]
    if err:
        print(f"  errors: {err:,}", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=30)
    p.add_argument("--limit", type=int, default=None, help="smoke-test cap")
    p.add_argument("--force", action="store_true", help="drop existing rows at this version")
    args = p.parse_args()

    global _db_con
    try:
        run(workers=args.workers, limit=args.limit, force=args.force)
        summarize()
    finally:
        # classify_bills.py·download_*.py 와 같은 관행. 열어둔 채 끝내면
        # 다음 read-only 접속(감사 로그의 종료 스냅샷 포함)이 막힌다.
        if _db_con is not None:
            _db_con.close()
            _db_con = None


if __name__ == "__main__":
    import db_audit
    with db_audit.audit_run(__file__, config.NEWS_ANALYSIS_DB_PATH, argv=sys.argv[1:]):
        main()
