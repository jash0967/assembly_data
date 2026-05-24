"""Phase 6: post-collection drift detection.

Asserts the post-Phase-2 invariants on every ApiSpec table plus the
Phase 3/4 schemas. Intended to be run after `python download_all.py`
or any backfill — exits with code 1 on failure.

Usage:
    python validate_collection.py
    python validate_collection.py --verbose
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- adds repo root + collect/ to sys.path

import argparse
import sys

import duckdb

import config

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def _columns(con, catalog: str, table_name: str) -> set[str]:
    return {
        r[0].lower() for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = ? AND table_name = ?",
            [catalog, table_name],
        ).fetchall()
    }


def _table_exists(con, catalog: str, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_catalog = ? AND table_name = ?",
        [catalog, name],
    ).fetchone() is not None


def _view_exists(con, catalog: str, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM information_schema.views "
        "WHERE table_catalog = ? AND table_name = ?",
        [catalog, name],
    ).fetchone() is not None


def _row_count(con, qualified: str) -> int:
    return con.execute(f'SELECT COUNT(*) FROM {qualified}').fetchone()[0]


def validate(con, raw_catalog: str, analysis_catalog: str, verbose: bool = False) -> list[str]:
    """Validate raw tables (collected via ApiSpec) and analysis tables/views.

    raw_catalog is the ATTACH alias for assembly_raw.duckdb (e.g. 'raw').
    analysis_catalog is the main DB (e.g. 'assembly_analysis').
    """
    errors: list[str] = []

    # Raw side: 37 API tables + bill_text + document_text + speeches
    for spec in config.APIS:
        t = spec.table_name
        if not _table_exists(con, raw_catalog, t):
            if verbose:
                print(f"  -- {raw_catalog}.{t}: not collected yet")
            continue

        cols = _columns(con, raw_catalog, t)
        if "age" not in cols:
            errors.append(f"{raw_catalog}.{t}: missing `age` column")
            continue

        rows = _row_count(con, f'{raw_catalog}."{t}"')
        if rows == 0:
            if verbose:
                print(f"  -- {raw_catalog}.{t}: empty")
            continue

        nulls = con.execute(
            f'SELECT COUNT(*) FROM {raw_catalog}."{t}" WHERE age IS NULL'
        ).fetchone()[0]

        if spec.age_behavior == "current_only":
            distinct = con.execute(
                f'SELECT COUNT(DISTINCT age) FROM {raw_catalog}."{t}" WHERE age IS NOT NULL'
            ).fetchone()[0]
            if distinct != 1:
                errors.append(
                    f"{raw_catalog}.{t}: current_only expects exactly 1 distinct age, got {distinct}"
                )
            if nulls > 0:
                errors.append(f"{raw_catalog}.{t}: current_only has {nulls} NULL ages")
        elif spec.age_behavior in ("per_age", "by_date", "by_bill_id"):
            if nulls > 0:
                errors.append(f"{raw_catalog}.{t}: {spec.age_behavior} has {nulls} NULL ages")

        if verbose:
            print(f"  ok {raw_catalog}.{t} [{spec.age_behavior}] rows={rows:,} null=0")

    # Raw side: extracted-text tables
    for required in ("bill_text", "document_text", "speeches"):
        if not _table_exists(con, raw_catalog, required):
            errors.append(f"missing raw schema: {raw_catalog}.{required}")

    # Analysis side: classification / filter / prompt_versions / speech_issues
    for required in ("bill_classifications", "bill_ai_filter", "prompt_versions",
                     "speech_issues"):
        if not _table_exists(con, analysis_catalog, required):
            errors.append(f"missing analysis schema: {analysis_catalog}.{required}")

    # Analysis side: views
    for required in ("v_bill_classifications_current", "v_kr_bills_analysis"):
        if not _view_exists(con, analysis_catalog, required):
            errors.append(f"missing analysis view: {analysis_catalog}.{required}")

    # speeches.dae_num format invariant (no bare numeric)
    if _table_exists(con, raw_catalog, "speeches"):
        bare = con.execute(
            f"SELECT COUNT(*) FROM {raw_catalog}.speeches WHERE dae_num ~ '^[0-9]+$'"
        ).fetchone()[0]
        if bare > 0:
            errors.append(f"{raw_catalog}.speeches.dae_num: {bare} bare-numeric values (run backfill_ages)")

    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--raw-db", default=config.RAW_DB_PATH,
                        help="path to assembly_raw.duckdb")
    parser.add_argument("--analysis-db", default=config.ANALYSIS_DB_PATH,
                        help="path to assembly_analysis.duckdb")
    args = parser.parse_args()

    con = duckdb.connect(args.analysis_db, read_only=True)
    try:
        con.execute(f"ATTACH '{args.raw_db}' AS raw (READ_ONLY)")
        # DB filename without extension is the catalog name
        analysis_catalog = "assembly_analysis"
        errors = validate(con, raw_catalog="raw",
                          analysis_catalog=analysis_catalog,
                          verbose=args.verbose)
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
