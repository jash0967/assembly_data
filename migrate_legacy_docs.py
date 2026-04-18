"""Phase 1: seed the document_text table from legacy on-disk JSONs.

Covers:
- data/txt/*/research/*.json        → source='research'         (소규모연구용역, nfvmtaqoaldzhobsw)
- data/txt/*/report/*.json          → source='report'           (연구단체 활동보고서 — 대응 API 없음, seed만)
- data/txt/*/conf/*.json            → source='minutes_plenary'  (본회의 회의록, nzbyfwhwaoanttzje)
- data/minutes_txt/*/본회의/*.json    → source='minutes_plenary'
- data/minutes_txt/*/소위원회/*.json   → source='minutes_subcommittee' (vconfsubcconflist)

Idempotent. ON CONFLICT DO UPDATE keeps the version with longer full_text
when duplicates exist (e.g., minutes_txt and txt/conf both have 22대 본회의).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb

import config
from age_utils import parse_eraco

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

ROOT = Path(__file__).parent

DDL = """
CREATE TABLE IF NOT EXISTS document_text (
    doc_id             VARCHAR NOT NULL,
    source             VARCHAR NOT NULL,
    source_table       VARCHAR,
    age                INTEGER,
    title              VARCHAR,
    author             VARCHAR,
    doc_date           VARCHAR,
    url                VARCHAR,
    file_format        VARCHAR,
    file_path          VARCHAR,
    full_text          TEXT,
    text_length        INTEGER,
    status             VARCHAR NOT NULL,
    error_message      TEXT,
    extractor_version  VARCHAR,
    fetched_at         TIMESTAMP,
    extracted_at       TIMESTAMP,
    PRIMARY KEY (doc_id, source)
);
CREATE INDEX IF NOT EXISTS idx_document_text_age    ON document_text(age);
CREATE INDEX IF NOT EXISTS idx_document_text_source ON document_text(source);
CREATE INDEX IF NOT EXISTS idx_document_text_status ON document_text(status);
"""

# ON CONFLICT: prefer the row with the longer full_text (so a more complete
# legacy extraction beats a truncated one).
UPSERT = """
INSERT INTO document_text
    (doc_id, source, source_table, age, title, author, doc_date,
     url, file_format, file_path, full_text, text_length,
     status, error_message, extractor_version, fetched_at, extracted_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (doc_id, source) DO UPDATE SET
    full_text   = CASE WHEN EXCLUDED.text_length > COALESCE(document_text.text_length, 0)
                       THEN EXCLUDED.full_text ELSE document_text.full_text END,
    text_length = CASE WHEN EXCLUDED.text_length > COALESCE(document_text.text_length, 0)
                       THEN EXCLUDED.text_length ELSE document_text.text_length END,
    title       = COALESCE(document_text.title,       EXCLUDED.title),
    author      = COALESCE(document_text.author,      EXCLUDED.author),
    doc_date    = COALESCE(document_text.doc_date,    EXCLUDED.doc_date),
    age         = COALESCE(document_text.age,         EXCLUDED.age),
    status      = 'extracted_ok',
    extracted_at= COALESCE(document_text.extracted_at, EXCLUDED.extracted_at);
"""


def migrate_research(con) -> int:
    """data/txt/*/research/ → source='research'.

    Legacy filenames are {FILE_ID}.json or {FILE_ID}_{title}.json; the two
    variants duplicate content. The upsert handles the dup by keeping one row.
    """
    n = 0
    for age_dir in sorted(ROOT.glob("data/txt/*대")):
        rdir = age_dir / "research"
        if not rdir.is_dir():
            continue
        for fp in sorted(rdir.glob("*.json")):
            stem = fp.stem
            m = re.match(r"^(\d+)", stem)
            if not m:
                continue
            file_id = m.group(1)
            d = json.loads(fp.read_text(encoding="utf-8"))
            con.execute(UPSERT, [
                file_id, "research", "nfvmtaqoaldzhobsw",
                d.get("age"), d.get("title"), d.get("member"),
                str(d.get("date") or ""), None, "pdf", str(fp),
                d.get("full_text"), d.get("full_text_length"),
                "extracted_ok", None, "legacy-unknown",
                None, datetime.now(),
            ])
            n += 1
    return n


def migrate_report(con) -> int:
    """data/txt/*/report/ → source='report'.

    These don't map to a specific API row (연구단체 활동계획서 is not directly
    surfaced by any of the 37 APIs). We keep them as seed-only; the table
    record is information-only.
    """
    n = 0
    for age_dir in sorted(ROOT.glob("data/txt/*대")):
        rdir = age_dir / "report"
        if not rdir.is_dir():
            continue
        for fp in sorted(rdir.glob("*.json")):
            d = json.loads(fp.read_text(encoding="utf-8"))
            doc_id = str(d.get("id") or fp.stem)
            con.execute(UPSERT, [
                doc_id, "report", None,
                d.get("age"), d.get("title"), d.get("group_name"),
                str(d.get("date") or ""), None, "pdf", str(fp),
                d.get("full_text"), d.get("full_text_length"),
                "extracted_ok", None, "legacy-unknown",
                None, datetime.now(),
            ])
            n += 1
    return n


def _minutes_upsert(con, fp: Path, source: str, source_table: str) -> bool:
    d = json.loads(fp.read_text(encoding="utf-8"))
    conf_id = d.get("conf_id") or d.get("id")
    if not conf_id:
        return False
    age_val = parse_eraco(d.get("dae_num"))
    if age_val is None and "age" in d:
        try:
            age_val = int(d["age"])
        except Exception:
            age_val = None
    full = d.get("full_text") or ""
    con.execute(UPSERT, [
        str(conf_id), source, source_table,
        age_val, d.get("title") or d.get("source"),
        None, str(d.get("conf_date") or d.get("date") or ""),
        None, "pdf", str(fp),
        full, len(full) if full else 0,
        "extracted_ok", None, "legacy-unknown",
        None, datetime.now(),
    ])
    return True


def migrate_minutes_txt(con) -> int:
    """data/minutes_txt/{age}대/{본회의|소위원회}/*.json."""
    n = 0
    root = ROOT / "data" / "minutes_txt"
    if not root.is_dir():
        return 0
    for age_dir in sorted(root.iterdir()):
        if not age_dir.is_dir():
            continue
        for sub_dir in age_dir.iterdir():
            if not sub_dir.is_dir():
                continue
            if sub_dir.name == "본회의":
                source, src_table = "minutes_plenary", "nzbyfwhwaoanttzje"
            elif sub_dir.name == "소위원회":
                source, src_table = "minutes_subcommittee", "vconfsubcconflist"
            else:
                continue
            for fp in sorted(sub_dir.glob("*.json")):
                if _minutes_upsert(con, fp, source, src_table):
                    n += 1
    return n


def migrate_txt_conf(con) -> int:
    """data/txt/*/conf/ → source='minutes_plenary'."""
    n = 0
    for age_dir in sorted(ROOT.glob("data/txt/*대")):
        cdir = age_dir / "conf"
        if not cdir.is_dir():
            continue
        for fp in sorted(cdir.glob("*.json")):
            if _minutes_upsert(con, fp, "minutes_plenary", "nzbyfwhwaoanttzje"):
                n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=["research", "report", "minutes"],
                        help="restrict to one source")
    args = parser.parse_args()

    con = duckdb.connect(config.DB_PATH)
    log.info("opened %s", config.DB_PATH)
    con.execute(DDL)

    started = datetime.now()
    totals = {}
    if args.only in (None, "research"):
        totals["research"] = migrate_research(con)
        log.info("  research: %d", totals["research"])
    if args.only in (None, "report"):
        totals["report"] = migrate_report(con)
        log.info("  report: %d", totals["report"])
    if args.only in (None, "minutes"):
        totals["minutes_txt"] = migrate_minutes_txt(con)
        log.info("  minutes_txt: %d", totals["minutes_txt"])
        totals["txt_conf"] = migrate_txt_conf(con)
        log.info("  txt_conf: %d", totals["txt_conf"])

    log.info("done in %.1fs", (datetime.now() - started).total_seconds())

    print()
    by_source = con.execute(
        "SELECT source, status, COUNT(*) FROM document_text "
        "GROUP BY source, status ORDER BY source, status"
    ).fetchall()
    print("document_text by source/status:")
    for s, st, n in by_source:
        print(f"  {s:22s} {st:16s} {n:>6,}")

    by_age = con.execute(
        "SELECT source, age, COUNT(*) FROM document_text "
        "WHERE age IS NOT NULL GROUP BY source, age ORDER BY source, age"
    ).fetchall()
    print("\ndocument_text coverage by source/age:")
    for s, a, n in by_age:
        print(f"  {s:22s} age {a}: {n:>6,}")

    con.close()


if __name__ == "__main__":
    main()
