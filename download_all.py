"""국회 Open API 데이터 수집기.

Usage:
    python -m assembly.download_all                    # 전체 19개 API 수집
    python -m assembly.download_all --api BILLRCP      # 특정 API만
    python -m assembly.download_all --max-age 22       # 최대 대수 지정
    python -m assembly.download_all --status            # 진행 상황 확인
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import duckdb

import config
from config import APIS, MAX_AGE
from collector import collect_api
from api_client import MandatoryParamError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── DB ──────────────────────────────────────────────────────

def init_db(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS _progress (
            task_key    VARCHAR PRIMARY KEY,
            api_id      VARCHAR,
            name_kr     VARCHAR,
            row_count   INTEGER,
            fetched_at  TIMESTAMP
        )
    """)


def done_keys_for(con, api_id: str) -> set[str]:
    rows = con.execute(
        "SELECT task_key FROM _progress WHERE api_id = ?", [api_id]
    ).fetchall()
    return {r[0] for r in rows}


def save_rows(con, table_name, task_key, api_id, name_kr, rows):
    if rows:
        all_cols = []
        seen = set()
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    all_cols.append(k)
                    seen.add(k)

        _ensure_table(con, table_name, all_cols)

        col_str = ", ".join(f'"{c}"' for c in all_cols)
        placeholders = ", ".join("?" for _ in all_cols)
        values = [[str(row.get(c, "")) if row.get(c) is not None else "" for c in all_cols] for row in rows]
        con.executemany(
            f'INSERT INTO "{table_name}" ({col_str}) VALUES ({placeholders})',
            values,
        )

    con.execute("DELETE FROM _progress WHERE task_key = ?", [task_key])
    con.execute(
        "INSERT INTO _progress VALUES (?, ?, ?, ?, ?)",
        [task_key, api_id, name_kr, len(rows), datetime.now()],
    )


def _ensure_table(con, table_name, columns):
    exists = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    ).fetchone()

    if not exists:
        col_defs = ", ".join(f'"{c}" VARCHAR' for c in columns)
        con.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
    else:
        existing = {
            r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [table_name],
            ).fetchall()
        }
        for c in columns:
            if c not in existing:
                con.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{c}" VARCHAR')


# ── Progress ────────────────────────────────────────────────

def update_progress(done, skipped, errors, current, idx, total, t0):
    elapsed = (datetime.now() - t0).total_seconds()
    eta = str(timedelta(seconds=int(elapsed / idx * (total - idx)))) if idx > 0 else "..."
    db_size = os.path.getsize(config.DB_PATH) if os.path.exists(config.DB_PATH) else 0

    with open(config.PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "current_api": current,
            "progress": f"{idx}/{total}",
            "pct": round(idx / total * 100, 1) if total else 0,
            "done": done, "skipped": skipped, "errors": errors,
            "eta": eta,
            "elapsed": str(timedelta(seconds=int(elapsed))),
            "db_mb": round(db_size / 1024 / 1024, 1),
        }, f, ensure_ascii=False, indent=2)


def show_status():
    print("=" * 55)
    print(" 국회 Open API 수집기 - 진행 상황")
    print("=" * 55)

    if os.path.exists(config.PROGRESS_FILE):
        with open(config.PROGRESS_FILE, encoding="utf-8") as f:
            p = json.load(f)
        print(f"  현재 API  : {p['current_api']}")
        print(f"  진행      : {p['progress']}  ({p['pct']}%)")
        print(f"  완료/건너뜀/오류 : {p['done']}/{p['skipped']}/{p['errors']}")
        print(f"  경과/ETA  : {p['elapsed']} / {p['eta']}")
        print(f"  DB 크기   : {p['db_mb']} MB")
    else:
        print("  진행 파일 없음.")

    if os.path.exists(config.DB_PATH):
        try:
            con = duckdb.connect(config.DB_PATH, read_only=True)
            rows = con.execute(
                "SELECT api_id, name_kr, SUM(row_count) as total, COUNT(*) as tasks "
                "FROM _progress GROUP BY api_id, name_kr ORDER BY api_id"
            ).fetchall()
            con.close()
            print()
            for api_id, name_kr, total, tasks in rows:
                print(f"  {name_kr:20s}  {total:>8,}건  ({tasks} tasks)")
        except Exception:
            print("  DB 조회 불가")

    print("=" * 55)


# ── Main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="국회 Open API 수집기")
    parser.add_argument("--api", help="특정 API만 수집 (api_id)")
    parser.add_argument("--max-age", type=int, default=MAX_AGE)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    apis = APIS
    if args.api:
        apis = [s for s in APIS if s.api_id == args.api]
        if not apis:
            print(f"ERROR: '{args.api}' 없음", file=sys.stderr)
            print("가능한 API:", ", ".join(s.api_id for s in APIS))
            sys.exit(1)

    age_range = range(1, args.max_age + 1)

    total_tasks = sum(len(age_range) if s.iterate_age else 1 for s in apis)

    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    con = duckdb.connect(config.DB_PATH)
    init_db(con)

    print("=" * 55)
    print(f" 대상 API: {len(apis)}개  |  총 작업: {total_tasks}개")
    print(f" DB: {config.DB_PATH}")
    print("=" * 55)

    done = skipped = errors = 0
    t0 = datetime.now()
    task_idx = 0

    for api_idx, spec in enumerate(apis, 1):
        print(f"\n[{api_idx}/{len(apis)}] {spec.name_kr} ({spec.api_id})")

        skip = done_keys_for(con, spec.api_id)
        if skip:
            skip_count = len(skip)
            expected = len(age_range) if spec.iterate_age else 1
            if skip_count >= expected:
                print(f"  이미 완료 ({skip_count} tasks)")
                skipped += skip_count
                task_idx += expected
                update_progress(done, skipped, errors, spec.name_kr, task_idx, total_tasks, t0)
                continue

        try:
            results = collect_api(spec, age_range=age_range, skip_keys=skip)
        except MandatoryParamError as e:
            logger.warning(f"  SKIP (필수 파라미터 누락): {e}")
            errors += 1
            task_idx += len(age_range) if spec.iterate_age else 1
            update_progress(done, skipped, errors, spec.name_kr, task_idx, total_tasks, t0)
            continue

        for task_key, rows in results.items():
            task_idx += 1
            save_rows(con, spec.table_name, task_key, spec.api_id, spec.name_kr, rows)
            done += 1
            print(f"  {task_key}: {len(rows):,}건", flush=True)

        update_progress(done, skipped, errors, spec.name_kr, task_idx, total_tasks, t0)

    con.close()
    print()
    print("=" * 55)
    print(f" 완료  저장:{done:,}  건너뜀:{skipped:,}  오류:{errors:,}")
    print(f" 소요: {str(datetime.now() - t0).split('.')[0]}")
    print("=" * 55)


if __name__ == "__main__":
    main()
