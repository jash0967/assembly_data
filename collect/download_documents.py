"""Phase 2-4: download PDF/HWP/HWPX from URLs in API tables → document_text.

Handles:
- ncrwiahparxrpodcv  (연구단체 연구활동 보고서) — PDF_DOWN_URL, usually HWP/HWPX
- nzbyfwhwaoanttzje  (본회의 회의록)             — PDF_LINK_URL
- ncwgseseafwbuheph  (위원회 회의록)             — PDF_LINK_URL  [BIG]
- ngytonzwavydlbbha  (전원위 회의록)             — PDF_LINK_URL
- vconfsubcconflist  (소위원회 회의록)            — DOWN_URL

Not handled (no usable URL):
- nfvmtaqoaldzhobsw  (소규모연구용역) — legacy seed only; new rows stay no_url

Usage:
    python download_documents.py --source research
    python download_documents.py --source minutes_plenary --age 22
    python download_documents.py --source minutes_committee --limit 100
    python download_documents.py --all --dry-run

File format is detected from magic bytes, not URL. Raw files are kept under
data/bills_kr/docs/{source}/{doc_id}.{ext}. Text is extracted and stored in
document_text.full_text. DB writes go through a single writer thread + lock.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import duckdb
import fitz
import requests

import _bootstrap  # noqa: F401  -- adds repo root + collect/ to sys.path

import config
from age_utils import parse_eraco

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent
DOCS_DIR = Path(config.BILLS_KR_DIR) / "docs"
HWP5TXT = str(ROOT.parent / "venv" / "Scripts" / "hwp5txt.exe")

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
RETRY = 3
RETRY_DELAY = 2


# =============================================================================
# Source registry — how to pull rows out of each API table
# =============================================================================

@dataclass(frozen=True)
class Source:
    key: str                 # 'research' | 'minutes_plenary' | ...
    source_table: str        # DB table containing URL
    id_col: str              # column with unique document id
    url_col: str             # column with direct download URL
    title_col: str | None    # column with title
    date_col: str | None     # column with date (or year)
    age_col: str | None = None  # column with age (INTEGER or 16)
    dae_num_col: str | None = None  # or dae_num string (has parse_eraco)
    extra_filter: str = ""   # additional WHERE clause


SOURCES = {
    # ncrwiahparxrpodcv = 연구단체 연구활동 보고서. doc_id = REPORT_TITLE+YEAR+RE_NAME
    # (the table has no single natural PK).
    "report": Source(
        key="report",
        source_table="ncrwiahparxrpodcv",
        id_col="\"REPORT_TITLE\" || '|' || \"YEAR\" || '|' || \"RE_NAME\"",
        url_col="\"PDF_DOWN_URL\"",
        title_col="\"REPORT_TITLE\"",
        date_col="\"YEAR\"",
        age_col="age",
        extra_filter='AND "PDF_DOWN_URL" IS NOT NULL',
    ),
    "minutes_plenary": Source(
        key="minutes_plenary",
        source_table="nzbyfwhwaoanttzje",
        id_col="\"CONF_ID\"",
        url_col="\"PDF_LINK_URL\"",
        title_col="\"TITLE\"",
        date_col="\"CONF_DATE\"",
        age_col="age",
        extra_filter='AND "PDF_LINK_URL" IS NOT NULL',
    ),
    "minutes_committee": Source(
        key="minutes_committee",
        source_table="ncwgseseafwbuheph",
        id_col="\"CONF_ID\"",
        url_col="\"PDF_LINK_URL\"",
        title_col="\"COMM_NAME\" || ' — ' || \"TITLE\"",
        date_col="\"CONF_DATE\"",
        age_col="age",
        extra_filter='AND "PDF_LINK_URL" IS NOT NULL',
    ),
    "minutes_committee_of_whole": Source(
        key="minutes_committee_of_whole",
        source_table="ngytonzwavydlbbha",
        id_col="\"CONF_ID\"",
        url_col="\"PDF_LINK_URL\"",
        title_col="\"TITLE\"",
        date_col="\"CONF_DATE\"",
        age_col="age",
        extra_filter='AND "PDF_LINK_URL" IS NOT NULL',
    ),
    "minutes_subcommittee": Source(
        key="minutes_subcommittee",
        source_table="vconfsubcconflist",
        id_col="\"CONF_ID\"",
        url_col="\"DOWN_URL\"",
        title_col="\"CMIT_NM\"",
        date_col="\"CONF_DT\"",
        age_col="age",
        extra_filter='AND "DOWN_URL" IS NOT NULL',
    ),
}


# =============================================================================
# DB
# =============================================================================

_db_lock = threading.Lock()
_db_con: duckdb.DuckDBPyConnection | None = None

UPSERT = """
INSERT INTO document_text
    (doc_id, source, source_table, age, title, author, doc_date,
     url, file_format, file_path, full_text, text_length,
     status, error_message, extractor_version, fetched_at, extracted_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (doc_id, source) DO UPDATE SET
    age          = COALESCE(EXCLUDED.age,          document_text.age),
    title        = COALESCE(EXCLUDED.title,        document_text.title),
    doc_date     = COALESCE(EXCLUDED.doc_date,     document_text.doc_date),
    url          = EXCLUDED.url,
    file_format  = EXCLUDED.file_format,
    file_path    = EXCLUDED.file_path,
    full_text    = CASE WHEN EXCLUDED.status = 'extracted_ok'
                        THEN EXCLUDED.full_text ELSE document_text.full_text END,
    text_length  = CASE WHEN EXCLUDED.status = 'extracted_ok'
                        THEN EXCLUDED.text_length ELSE document_text.text_length END,
    status       = EXCLUDED.status,
    error_message= EXCLUDED.error_message,
    extractor_version = EXCLUDED.extractor_version,
    fetched_at   = COALESCE(EXCLUDED.fetched_at,   document_text.fetched_at),
    extracted_at = COALESCE(EXCLUDED.extracted_at, document_text.extracted_at);
"""


def upsert_doc(doc_id: str, source: str, source_table: str, *,
               age=None, title=None, author=None, doc_date=None,
               url=None, file_format=None, file_path=None,
               full_text=None, status="unknown", error_message=None,
               extractor_version=None, fetched_at=None, extracted_at=None):
    text_len = len(full_text) if full_text else None
    with _db_lock:
        _db_con.execute(UPSERT, [
            str(doc_id), source, source_table, age, title, author, doc_date,
            url, file_format, file_path, full_text, text_len,
            status, error_message, extractor_version, fetched_at, extracted_at,
        ])


def _reader_con():
    """Return the shared write connection (DuckDB forbids mixing RO+RW in same process)."""
    return _db_con if _db_con is not None else duckdb.connect(config.RAW_DB_PATH, read_only=True)


def load_done_ids(source: str) -> set[str]:
    con = _reader_con()
    rows = con.execute(
        "SELECT doc_id FROM document_text WHERE source = ? "
        "AND status IN ('extracted_ok', 'format_unsupported', 'url_404', 'no_url')",
        [source],
    ).fetchall()
    return {r[0] for r in rows}


def load_rows(src: Source, age: int | None, limit: int | None) -> list[dict]:
    cols = [src.id_col, src.url_col]
    if src.title_col: cols.append(src.title_col)
    if src.date_col:  cols.append(src.date_col)
    if src.age_col:   cols.append(src.age_col)

    where = ["1=1"]
    params: list = []
    if age is not None and src.age_col:
        where.append(f"{src.age_col} = ?")
        params.append(age)
    if src.extra_filter:
        where.append(src.extra_filter[4:] if src.extra_filter.startswith("AND ") else src.extra_filter)

    sql = f'SELECT DISTINCT {", ".join(cols)} FROM "{src.source_table}" WHERE {" AND ".join(where)}'
    if limit:
        sql += f" LIMIT {int(limit)}"

    con = _reader_con()
    rows = con.execute(sql, params).fetchall()

    out = []
    for row in rows:
        rec = {"doc_id": row[0], "url": row[1]}
        i = 2
        if src.title_col:
            rec["title"] = row[i]; i += 1
        if src.date_col:
            rec["doc_date"] = row[i]; i += 1
        if src.age_col:
            rec["age"] = row[i]; i += 1
        out.append(rec)
    return out


# =============================================================================
# Format detection + extraction
# =============================================================================

def detect_format(raw: bytes) -> str:
    if raw.startswith(b"%PDF"):
        return "pdf"
    # HWP 5.x = OLE2 compound document
    if raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # Could be .doc too; safer to still try hwp5txt first
        return "hwp"
    # HWPX = zip with specific mimetype
    if raw.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if "mimetype" in z.namelist():
                    mt = z.read("mimetype").decode("ascii", errors="ignore").strip()
                    if "hwpx" in mt:
                        return "hwpx"
                # even without mimetype, Contents/section0.xml is a good hint
                if any(n.startswith("Contents/section") for n in z.namelist()):
                    return "hwpx"
        except Exception:
            pass
        return "zip"
    return "unknown"


def extract_pdf(path: str) -> str:
    doc = fitz.open(path)
    try:
        return "".join(page.get_text() for page in doc)
    finally:
        doc.close()


def extract_hwp(path: str) -> str:
    import subprocess
    r = subprocess.run([HWP5TXT, path], capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"hwp5txt rc={r.returncode}: {r.stderr[:200].decode(errors='replace')}")
    return r.stdout.decode("utf-8", errors="replace")


def extract_hwpx(path: str) -> str:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as z:
        section_names = sorted(n for n in z.namelist()
                               if n.startswith("Contents/section") and n.endswith(".xml"))
        for name in section_names:
            try:
                tree = ET.parse(z.open(name))
                for t in tree.iter():
                    if t.text:
                        chunks.append(t.text)
            except ET.ParseError:
                continue
    return "\n".join(chunks)


# =============================================================================
# Download + extract for a single row
# =============================================================================

def safe_id(doc_id: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(doc_id))


def process_one(rec: dict, src: Source) -> None:
    doc_id = str(rec["doc_id"])
    url = rec.get("url") or ""
    common = dict(
        doc_id=doc_id, source=src.key, source_table=src.source_table,
        age=rec.get("age"),
        title=rec.get("title"),
        doc_date=str(rec.get("doc_date") or "") if rec.get("doc_date") else None,
        url=url,
    )

    if not url:
        upsert_doc(**common, status="no_url")
        return

    # Download
    raw: bytes | None = None
    last_err = None
    for attempt in range(RETRY):
        try:
            r = requests.get(url, headers=UA, timeout=60, allow_redirects=True)
            if r.status_code in (404, 410):
                upsert_doc(**common, status="url_404", fetched_at=datetime.now())
                return
            r.raise_for_status()
            raw = r.content
            break
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = str(e)
            if attempt < RETRY - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
        except Exception as e:
            last_err = str(e)
            break

    if raw is None:
        upsert_doc(**common, status="url_error",
                   error_message=(last_err or "")[:500])
        return

    if len(raw) < 500:
        upsert_doc(**common, status="url_error",
                   error_message=f"file too small ({len(raw)} bytes)")
        return

    fmt = detect_format(raw)
    if fmt not in ("pdf", "hwp", "hwpx"):
        upsert_doc(**common, file_format=fmt, status="format_unsupported",
                   error_message=f"detected: {fmt}", fetched_at=datetime.now())
        return

    # Save raw file
    DOCS_DIR.joinpath(src.key).mkdir(parents=True, exist_ok=True)
    fpath = DOCS_DIR / src.key / f"{safe_id(doc_id)}.{fmt}"
    fpath.write_bytes(raw)

    # Extract
    try:
        if fmt == "pdf":
            text = extract_pdf(str(fpath))
            extractor_version = "fitz-1.0"
        elif fmt == "hwp":
            text = extract_hwp(str(fpath))
            extractor_version = "hwp5txt-0.1"
        else:
            text = extract_hwpx(str(fpath))
            extractor_version = "hwpx-xml-0.1"
    except Exception as e:
        upsert_doc(**common, file_format=fmt, file_path=str(fpath),
                   status="extract_failed", error_message=str(e)[:500],
                   fetched_at=datetime.now())
        return

    upsert_doc(**common, file_format=fmt, file_path=str(fpath),
               full_text=text, status="extracted_ok",
               extractor_version=extractor_version,
               fetched_at=datetime.now(), extracted_at=datetime.now())


# =============================================================================
# Main
# =============================================================================

def _init_db() -> None:
    global _db_con
    _db_con = duckdb.connect(config.RAW_DB_PATH)
    _db_con.execute("""
        CREATE TABLE IF NOT EXISTS document_text (
            doc_id VARCHAR NOT NULL, source VARCHAR NOT NULL,
            source_table VARCHAR, age INTEGER, title VARCHAR, author VARCHAR,
            doc_date VARCHAR, url VARCHAR, file_format VARCHAR, file_path VARCHAR,
            full_text TEXT, text_length INTEGER,
            status VARCHAR NOT NULL, error_message TEXT, extractor_version VARCHAR,
            fetched_at TIMESTAMP, extracted_at TIMESTAMP,
            PRIMARY KEY (doc_id, source)
        );
    """)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=list(SOURCES), help="one source")
    ap.add_argument("--all", action="store_true", help="all sources")
    ap.add_argument("--age", type=int, help="restrict to one age")
    ap.add_argument("--limit", type=int, default=0, help="max rows to process")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true",
                    help="print row count per source, no downloads")
    args = ap.parse_args()

    if not args.source and not args.all:
        ap.error("one of --source or --all required")

    target_keys = list(SOURCES) if args.all else [args.source]

    if args.dry_run:
        con = duckdb.connect(config.RAW_DB_PATH, read_only=True)
        try:
            for key in target_keys:
                src = SOURCES[key]
                n = con.execute(
                    f'SELECT COUNT(DISTINCT {src.id_col}) FROM "{src.source_table}" '
                    f'WHERE {src.url_col} IS NOT NULL'
                    + (f' AND {src.age_col} = {int(args.age)}' if args.age and src.age_col else "")
                ).fetchone()[0]
                print(f"  {key:30s} {src.source_table:22s} {n:>8,} rows with URL")
        finally:
            con.close()
        return

    _init_db()
    try:
        for key in target_keys:
            src = SOURCES[key]
            print(f"\n=== {key} ({src.source_table}) ===")
            rows = load_rows(src, age=args.age, limit=args.limit or None)
            done = load_done_ids(src.key)
            todo = [r for r in rows if str(r["doc_id"]) not in done]
            print(f"  total URLs: {len(rows)}  already done: {len(done)}  todo: {len(todo)}")

            counts = {"extracted_ok": 0, "url_404": 0, "url_error": 0,
                      "format_unsupported": 0, "extract_failed": 0, "no_url": 0}
            t0 = datetime.now()
            progress_lock = threading.Lock()
            completed = 0

            def _after(rec_in):
                nonlocal completed
                # Status read back
                with progress_lock:
                    completed += 1
                    if completed % 25 == 0 or completed == len(todo):
                        elapsed = (datetime.now() - t0).total_seconds()
                        speed = completed / elapsed if elapsed else 0
                        eta = int((len(todo) - completed) / speed) if speed else 0
                        eta_s = f"{eta // 60}분 {eta % 60}초" if eta >= 60 else f"{eta}초"
                        print(f"  [{completed}/{len(todo)}] {speed:.1f}/s  ETA {eta_s}")

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs = {pool.submit(process_one, r, src): r for r in todo}
                for fut in as_completed(futs):
                    try:
                        fut.result()
                    except Exception as e:
                        log.error("process_one failure: %s", e)
                    _after(None)

            # summary
            summary = _db_con.execute(
                "SELECT status, COUNT(*) FROM document_text WHERE source = ? "
                "GROUP BY status ORDER BY status", [key],
            ).fetchall()
            print(f"  -- status counts for {key} --")
            for s, n in summary:
                print(f"    {s:22s} {n:>6,}")
    finally:
        if _db_con is not None:
            _db_con.close()


if __name__ == "__main__":
    import db_audit
    with db_audit.audit_run(__file__, config.RAW_DB_PATH, argv=sys.argv[1:]):
        main()
