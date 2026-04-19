"""Phase 6: post-collection drift detection.

Asserts the post-Phase-2 invariants on every ApiSpec table plus the
Phase 3/4 schemas. Intended to be run after `python download_all.py`
or any backfill — exits with code 1 on failure.

Usage:
    python validate_collection.py
    python validate_collection.py --verbose
"""

from __future__ import annotations

import argparse
import sys

import duckdb

import config

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def _columns(con, table_name: str) -> set[str]:
    return {
        r[0].lower() for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table_name],
        ).fetchall()
    }


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [name],
    ).fetchone() is not None


def _row_count(con, name: str) -> int:
    return con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]


def validate(con, verbose: bool = False) -> list[str]:
    errors: list[str] = []

    for spec in config.APIS:
        t = spec.table_name
        if not _table_exists(con, t):
            if verbose:
                print(f"  -- {t}: not collected yet")
            continue

        cols = _columns(con, t)
        if "age" not in cols:
            errors.append(f"{t}: missing `age` column")
            continue

        rows = _row_count(con, t)
        if rows == 0:
            if verbose:
                print(f"  -- {t}: empty")
            continue

        nulls = con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE age IS NULL').fetchone()[0]

        if spec.age_behavior == "current_only":
            distinct = con.execute(
                f'SELECT COUNT(DISTINCT age) FROM "{t}" WHERE age IS NOT NULL'
            ).fetchone()[0]
            if distinct != 1:
                errors.append(
                    f"{t}: current_only expects exactly 1 distinct age, got {distinct}"
                )
            if nulls > 0:
                errors.append(f"{t}: current_only has {nulls} NULL ages")
        elif spec.age_behavior in ("per_age", "by_date", "by_bill_id"):
            if nulls > 0:
                errors.append(f"{t}: {spec.age_behavior} has {nulls} NULL ages")

        if verbose:
            print(f"  ok {t} [{spec.age_behavior}] rows={rows:,} null=0")

    # Phase 3/4 schemas + doc pipeline (Phase 8)
    for required in ("bill_text", "bill_classifications", "bill_ai_filter",
                     "prompt_versions", "document_text"):
        if not _table_exists(con, required):
            errors.append(f"missing schema: {required}")

    if _table_exists(con, "v_bill_classifications_current") or True:
        # view may exist as VIEW, not TABLE
        v_exists = con.execute(
            "SELECT 1 FROM information_schema.views WHERE table_name = ?",
            ["v_bill_classifications_current"],
        ).fetchone()
        if not v_exists:
            errors.append("missing view: v_bill_classifications_current")

    v_exists = con.execute(
        "SELECT 1 FROM information_schema.views WHERE table_name = ?",
        ["v_kr_bills_analysis"],
    ).fetchone()
    if not v_exists:
        errors.append("missing view: v_kr_bills_analysis")

    # speeches.dae_num format invariant (no bare numeric)
    if _table_exists(con, "speeches"):
        bare = con.execute(
            "SELECT COUNT(*) FROM speeches WHERE dae_num ~ '^[0-9]+$'"
        ).fetchone()[0]
        if bare > 0:
            errors.append(f"speeches.dae_num: {bare} bare-numeric values (run backfill_ages)")

    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--db", default=config.DB_PATH)
    args = parser.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    try:
        errors = validate(con, verbose=args.verbose)
    finally:
        con.close()

    if errors:
        print(f"\nFAILED ({len(errors)} error{'s' if len(errors) != 1 else ''}):")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)

    print("\nOK — all invariants satisfied")


if __name__ == "__main__":
    main()
