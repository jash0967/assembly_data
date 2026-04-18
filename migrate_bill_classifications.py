"""Phase 4: migrate classification JSONs into DuckDB.

Inserts:
- prompt_versions       — registry of SYSTEM_PROMPT releases
- bill_classifications  — 10-attribute results (KR/US/EU)
- bill_ai_filter        — KR Stage-2 GPT filter (core/adjacent/unrelated)

Idempotent. Uses ON CONFLICT to overwrite. PROMPT_VERSION below is the version
tag for the current results — bump when SYSTEM_PROMPT or AI_FILTER_PROMPT changes.

Usage:
    python migrate_bill_classifications.py
    python migrate_bill_classifications.py --version v3_en_20260601  # next bump
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

ROOT = Path(__file__).parent
PROCESSED = ROOT / "data" / "processed"
CACHE = ROOT / "cache"

DEFAULT_VERSION = "v2_en_20260418"
DEFAULT_DESCRIPTION = "10-attribute SYSTEM_PROMPT (English labels), released 2026-04-18"

CLASSIFICATION_TARGETS = (
    "kr_19", "kr_20", "kr_21", "kr_22",
    "us_118", "us_119",
    "eu_act", "eu_amendments",
)
KR_AGES = (19, 20, 21, 22)

DDL = """
CREATE TABLE IF NOT EXISTS prompt_versions (
    version     VARCHAR PRIMARY KEY,
    description TEXT,
    released_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bill_classifications (
    bill_id        VARCHAR NOT NULL,
    source         VARCHAR NOT NULL,
    prompt_version VARCHAR NOT NULL,
    primary_attr   VARCHAR,
    secondary_attr VARCHAR,
    tertiary_attr  VARCHAR,
    title          VARCHAR,
    classified_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bill_id, source, prompt_version)
);

CREATE TABLE IF NOT EXISTS bill_ai_filter (
    bill_id        VARCHAR PRIMARY KEY,
    age            INTEGER NOT NULL,
    classification VARCHAR,
    is_ai_bill     BOOLEAN,
    gpt_reason     TEXT,
    ai_provisions  TEXT,
    filtered_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Latest-version view: every classify_bills consumer queries through this.
VIEW_DDL = """
CREATE OR REPLACE VIEW v_bill_classifications_current AS
SELECT bc.*
FROM bill_classifications bc
JOIN (
    SELECT version FROM prompt_versions ORDER BY released_at DESC LIMIT 1
) cur ON bc.prompt_version = cur.version;
"""

UPSERT_PROMPT = """
INSERT INTO prompt_versions (version, description) VALUES (?, ?)
ON CONFLICT (version) DO UPDATE SET description = EXCLUDED.description;
"""

UPSERT_CLASSIFICATION = """
INSERT INTO bill_classifications
    (bill_id, source, prompt_version, primary_attr, secondary_attr, tertiary_attr, title)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (bill_id, source, prompt_version) DO UPDATE SET
    primary_attr   = EXCLUDED.primary_attr,
    secondary_attr = EXCLUDED.secondary_attr,
    tertiary_attr  = EXCLUDED.tertiary_attr,
    title          = EXCLUDED.title,
    classified_at  = now();
"""

UPSERT_AI_FILTER = """
INSERT INTO bill_ai_filter
    (bill_id, age, classification, is_ai_bill, gpt_reason, ai_provisions)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (bill_id) DO UPDATE SET
    age            = EXCLUDED.age,
    classification = EXCLUDED.classification,
    is_ai_bill     = EXCLUDED.is_ai_bill,
    gpt_reason     = EXCLUDED.gpt_reason,
    ai_provisions  = EXCLUDED.ai_provisions,
    filtered_at    = now();
"""


def migrate_classifications(con, version: str) -> int:
    inserted = 0
    for target in CLASSIFICATION_TARGETS:
        fp = PROCESSED / f"bills_classified_{target}.json"
        if not fp.exists():
            log.info("  skip %s (file missing)", target)
            continue
        records = json.loads(fp.read_text(encoding="utf-8"))
        ok = 0
        for r in records:
            if r.get("primary") == "error":
                continue
            con.execute(UPSERT_CLASSIFICATION, [
                r["id"],
                target,
                version,
                r.get("primary"),
                r.get("secondary"),
                r.get("tertiary"),
                r.get("title"),
            ])
            ok += 1
        log.info("  %s: %d rows (skipped %d errors)", target, ok, len(records) - ok)
        inserted += ok
    return inserted


def migrate_ai_filter(con) -> int:
    inserted = 0
    for age in KR_AGES:
        fp = CACHE / f"kr_{age}_ai_filtered.json"
        if not fp.exists():
            log.info("  skip kr_%d (cache missing)", age)
            continue
        records = json.loads(fp.read_text(encoding="utf-8"))
        ok = 0
        for r in records:
            if r.get("classification") is None:
                continue  # GPT failures
            con.execute(UPSERT_AI_FILTER, [
                r["id"],
                age,
                r.get("classification"),
                bool(r.get("is_ai_bill", False)),
                r.get("gpt_reason"),
                r.get("ai_provisions"),
            ])
            ok += 1
        log.info("  kr_%d: %d rows (skipped %d nulls)", age, ok, len(records) - ok)
        inserted += ok
    return inserted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION)
    args = parser.parse_args()

    con = duckdb.connect(config.DB_PATH)
    log.info("opened %s", config.DB_PATH)
    con.execute(DDL)

    started = datetime.now()
    con.execute(UPSERT_PROMPT, [args.version, args.description])
    log.info("prompt_version registered: %s", args.version)

    log.info("migrate bill_classifications")
    n_cls = migrate_classifications(con, args.version)

    log.info("migrate bill_ai_filter")
    n_ai = migrate_ai_filter(con)

    con.execute(VIEW_DDL)
    log.info("created/updated v_bill_classifications_current view")

    log.info("done in %.1fs — classifications=%d, ai_filter=%d",
             (datetime.now() - started).total_seconds(), n_cls, n_ai)

    print()
    by_source = con.execute(
        "SELECT source, COUNT(*) FROM bill_classifications GROUP BY source ORDER BY source"
    ).fetchall()
    print("bill_classifications by source:")
    for s, n in by_source:
        print(f"  {s:20s} {n:>6d}")

    by_age_cls = con.execute(
        "SELECT age, COUNT(*) FROM bill_ai_filter GROUP BY classification, age "
        "ORDER BY age"
    ).fetchall()
    by_class = con.execute(
        "SELECT classification, COUNT(*) FROM bill_ai_filter GROUP BY classification ORDER BY classification"
    ).fetchall()
    print("\nbill_ai_filter by classification:")
    for c, n in by_class:
        print(f"  {c:12s} {n:>6d}")

    con.close()


if __name__ == "__main__":
    main()
