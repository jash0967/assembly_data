"""Build data/news/news.duckdb from raw_news_archive JSON files.

Walks data/news/raw_news_archive/ for all *.json files, parses each, and
inserts into news_articles table. Idempotent on news_id (PK).

Usage:
    venv/Scripts/python.exe collect/build_news_db.py
    venv/Scripts/python.exe collect/build_news_db.py --rebuild
"""
import _bootstrap  # noqa: F401

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterator

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import duckdb


REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "data" / "news" / "raw_news_archive"
DB_PATH = REPO_ROOT / "data" / "news" / "news.duckdb"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS news_articles (
  news_id              TEXT PRIMARY KEY,
  title                TEXT NOT NULL,
  content              TEXT NOT NULL,
  dateline             TIMESTAMP WITH TIME ZONE,
  published_at         TIMESTAMP WITH TIME ZONE,
  enveloped_at         TIMESTAMP WITH TIME ZONE,
  provider             TEXT NOT NULL,
  byline               TEXT,
  provider_link_page   TEXT,
  category             JSON,
  category_incident    JSON,
  hilight              TEXT,
  printing_page        TEXT,
  ingested_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_news_provider     ON news_articles(provider)",
    "CREATE INDEX IF NOT EXISTS idx_news_published    ON news_articles(published_at)",
    "CREATE INDEX IF NOT EXISTS idx_news_provider_pub ON news_articles(provider, published_at)",
]

INSERT_SQL = """
INSERT OR IGNORE INTO news_articles
  (news_id, title, content, dateline, published_at, enveloped_at,
   provider, byline, provider_link_page, category, category_incident,
   hilight, printing_page)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

BATCH_SIZE = 5000


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def iter_json_files(root: Path) -> Iterator[Path]:
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".json"):
                yield Path(dirpath) / fn


def load_record(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        print(f"[skip] {path.name}: {e}", file=sys.stderr)
        return None
    if not d.get("news_id"):
        return None
    return (
        d["news_id"],
        d.get("title", "") or "",
        d.get("content", "") or "",
        parse_iso(d.get("dateline")),
        parse_iso(d.get("published_at")),
        parse_iso(d.get("enveloped_at")),
        d.get("provider", "") or "",
        d.get("byline"),
        d.get("provider_link_page"),
        json.dumps(d.get("category") or [], ensure_ascii=False),
        json.dumps(d.get("category_incident") or [], ensure_ascii=False),
        d.get("hilight"),
        d.get("printing_page"),
    )


def build(rebuild: bool = False) -> None:
    if not ARCHIVE_DIR.exists():
        raise SystemExit(f"missing: {ARCHIVE_DIR}")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"DB:      {DB_PATH}")
    print(f"Archive: {ARCHIVE_DIR}")
    print()

    con = duckdb.connect(str(DB_PATH))
    if rebuild:
        print("[rebuild] dropping news_articles ...")
        con.execute("DROP TABLE IF EXISTS news_articles")
    con.execute(SCHEMA_SQL)
    for sql in INDEXES_SQL:
        con.execute(sql)

    t0 = time.time()
    batch: list = []
    skipped = 0
    seen = 0
    provider_running: Counter = Counter()

    def flush():
        if batch:
            con.executemany(INSERT_SQL, batch)
            batch.clear()

    for p in iter_json_files(ARCHIVE_DIR):
        seen += 1
        rec = load_record(p)
        if rec is None:
            skipped += 1
            continue
        batch.append(rec)
        provider_running[rec[6]] += 1
        if len(batch) >= BATCH_SIZE:
            flush()
            elapsed = time.time() - t0
            rate = seen / elapsed if elapsed > 0 else 0
            print(
                f"[{seen:>7}] skip={skipped:>4} rate={rate:>5.0f}/s "
                f"elapsed={elapsed:>5.0f}s",
                flush=True,
            )

    flush()
    elapsed = time.time() - t0
    print()
    print(f"DONE: seen={seen:,} skipped={skipped:,} elapsed={elapsed:.0f}s")
    print()

    final_count = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    print(f"Total rows in news_articles: {final_count:,}")
    print()
    print("Per-provider:")
    for prov, cnt in con.execute(
        "SELECT provider, COUNT(*) FROM news_articles GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  {prov:<14} {cnt:>10,}")

    dmin, dmax = con.execute(
        "SELECT MIN(published_at), MAX(published_at) FROM news_articles"
    ).fetchone()
    print()
    print(f"Date range:    {dmin}  ~  {dmax}")

    dup = con.execute(
        "SELECT COUNT(*) - COUNT(DISTINCT news_id) FROM news_articles"
    ).fetchone()[0]
    print(f"Duplicate PK:  {dup}")

    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true",
                    help="drop news_articles table first")
    args = ap.parse_args()
    build(rebuild=args.rebuild)
