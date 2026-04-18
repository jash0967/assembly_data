"""Phase 0 audit snapshot: row counts + per-table age-like column distribution.

One-shot script. Output: data/_audit/pre_migration.json
Opens assembly.duckdb read-only so it can run alongside the MCP server.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb

DB_PATH = Path("data/assembly.duckdb")
OUT_PATH = Path("data/_audit/pre_migration.json")

AGE_LIKE_COLUMNS = (
    "AGE",
    "DAE_NUM",
    "ERACO",
    "REGDAESU",
    "UNIT_CD",
    "PROFILE_UNIT_CD",
    "ORD_NUM",
    "DIV",
    "YR",
    "age",
    "dae_num",
)


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)

    snapshot: dict = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "db_path": str(DB_PATH),
        "tables": {},
    }

    rows = con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name='main' ORDER BY table_name"
    ).fetchall()
    table_names = [r[0] for r in rows]

    for name in table_names:
        cols = con.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE schema_name='main' AND table_name=? ORDER BY column_index",
            [name],
        ).fetchall()
        col_names = [c[0] for c in cols]

        rowcount = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]

        age_dists: dict[str, list[dict]] = {}
        for c in col_names:
            if c in AGE_LIKE_COLUMNS:
                try:
                    dist = con.execute(
                        f'SELECT "{c}" AS v, COUNT(*) AS n FROM "{name}" '
                        f'GROUP BY "{c}" ORDER BY n DESC LIMIT 50'
                    ).fetchall()
                    age_dists[c] = [{"value": str(v), "count": n} for v, n in dist]
                except duckdb.Error as e:
                    age_dists[c] = [{"error": str(e)}]

        snapshot["tables"][name] = {
            "rowcount": rowcount,
            "columns": col_names,
            "age_distributions": age_dists,
        }

    views = con.execute(
        "SELECT view_name FROM duckdb_views() WHERE schema_name='main' ORDER BY view_name"
    ).fetchall()
    snapshot["views"] = [v[0] for v in views]

    con.close()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    n_tables = len(snapshot["tables"])
    n_views = len(snapshot["views"])
    total_rows = sum(t["rowcount"] for t in snapshot["tables"].values())
    age_tables = sum(1 for t in snapshot["tables"].values() if t["age_distributions"])
    print(f"Snapshot written: {OUT_PATH}")
    print(f"  tables={n_tables}  views={n_views}  total_rows={total_rows:,}")
    print(f"  tables with age-like cols: {age_tables}")


if __name__ == "__main__":
    main()
