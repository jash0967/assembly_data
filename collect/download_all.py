"""국회 Open API 데이터 수집기.

Usage:
    python download_all.py                      # 전체 API 수집 (13대~22대)
    python download_all.py --api BILLRCP        # 특정 API만
    python download_all.py --min-age 17         # 수집 시작 대수 변경
    python download_all.py --stale-days 0       # 전체 강제 재수집
    python download_all.py --status             # 진행 상황 확인
    python download_all.py --verbose            # 개별 task 상세 출력
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

import _bootstrap  # noqa: F401  -- adds repo root + collect/ to sys.path

import config
from config import APIS, MAX_AGE
from collector import collect_api, _generate_tasks
from api_client import MandatoryParamError
from age_utils import derive_age
from progress import (
    PerStrategyETA,
    print_job_header, print_api_start, print_api_progress,
    print_api_done, print_api_cached, print_api_error,
    print_phase_separator, print_midpoint_summary, print_job_footer,
)

logging.basicConfig(
    level=logging.WARNING,
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
    # Drop legacy _task_key indexes that cause DuckDB ART index bugs
    try:
        for row in con.execute(
            "SELECT index_name FROM duckdb_indexes() WHERE index_name LIKE '%__task_key'"
        ).fetchall():
            try:
                con.execute(f'DROP INDEX "{row[0]}"')
            except Exception:
                pass
    except Exception:
        pass


def valid_keys_for(con, api_id: str, stale_days: int = 7,
                   current_age_stale_days: int = 1) -> set[str]:
    """완료된 task_key 집합 반환."""
    rows = con.execute(
        "SELECT task_key, fetched_at FROM _progress WHERE api_id = ?",
        [api_id],
    ).fetchall()

    now = datetime.now()
    valid = set()
    current_marker = f"__AGE_{MAX_AGE}"
    for task_key, fetched_at in rows:
        if current_marker in task_key:
            cutoff = now - timedelta(days=current_age_stale_days)
        else:
            cutoff = now - timedelta(days=stale_days)
        if fetched_at > cutoff:
            valid.add(task_key)
    return valid


_AGE_KEY = "age"


def _canonical_age_key(rows: list[dict]) -> str:
    """Spelling to store the age under for this batch.

    DuckDB identifiers are case-insensitive, so an API-supplied ``AGE`` and our
    injected ``age`` are the *same* column: emitting both blows up CREATE /
    ALTER / INSERT. Reuse whatever casing the API sent so the on-disk schema
    never drifts (9 tables were seeded with ``AGE VARCHAR``).
    """
    for row in rows:
        for k in row:
            if k.lower() == _AGE_KEY:
                return k
    return _AGE_KEY


def transform_rows(rows: list[dict], task_key: str, age_source: str = "none") -> list[dict]:
    transformed = []
    age_key = _canonical_age_key(rows)
    for row in rows:
        cleaned = {"_task_key": task_key}
        for k, v in row.items():
            if isinstance(v, str):
                v = v.strip()
                if v == "":
                    v = None
            cleaned[k] = v
        # Write-time age injection. None for `join:...` (resolved post-insert)
        # or when source value is missing — backfill / validator handles those.
        derived = derive_age(age_source, task_key=task_key, row=cleaned)
        # Fold every casing variant into the single canonical key. When the
        # derivation yields nothing, keep the API's own value so the validator's
        # "no NULL age" invariant keeps holding on AGE-bearing tables.
        raw_age = None
        for k in [k for k in cleaned if k.lower() == _AGE_KEY]:
            v = cleaned.pop(k)
            if raw_age is None:
                raw_age = v
        cleaned[age_key] = derived if derived is not None else raw_age
        transformed.append(cleaned)
    return transformed


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _serialize_value(v):
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return json.dumps(v, ensure_ascii=False)


def _reconnect(db_holder):
    """Close broken connection and open a fresh one."""
    try:
        db_holder[0].close()
    except Exception:
        pass
    db_holder[0] = duckdb.connect(config.RAW_DB_PATH)


def save_rows(db_holder, table_name, task_key, api_id, name_kr, rows, age_source: str = "none"):
    rows = transform_rows(rows, task_key, age_source)
    if not rows:
        return
    con = db_holder[0]

    all_cols = []
    seen = set()   # case-folded: DuckDB treats "AGE" and "age" as one column
    for row in rows:
        for k in row.keys():
            if k.lower() not in seen:
                all_cols.append(k)
                seen.add(k.lower())

    _ensure_table(con, table_name, all_cols)
    tbl = _quote_ident(table_name)

    try:
        con.commit()
    except duckdb.InvalidInputException:
        pass
    con.begin()
    try:
        con.execute(f"DELETE FROM {tbl} WHERE {_quote_ident('_task_key')} = ?", [task_key])
        col_str = ", ".join(_quote_ident(c) for c in all_cols)
        placeholders = ", ".join("?" for _ in all_cols)
        values = [[_serialize_value(row.get(c)) for c in all_cols] for row in rows]
        stmt = f"INSERT INTO {tbl} ({col_str}) VALUES ({placeholders})"
        BATCH = 500
        for i in range(0, len(values), BATCH):
            con.executemany(stmt, values[i:i + BATCH])
        con.execute(
            "INSERT OR REPLACE INTO _progress VALUES (?, ?, ?, ?, ?)",
            [task_key, api_id, name_kr, len(rows), datetime.now()],
        )
        con.commit()
    except duckdb.FatalException:
        logger.warning("DuckDB fatal error on %s – reconnecting", task_key)
        _reconnect(db_holder)
        raise
    except Exception:
        try:
            con.rollback()
        except duckdb.FatalException:
            logger.warning("DuckDB fatal on rollback for %s – reconnecting", task_key)
            _reconnect(db_holder)
        raise


_TYPED_COLUMNS = {"age": "INTEGER"}


def _column_type(col: str) -> str:
    return _TYPED_COLUMNS.get(col.lower(), "VARCHAR")


def _ensure_table(con, table_name, columns):
    exists = con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table_name],
    ).fetchone()

    tbl = _quote_ident(table_name)
    if not exists:
        col_defs = ", ".join(f"{_quote_ident(c)} {_column_type(c)}" for c in columns)
        con.execute(f"CREATE TABLE {tbl} ({col_defs})")
    else:
        # Case-folded compare: DuckDB identifiers are case-insensitive, so
        # ADD COLUMN "age" against a table already holding "AGE" raises
        # `Catalog Error: Column with name age already exists!`.
        existing = {
            r[0].lower() for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                [table_name],
            ).fetchall()
        }
        for c in columns:
            if c.lower() not in existing:
                con.execute(f"ALTER TABLE {tbl} ADD COLUMN {_quote_ident(c)} {_column_type(c)}")
                existing.add(c.lower())


def _estimate_tasks(spec, age_range, con=None) -> int:
    try:
        return len(_generate_tasks(spec, age_range, con))
    except Exception:
        return 1


# ── Progress file ──────────────────────────────────────────

def update_progress(current_api, api_idx, api_total,
                    task_idx, total_tasks,
                    done, skipped_cached, skipped_empty, errors,
                    eta_tracker):
    db_size = os.path.getsize(config.RAW_DB_PATH) if os.path.exists(config.RAW_DB_PATH) else 0
    elapsed = eta_tracker.elapsed().total_seconds()

    with open(config.PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().isoformat(),
            "current_api": current_api,
            "api_progress": f"{api_idx}/{api_total}",
            "task_progress": f"{task_idx}/{total_tasks}",
            "pct": round(task_idx / total_tasks * 100, 1) if total_tasks else 0,
            "done": done,
            "skipped_cached": skipped_cached,
            "skipped_empty": skipped_empty,
            "errors": errors,
            "elapsed_sec": int(elapsed),
            "eta_range": eta_tracker.eta_range_str(),
            "last_activity": eta_tracker.last_activity_ago(),
            "db_mb": round(db_size / 1024 / 1024, 1),
        }, f, ensure_ascii=False, indent=2)


def show_status():
    print("=" * 55)
    print(" 국회 Open API 수집기 - 진행 상황")
    print("=" * 55)

    if os.path.exists(config.PROGRESS_FILE):
        with open(config.PROGRESS_FILE, encoding="utf-8") as f:
            p = json.load(f)
        print(f"  현재 API   : {p.get('current_api', '?')}")
        print(f"  API 진행   : {p.get('api_progress', '?')}")
        print(f"  Task 진행  : {p.get('task_progress', '?')}  ({p.get('pct', 0)}%)")
        print(f"  저장/캐시/빈응답/오류 : {p.get('done', 0)}/{p.get('skipped_cached', 0)}/{p.get('skipped_empty', 0)}/{p.get('errors', 0)}")
        print(f"  남은 시간  : {p.get('eta_range', '?')}")
        print(f"  마지막 응답: {p.get('last_activity', '?')}")
        print(f"  DB 크기   : {p.get('db_mb', 0)} MB")
    else:
        print("  진행 파일 없음.")

    if os.path.exists(config.RAW_DB_PATH):
        try:
            con = duckdb.connect(config.RAW_DB_PATH, read_only=True)
            rows = con.execute(
                "SELECT api_id, name_kr, SUM(row_count) as total, COUNT(*) as tasks "
                "FROM _progress GROUP BY api_id, name_kr ORDER BY api_id"
            ).fetchall()
            con.close()
            print()
            for api_id, name_kr, total, tasks in rows:
                print(f"  {name_kr:25s}  {total:>8,}건  ({tasks} tasks)")
        except Exception:
            print("  DB 조회 불가")

    print("=" * 55)


# ── Main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="국회 Open API 수집기")
    parser.add_argument("--api", help="특정 API만 수집 (api_id)")
    parser.add_argument("--min-age", type=int, default=13,
                        help="수집 시작 대수 (기본: 13)")
    parser.add_argument("--max-age", type=int, default=MAX_AGE)
    parser.add_argument("--stale-days", type=int, default=7,
                        help="재수집 기준 일수 (기본: 7일)")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--verbose", action="store_true",
                        help="개별 task 상세 출력")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    apis = APIS
    if args.api:
        apis = [s for s in APIS if s.api_id == args.api]
        if not apis:
            print(f"ERROR: '{args.api}' 없음", file=sys.stderr)
            print("가능한 API:", ", ".join(s.api_id for s in APIS))
            sys.exit(1)

    age_range = range(args.min_age, args.max_age + 1)

    os.makedirs(os.path.dirname(config.RAW_DB_PATH), exist_ok=True)
    db_holder = [duckdb.connect(config.RAW_DB_PATH)]
    con = db_holder[0]
    init_db(con)

    apis_sorted = sorted(apis, key=lambda s: s.phase)

    # ── 사전 계산: task 수, 캐시 현황, ETA 계획 ──
    eta_tracker = PerStrategyETA()
    total_tasks = 0
    total_cached = 0
    remaining_by_strategy: dict[str, int] = {}

    for spec in apis_sorted:
        expected = _estimate_tasks(spec, age_range, con)
        cached = len(valid_keys_for(con, spec.api_id, stale_days=args.stale_days))
        new = max(0, expected - cached)
        total_tasks += expected
        total_cached += cached
        remaining_by_strategy[spec.strategy] = (
            remaining_by_strategy.get(spec.strategy, 0) + new
        )

    eta_tracker.set_plan(remaining_by_strategy)
    total_new = total_tasks - total_cached

    db_mb_start = os.path.getsize(config.RAW_DB_PATH) / 1024 / 1024 if os.path.exists(config.RAW_DB_PATH) else 0

    print_job_header(
        api_count=len(apis_sorted),
        total_tasks=total_tasks,
        cached_tasks=total_cached,
        new_tasks=total_new,
        age_range=age_range,
        db_path=config.RAW_DB_PATH,
        db_mb=db_mb_start,
    )

    done = skipped_cached = skipped_empty = error_count = 0
    error_details: list[str] = []
    task_idx = 0
    prev_phase = apis_sorted[0].phase if apis_sorted else 1
    last_midpoint_api = 0

    import threading
    _db_lock = threading.Lock()

    for api_idx, spec in enumerate(apis_sorted, 1):
        # phase 전환 시 구분선
        if spec.phase != prev_phase:
            print_phase_separator(spec.phase)
            prev_phase = spec.phase

        con = db_holder[0]  # refresh in case of reconnection
        expected = _estimate_tasks(spec, age_range, con)
        skip = valid_keys_for(con, spec.api_id, stale_days=args.stale_days)

        # ── 캐시로 전부 건너뛸 경우 ──
        if skip and len(skip) >= expected:
            print_api_cached(api_idx, len(apis_sorted), spec.name_kr, len(skip))
            skipped_cached += len(skip)
            task_idx += expected
            eta_tracker.skip(spec.strategy, expected)
            update_progress(spec.name_kr, api_idx, len(apis_sorted),
                            task_idx, total_tasks,
                            done, skipped_cached, skipped_empty, error_count,
                            eta_tracker)
            continue

        # ── API 시작 ──
        cached_count = len(skip) if skip else 0
        new_expected = max(0, expected - cached_count)
        print_api_start(
            api_idx, len(apis_sorted), spec.name_kr,
            spec.strategy, expected, cached_count,
            task_idx, total_tasks, eta_tracker.elapsed(),
        )

        api_t0 = datetime.now()
        api_rows = 0
        api_empty = 0
        api_errors = 0
        task_in_api = 0
        tasks_in_api = new_expected

        def on_task_done(task_key, rows,
                         _spec=spec, _api_idx=api_idx):
            nonlocal done, skipped_empty, error_count, task_idx
            nonlocal task_in_api, api_rows, api_empty
            with _db_lock:
                task_t0 = datetime.now()
                task_idx += 1
                task_in_api += 1

                if not rows:
                    skipped_empty += 1
                    eta_tracker.record(_spec.strategy, 0.1)
                else:
                    save_rows(db_holder, _spec.table_name, task_key,
                              _spec.api_id, _spec.name_kr, rows,
                              age_source=_spec.age_source)
                    done += 1
                    api_rows += len(rows)
                    task_elapsed = (datetime.now() - task_t0).total_seconds()
                    eta_tracker.record(_spec.strategy, task_elapsed)

                if args.verbose and rows:
                    label = task_key.split('__', 1)[-1] if '__' in task_key else '-'
                    print(f"\r  {_spec.name_kr} ({label}): {len(rows):,}건")
                else:
                    update_interval = max(1, tasks_in_api // 50)
                    if task_in_api % update_interval == 0 or task_in_api == tasks_in_api:
                        print_api_progress(task_in_api, tasks_in_api, api_rows, eta_tracker)

        try:
            collect_api(spec, age_range=age_range, skip_keys=skip, con=db_holder[0],
                        on_task_done=on_task_done)
        except MandatoryParamError as e:
            print_api_error(api_idx, len(apis_sorted), spec.name_kr, str(e))
            error_count += 1
            error_details.append(f"{spec.name_kr}: {e}")
            remaining = expected - task_in_api
            task_idx += remaining
            eta_tracker.skip(spec.strategy, remaining)
            update_progress(spec.name_kr, api_idx, len(apis_sorted),
                            task_idx, total_tasks,
                            done, skipped_cached, skipped_empty, error_count,
                            eta_tracker)
            continue

        # ── API 완료 ──
        api_duration = (datetime.now() - api_t0).total_seconds()
        print_api_done(api_idx, len(apis_sorted), spec.name_kr,
                       api_rows, api_empty, api_errors, api_duration)

        update_progress(spec.name_kr, api_idx, len(apis_sorted),
                        task_idx, total_tasks,
                        done, skipped_cached, skipped_empty, error_count,
                        eta_tracker)

        # ── 중간 요약 (5개 API마다) ──
        if api_idx - last_midpoint_api >= 5 and api_idx < len(apis_sorted):
            print_midpoint_summary(task_idx, total_tasks, eta_tracker.elapsed(), eta_tracker)
            last_midpoint_api = api_idx

    db_holder[0].close()

    db_mb_end = os.path.getsize(config.RAW_DB_PATH) / 1024 / 1024 if os.path.exists(config.RAW_DB_PATH) else 0
    print_job_footer(
        done=done,
        skipped_cached=skipped_cached,
        skipped_empty=skipped_empty,
        errors=error_count,
        elapsed=eta_tracker.elapsed(),
        db_mb=db_mb_end,
        db_mb_start=db_mb_start,
        error_details=error_details,
    )

    # Phase 6: drift detection. Exit 1 on any invariant violation.
    print()
    from validate_collection import validate
    val_con = duckdb.connect(config.ANALYSIS_DB_PATH, read_only=True)
    try:
        val_con.execute(f"ATTACH '{config.RAW_DB_PATH}' AS raw (READ_ONLY)")
        val_errors = validate(val_con, raw_catalog="raw",
                              analysis_catalog="assembly_analysis")
    finally:
        val_con.close()
    if val_errors:
        print(f"VALIDATION FAILED ({len(val_errors)} error(s)):")
        for e in val_errors:
            print(f"  ✗ {e}")
        sys.exit(1)
    print("validation: OK")


if __name__ == "__main__":
    # DB 변경 이력 기록 — 무엇이 갱신됐는지 data/_audit/db_updates.jsonl 에 남는다.
    # 감사는 read-only 스냅샷만 찍으므로 이 스크립트의 커넥션·트랜잭션과 무관하다.
    import db_audit
    with db_audit.audit_run(__file__, config.RAW_DB_PATH, argv=sys.argv[1:]):
        main()
