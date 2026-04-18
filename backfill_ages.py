"""Phase 2: backfill `age` column on existing rows.

One-shot, idempotent (only updates rows where age IS NULL).
Run AFTER Phase 1 deploy and BEFORE the next download_all run.

Pre-requisite: MCP server must NOT have an open connection to the DB
during this script's run, since DuckDB takes an exclusive write lock.

Usage:
    python backfill_ages.py            # backfill all + normalize speeches
    python backfill_ages.py --dry-run  # plan only, no writes
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

import duckdb

import config
from age_utils import derive_age, ERACO_SPECIAL, ERACO_NAMED_AGES

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [name],
    ).fetchone() is not None


def _columns(con, table_name: str) -> set[str]:
    return {
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            [table_name],
        ).fetchall()
    }


def _has_age_column(con, table_name: str) -> bool:
    """DuckDB compares identifiers case-insensitively, so an existing AGE
    column (e.g. legacy uppercase) counts as having age."""
    return "age" in {c.lower() for c in _columns(con, table_name)}


def _ensure_age_column(con, table_name: str, dry_run: bool) -> None:
    if not _has_age_column(con, table_name):
        sql = f'ALTER TABLE "{table_name}" ADD COLUMN age INTEGER'
        log.info("  + %s", sql)
        if not dry_run:
            con.execute(sql)


def _null_count(con, table_name: str) -> int:
    if not _has_age_column(con, table_name):
        return _row_count(con, table_name)  # column not added yet (dry-run)
    return con.execute(f'SELECT COUNT(*) FROM "{table_name}" WHERE age IS NULL').fetchone()[0]


def _row_count(con, table_name: str) -> int:
    return con.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]


def backfill_constant(con, table_name: str, value: int, dry_run: bool) -> int:
    _ensure_age_column(con, table_name, dry_run)
    if dry_run:
        return _null_count(con, table_name)
    before = _null_count(con, table_name)
    con.execute(f'UPDATE "{table_name}" SET age = ? WHERE age IS NULL', [int(value)])
    after = _null_count(con, table_name)
    return before - after


def backfill_param(con, table_name: str, field: str, dry_run: bool) -> int:
    _ensure_age_column(con, table_name, dry_run)
    if dry_run:
        return _null_count(con, table_name)
    pattern = f"__{field}_(-?[0-9]+)"
    before = _null_count(con, table_name)
    con.execute(
        f'UPDATE "{table_name}" SET age = '
        f'CAST(regexp_extract(_task_key, ?, 1) AS INTEGER) '
        f'WHERE age IS NULL AND regexp_extract(_task_key, ?, 1) <> \'\'',
        [pattern, pattern],
    )
    after = _null_count(con, table_name)
    return before - after


def backfill_column(con, table_name: str, col: str, dry_run: bool) -> int:
    _ensure_age_column(con, table_name, dry_run)
    if col not in _columns(con, table_name):
        log.warning("  %s: source column %s missing — skipping", table_name, col)
        return 0
    if dry_run:
        return _null_count(con, table_name)

    qcol = f'"{col}"'
    before = _null_count(con, table_name)

    if col == "ERACO":
        # standard "제N대"
        con.execute(
            f'UPDATE "{table_name}" SET age = '
            f"CAST(regexp_extract({qcol}, '^제([0-9]+)대$', 1) AS INTEGER) "
            f"WHERE age IS NULL AND regexp_extract({qcol}, '^제([0-9]+)대$', 1) <> ''"
        )
        # alternative "N대" or "N대 국회" (seen in nzivskufaliivfhpb)
        con.execute(
            f'UPDATE "{table_name}" SET age = '
            f"CAST(regexp_extract({qcol}, '^([0-9]+)대(\\s*국회)?$', 1) AS INTEGER) "
            f"WHERE age IS NULL AND regexp_extract({qcol}, '^([0-9]+)대(\\s*국회)?$', 1) <> ''"
        )
        # named ages (제헌 → 1)
        for label, age_val in ERACO_NAMED_AGES.items():
            con.execute(
                f'UPDATE "{table_name}" SET age = ? WHERE age IS NULL AND {qcol} = ?',
                [age_val, label],
            )
        # special bodies (negative ages so analysis filters via age >= 1)
        for label, neg_age in ERACO_SPECIAL.items():
            con.execute(
                f'UPDATE "{table_name}" SET age = ? WHERE age IS NULL AND {qcol} = ?',
                [neg_age, label],
            )
    elif col in ("UNIT_CD", "PROFILE_UNIT_CD"):
        # 100013-100099 → 13-99. Subtract 100000.
        con.execute(
            f'UPDATE "{table_name}" SET age = CAST({qcol} AS INTEGER) - 100000 '
            f"WHERE age IS NULL AND {qcol} ~ '^10[0-9]{{4}}$'"
        )
    elif col in ("ORD_NUM", "DIV"):
        con.execute(
            f'UPDATE "{table_name}" SET age = '
            f"CAST(regexp_extract({qcol}, '([0-9]+)대', 1) AS INTEGER) "
            f"WHERE age IS NULL AND regexp_extract({qcol}, '([0-9]+)대', 1) <> ''"
        )
    elif col in ("AGE", "DAE_NUM", "REGDAESU"):
        con.execute(
            f'UPDATE "{table_name}" SET age = TRY_CAST({qcol} AS INTEGER) '
            f"WHERE age IS NULL AND TRY_CAST({qcol} AS INTEGER) IS NOT NULL"
        )
    else:
        log.warning("  unknown column source %s for %s", col, table_name)

    after = _null_count(con, table_name)
    return before - after


def backfill_date(con, table_name: str, col: str, dry_run: bool) -> int:
    _ensure_age_column(con, table_name, dry_run)
    if col not in _columns(con, table_name):
        log.warning("  %s: date column %s missing — skipping", table_name, col)
        return 0
    if dry_run:
        return _null_count(con, table_name)

    qcol = f'"{col}"'
    before = _null_count(con, table_name)

    distinct = con.execute(
        f'SELECT DISTINCT {qcol} FROM "{table_name}" '
        f'WHERE age IS NULL AND {qcol} IS NOT NULL'
    ).fetchall()

    age_map: dict[str, int] = {}
    for (val,) in distinct:
        derived = derive_age(f"date:{col}", row={col: val})
        if derived is not None:
            age_map[val] = derived

    log.info("  %s: %d distinct date values → %d mapped", table_name,
             len(distinct), len(age_map))

    for val, age_val in age_map.items():
        con.execute(
            f'UPDATE "{table_name}" SET age = ? WHERE age IS NULL AND {qcol} = ?',
            [age_val, val],
        )

    after = _null_count(con, table_name)
    return before - after


def backfill_join_billinfodetail(con, dry_run: bool) -> int:
    """billinfodetail.age = billrcp.age via BILL_ID."""
    _ensure_age_column(con, "billinfodetail", dry_run)
    if dry_run:
        return _null_count(con, "billinfodetail")
    before = _null_count(con, "billinfodetail")
    con.execute("""
        UPDATE billinfodetail d
        SET age = r.age
        FROM billrcp r
        WHERE d."BILL_ID" = r."BILL_ID"
          AND d.age IS NULL
          AND r.age IS NOT NULL
    """)
    after = _null_count(con, "billinfodetail")
    return before - after


def normalize_speeches_dae_num(con, dry_run: bool) -> int:
    """speeches.dae_num: '22' → '제22대' (Plan §B.4)."""
    if not _table_exists(con, "speeches"):
        return 0
    cnt = con.execute(
        "SELECT COUNT(*) FROM speeches WHERE dae_num ~ '^[0-9]+$'"
    ).fetchone()[0]
    log.info("  speeches.dae_num: %d numeric-only values to normalize", cnt)
    if dry_run or cnt == 0:
        return cnt
    con.execute(
        "UPDATE speeches SET dae_num = '제' || dae_num || '대' "
        "WHERE dae_num ~ '^[0-9]+$'"
    )
    return cnt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="plan only, no writes")
    parser.add_argument("--db", default=config.DB_PATH)
    args = parser.parse_args()

    con = duckdb.connect(args.db, read_only=args.dry_run)
    log.info("opened %s (dry_run=%s)", args.db, args.dry_run)

    started = datetime.now()
    summary: list[tuple[str, str, int]] = []

    # Order matters: BILLRCP must be backfilled before BILLINFODETAIL join.
    sorted_apis = sorted(
        config.APIS,
        key=lambda s: (
            0 if s.age_source.startswith("constant:") else
            1 if s.age_source.startswith("param:")    else
            2 if s.age_source.startswith("column:")   else
            3 if s.age_source.startswith("date:")     else
            4  # join: last
        ),
    )

    for spec in sorted_apis:
        if not _table_exists(con, spec.table_name):
            log.info("skip %s (table missing)", spec.table_name)
            continue

        kind, _, arg = spec.age_source.partition(":")
        if kind == "constant":
            n = backfill_constant(con, spec.table_name, int(arg), args.dry_run)
        elif kind == "param":
            n = backfill_param(con, spec.table_name, arg, args.dry_run)
        elif kind == "column":
            n = backfill_column(con, spec.table_name, arg, args.dry_run)
        elif kind == "date":
            n = backfill_date(con, spec.table_name, arg, args.dry_run)
        elif kind == "join":
            n = backfill_join_billinfodetail(con, args.dry_run)
        elif kind == "none":
            continue
        else:
            log.warning("unknown age_source kind %s for %s", kind, spec.api_id)
            continue

        nulls = _null_count(con, spec.table_name)
        rows = _row_count(con, spec.table_name)
        log.info("  %s [%s/%s]: filled=%d, null_remaining=%d/%d",
                 spec.table_name, spec.age_behavior, spec.age_source, n, nulls, rows)
        summary.append((spec.table_name, spec.age_source, n))

    # speeches normalization (not in APIS — derived table)
    log.info("normalize speeches.dae_num")
    norm = normalize_speeches_dae_num(con, args.dry_run)
    summary.append(("speeches.dae_num", "format-normalize", norm))

    elapsed = (datetime.now() - started).total_seconds()
    con.close()

    log.info("done in %.1fs", elapsed)
    print()
    print("=" * 70)
    print(f"{'table':38s} {'source':20s} {'filled':>10s}")
    print("=" * 70)
    for name, src, n in summary:
        print(f"{name:38s} {src:20s} {n:>10,}")
    print("=" * 70)


if __name__ == "__main__":
    main()
