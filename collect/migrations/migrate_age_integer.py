"""One-shot migration (2026-07-26): AGE VARCHAR -> INTEGER in assembly_raw.duckdb.

Nine of the 37 API tables were seeded with the API-native ``AGE`` spelling as
VARCHAR (the collector only types the column when it *creates* the table, and
these predate ``_TYPED_COLUMNS``). The rest carry ``age INTEGER``. The mismatch
means a direct ``WHERE age >= 20`` raises BinderException on those nine and
``ORDER BY age`` sorts lexicographically ('9' > '22'); only the ``v_*`` views
were usable, because they wrap the column in ``CAST(AGE AS INTEGER)``.

This script converts the column in place. It is idempotent: tables already
typed INTEGER are skipped, so a re-run is a no-op.

Safety:
  * Pre-flight scans every target table for values that would not survive the
    cast (``AGE IS NOT NULL AND TRY_CAST(AGE AS INTEGER) IS NULL``). A single
    such row aborts the whole run *before* any ALTER is issued.
  * Row count, NULL count and the full age->count histogram are snapshotted per
    table and re-compared after the ALTER; a mismatch is reported as a failure.
  * Each table's ALTER runs in its own transaction.

Column *names* are left alone — DuckDB identifiers are case-insensitive, so
``AGE`` and ``age`` address the same column and the collector's
``_canonical_age_key()`` deliberately preserves whatever casing the API sent.

Usage:
    python collect/migrations/migrate_age_integer.py [--dry-run] [--db PATH]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _bootstrap  # noqa: F401,E402  -- adds repo root + collect/ to sys.path

import argparse  # noqa: E402

import duckdb  # noqa: E402

import config  # noqa: E402

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

INT_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
             "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}

SAMPLE_LIMIT = 10


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def age_columns(con) -> list[tuple[str, str, str]]:
    """(table, age column name as stored, data type) for every collected API table."""
    api_tables = {spec.table_name.lower() for spec in config.APIS}
    rows = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE lower(column_name) = 'age' ORDER BY table_name"
    ).fetchall()
    return [(t, c, dt) for t, c, dt in rows if t.lower() in api_tables]


def snapshot(con, table: str, col: str) -> dict:
    tbl, c = _quote(table), _quote(col)
    rows, nulls = con.execute(
        f"SELECT COUNT(*), COUNT(*) FILTER (WHERE {c} IS NULL) FROM {tbl}"
    ).fetchone()
    # Histogram keyed on the *integer* value so it is comparable across the
    # type change ('20' and 20 must land in the same bucket).
    hist = dict(con.execute(
        f"SELECT TRY_CAST({c} AS INTEGER), COUNT(*) FROM {tbl} "
        f"WHERE {c} IS NOT NULL GROUP BY 1"
    ).fetchall())
    return {"rows": rows, "nulls": nulls, "hist": hist}


def preflight(con, targets: list[tuple[str, str, str]]) -> list[str]:
    """Return a list of blocking problems (empty == safe to proceed)."""
    problems: list[str] = []
    print("== 사전검사: 캐스팅 불가 값 스캔 ==")
    for table, col, _ in targets:
        tbl, c = _quote(table), _quote(col)
        bad = con.execute(
            f"SELECT COUNT(*) FROM {tbl} "
            f"WHERE {c} IS NOT NULL AND TRY_CAST({c} AS INTEGER) IS NULL"
        ).fetchone()[0]
        if bad:
            samples = con.execute(
                f"SELECT DISTINCT {c} FROM {tbl} "
                f"WHERE {c} IS NOT NULL AND TRY_CAST({c} AS INTEGER) IS NULL "
                f"LIMIT {SAMPLE_LIMIT}"
            ).fetchall()
            rendered = ", ".join(repr(s[0]) for s in samples)
            print(f"  X {table}.{col}: {bad:,} rows uncastable — samples: {rendered}")
            problems.append(f"{table}.{col}: {bad} uncastable values")
        else:
            print(f"  ok {table}.{col}: 0 uncastable")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=config.RAW_DB_PATH, help="assembly_raw.duckdb path")
    parser.add_argument("--dry-run", action="store_true",
                        help="run pre-flight + snapshot only, issue no ALTER")
    args = parser.parse_args()

    con = duckdb.connect(args.db)
    try:
        all_age = age_columns(con)
        targets = [(t, c, dt) for t, c, dt in all_age if dt.upper() not in INT_TYPES]
        already = [(t, c, dt) for t, c, dt in all_age if dt.upper() in INT_TYPES]

        print(f"DB: {args.db}")
        print(f"age 컬럼 보유 API 테이블: {len(all_age)} "
              f"(이미 INTEGER {len(already)}, 변환 대상 {len(targets)})\n")

        if not targets:
            print("변환 대상 없음 — 이미 마이그레이션 완료 상태 (no-op).")
            return 0

        print("== 변환 대상 ==")
        for t, c, dt in targets:
            print(f"  {t}.{c} : {dt}")
        print()

        problems = preflight(con, targets)
        if problems:
            print(f"\n중단: {len(problems)}개 테이블에 캐스팅 불가 값 존재. ALTER 미실행.")
            for p in problems:
                print(f"  - {p}")
            return 1
        print()

        before = {t: snapshot(con, t, c) for t, c, _ in targets}

        if args.dry_run:
            print("== dry-run: ALTER 생략 ==")
            for t, c, dt in targets:
                s = before[t]
                print(f"  {t}.{c} rows={s['rows']:,} null={s['nulls']:,} "
                      f"distinct_age={len(s['hist'])}")
            return 0

        print("== ALTER 실행 ==")
        failures: list[str] = []
        for table, col, dt in targets:
            tbl, c = _quote(table), _quote(col)
            b = before[table]
            try:
                con.execute("BEGIN TRANSACTION")
                con.execute(
                    f"ALTER TABLE {tbl} ALTER COLUMN {c} SET DATA TYPE INTEGER "
                    f"USING CAST({c} AS INTEGER)"
                )
                con.execute("COMMIT")
            except Exception as e:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                print(f"  X {table}.{col}: ALTER 실패 — {type(e).__name__}: {e}")
                failures.append(f"{table}: ALTER failed ({e})")
                continue

            a = snapshot(con, table, col)
            new_type = con.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = ? AND lower(column_name) = 'age'",
                [table],
            ).fetchone()[0]

            diffs = []
            if a["rows"] != b["rows"]:
                diffs.append(f"rows {b['rows']:,} -> {a['rows']:,}")
            if a["nulls"] != b["nulls"]:
                diffs.append(f"nulls {b['nulls']:,} -> {a['nulls']:,}")
            if a["hist"] != b["hist"]:
                changed = sorted(
                    set(b["hist"]) | set(a["hist"]),
                    key=lambda k: (k is None, k),
                )
                detail = [f"{k}: {b['hist'].get(k, 0):,} -> {a['hist'].get(k, 0):,}"
                          for k in changed if b["hist"].get(k) != a["hist"].get(k)]
                diffs.append("histogram " + "; ".join(detail))
            if new_type.upper() not in INT_TYPES:
                diffs.append(f"type still {new_type}")

            if diffs:
                print(f"  X {table}.{col}: {dt} -> {new_type} but " + " | ".join(diffs))
                failures.append(f"{table}: " + " | ".join(diffs))
            else:
                print(f"  ok {table}.{col}: {dt} -> {new_type}  "
                      f"rows={a['rows']:,} null={a['nulls']:,} "
                      f"ages={min(a['hist'], default=None)}..{max(a['hist'], default=None)} "
                      f"({len(a['hist'])} distinct) 보존 확인")

        print("\n== 요약 ==")
        for table, col, dt in targets:
            b = before[table]
            now = con.execute(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = ? AND lower(column_name) = 'age'",
                [table],
            ).fetchone()[0]
            print(f"  {table:22s} {col:4s} {dt:8s} -> {now:8s}  rows={b['rows']:,}")

        if failures:
            print(f"\nFAILED ({len(failures)}):")
            for f in failures:
                print(f"  X {f}")
            return 1

        print(f"\nOK — {len(targets)}개 테이블 AGE VARCHAR -> INTEGER 완료 "
              f"(행수·NULL수·값분포 전부 보존)")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
