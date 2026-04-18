"""Phase 2 post-backfill verification.

Checks each ApiSpec table for:
- age column existence
- NULL age count
- distinct ages (current_only must be exactly 1)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

import config

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

con = duckdb.connect(config.DB_PATH, read_only=True)

errors: list[str] = []
warnings: list[str] = []

print(f"{'table':38s} {'behavior':14s} {'rows':>10s} {'null':>10s} {'distinct':>10s}  ages")
print("=" * 110)

for spec in config.APIS:
    t = spec.table_name
    exists = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [t]
    ).fetchone()
    if not exists:
        continue

    cols_lower = {c.lower() for c in (
        r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [t]
        ).fetchall()
    )}
    if "age" not in cols_lower:
        errors.append(f"{t}: no age column")
        continue

    rows = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    nulls = con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE age IS NULL').fetchone()[0]
    distinct = con.execute(f'SELECT COUNT(DISTINCT age) FROM "{t}" WHERE age IS NOT NULL').fetchone()[0]
    sample_ages = con.execute(
        f'SELECT age, COUNT(*) AS n FROM "{t}" WHERE age IS NOT NULL '
        f'GROUP BY age ORDER BY age'
    ).fetchall()
    ages_str = ", ".join(f"{a}:{n}" for a, n in sample_ages[:10])
    if len(sample_ages) > 10:
        ages_str += f" ... +{len(sample_ages) - 10}"

    print(f"{t:38s} {spec.age_behavior:14s} {rows:>10,} {nulls:>10,} {distinct:>10,}  {ages_str}")

    if spec.age_behavior == "current_only" and distinct > 1:
        errors.append(f"{t}: current_only but distinct ages = {distinct}")
    if spec.age_behavior in ("per_age", "by_date", "by_bill_id") and nulls > 0:
        # For per_age etc., NULLs are warnings unless they're the special-body negative ages
        warnings.append(f"{t}: {nulls} NULL ages")

print()
print("--- Errors ---")
for e in errors:
    print(f"  ✗ {e}")
if not errors:
    print("  (none)")
print("--- Warnings ---")
for w in warnings:
    print(f"  ! {w}")
if not warnings:
    print("  (none)")

# speeches.dae_num normalization check
remain = con.execute("SELECT COUNT(*) FROM speeches WHERE dae_num ~ '^[0-9]+$'").fetchone()[0]
print(f"\nspeeches.dae_num numeric-only remaining: {remain}")

con.close()
