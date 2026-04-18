"""Phase 3: migrate data/bill_txt_*/*.json into DuckDB.bill_text.

Idempotent. Re-running with --force re-inserts (overwrites) existing rows.
PDF files stay on disk; bill_text.pdf_path points to them.

Usage:
    python migrate_bill_text.py            # all ages, skip existing
    python migrate_bill_text.py --force    # re-import all
    python migrate_bill_text.py --age 22   # one age only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import duckdb

import config

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
BATCH_SIZE = 500

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS bill_text (
    bill_id            VARCHAR PRIMARY KEY,
    age                INTEGER NOT NULL,
    reason_and_content TEXT,
    full_text          TEXT,
    pdf_path           VARCHAR,
    extractor_version  VARCHAR DEFAULT 'fitz-1.0',
    extracted_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

INSERT_SQL = """
INSERT INTO bill_text (bill_id, age, reason_and_content, full_text, pdf_path)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (bill_id) DO UPDATE SET
    age = EXCLUDED.age,
    reason_and_content = EXCLUDED.reason_and_content,
    full_text = EXCLUDED.full_text,
    pdf_path = EXCLUDED.pdf_path,
    extracted_at = now();
"""


def migrate_age(con, age: int, existing: set[str], force: bool) -> tuple[int, int, int]:
    folder = DATA_DIR / f"bill_txt_{age}"
    pdf_folder = DATA_DIR / f"bill_pdf_{age}"
    if not folder.exists():
        log.warning("%s missing — skip", folder)
        return 0, 0, 0

    files = sorted(folder.glob("*.json"))
    log.info("age %d: %d files", age, len(files))

    inserted = 0
    skipped = 0
    failed = 0
    batch: list[tuple] = []

    for fp in files:
        try:
            rec = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("  parse fail %s: %s", fp.name, e)
            failed += 1
            continue

        bid = rec.get("bill_id")
        if not bid:
            failed += 1
            continue

        if not force and bid in existing:
            skipped += 1
            continue

        pdf_path = pdf_folder / f"{fp.stem}.pdf"
        pdf_str = str(pdf_path) if pdf_path.exists() else None

        batch.append((
            bid,
            age,
            rec.get("reason_and_content"),
            rec.get("full_text"),
            pdf_str,
        ))

        if len(batch) >= BATCH_SIZE:
            con.executemany(INSERT_SQL, batch)
            inserted += len(batch)
            batch.clear()
            if inserted % 5000 == 0:
                log.info("  age %d: %d inserted so far", age, inserted)

    if batch:
        con.executemany(INSERT_SQL, batch)
        inserted += len(batch)

    log.info("  age %d done: inserted=%d skipped=%d failed=%d",
             age, inserted, skipped, failed)
    return inserted, skipped, failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--age", type=int, choices=[19, 20, 21, 22],
                        help="single age only (default: all)")
    parser.add_argument("--force", action="store_true",
                        help="re-import (overwrite) existing rows")
    args = parser.parse_args()

    con = duckdb.connect(config.DB_PATH)
    log.info("opened %s", config.DB_PATH)
    con.execute(CREATE_SQL)

    existing = set()
    if not args.force:
        existing = {r[0] for r in con.execute("SELECT bill_id FROM bill_text").fetchall()}
        log.info("existing bill_text rows: %d", len(existing))

    started = datetime.now()
    ages = [args.age] if args.age else [19, 20, 21, 22]
    totals = [0, 0, 0]

    for age in ages:
        ins, skp, fld = migrate_age(con, age, existing, args.force)
        totals[0] += ins
        totals[1] += skp
        totals[2] += fld

    elapsed = (datetime.now() - started).total_seconds()
    log.info("done in %.1fs — inserted=%d skipped=%d failed=%d",
             elapsed, *totals)

    final = con.execute("SELECT COUNT(*) FROM bill_text").fetchone()[0]
    log.info("bill_text total rows: %d", final)
    by_age = con.execute(
        "SELECT age, COUNT(*) FROM bill_text GROUP BY age ORDER BY age"
    ).fetchall()
    for a, n in by_age:
        log.info("  age %d: %d", a, n)

    con.close()


if __name__ == "__main__":
    main()
