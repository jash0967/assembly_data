"""DB 업데이트 감사 로그 — 수집·분석 백엔드가 DuckDB를 바꿀 때마다 무엇이 바뀌었는지 기록.

정본 writer 스크립트(collect/·analyze/)를 `audit_run()` 으로 감싸면, 그 실행이
어느 테이블을 어떻게 바꿨는지 `data/_audit/db_updates.jsonl` 에 append 된다.

    if __name__ == "__main__":
        import db_audit
        with db_audit.audit_run(__file__, config.RAW_DB_PATH, argv=sys.argv[1:]):
            main()

조회는 CLI(`python db_audit.py --log`) 또는 DuckDB/MCP로 직접:

    SELECT * FROM read_json_auto('data/_audit/db_updates.jsonl')
    WHERE event = 'run_end' ORDER BY finished_at DESC;

────────────────────────────────────────────────────────────────────────
불변식 두 개 — 감사가 본 데이터를 절대 건드리지 않는다
────────────────────────────────────────────────────────────────────────
1. **대상 DB에 write 커넥션을 열지 않는다. 파이프라인 커넥션도 쓰지 않는다.**
   스냅샷은 `read_only=True` 로 짧게 열고 즉시 닫는다. 못 열면(다른 RW가 잡고
   있으면) 조용히 포기한다. 이 규칙을 깨면 실측으로 확인된 손상 경로가 열린다:
     - 파이프라인 커넥션 재사용 → DuckDB에서 제3자의 commit()이 **남의 미완
       트랜잭션을 확정**시킨다. `download_all.save_rows()` 가 DELETE 직후
       Ctrl-C 로 멈춘 상태에서 감사가 마감 쓰기를 하면 DELETE만 커밋되고
       INSERT는 유실된다 (`_progress.fetched_at` 은 옛 값이라 재수집도 안 됨).
     - 감사가 RW를 먼저 열면 같은 프로세스의 read-only 접속이 전부
       ConnectionException 으로 죽는다 (CLAUDE.md §Assembly DuckDB access 의
       "RO+RW 혼용 불가" 불변식. download_bills:84 / download_documents:424
       --dry-run / subtopic_bertopic:308 / download_all:503 이 해당).
2. **출력은 대상 DB 밖(JSONL 사이드카)에 쓴다.**
   `news_cleaning.py` 는 빌드 실패 시 `.bak` 을 살아있는 DB 파일 위에 copy2
   한다. 감사 이력이 그 DB 안에 있으면 롤백과 함께 사라지고, 열린 커넥션이
   있으면 복원본을 stale 페이지로 덮어써 손상시킬 수 있다. 파일이 별도라
   양쪽 다 성립하지 않는다. 이벤트마다 append+fsync 하므로 SIGKILL 에도
   그 시점까지 남는다.

감사 코드의 어떤 실패도 파이프라인을 멈추지 않는다 — 전부 삼키고 stderr 경고.

────────────────────────────────────────────────────────────────────────
변경을 어떻게 판정하는가 — 관측된 사실만
────────────────────────────────────────────────────────────────────────
run 시작·종료에 read-only 스냅샷을 찍어 비교한다:
  - `COUNT(*)` 차이            → rows_before / rows_after / delta
  - `duckdb_tables().sql` 해시 변화 → 컬럼 추가·타입 변경 탐지
  - 행 단위 write 타임스탬프(`extracted_at`, `classified_at`, `ingested_at` …)로
    `COUNT(*) FILTER (WHERE ts >= run_started_at)` → **이번 run 이 실제로 남긴
    행 수**. 시도한 문(statement) 수가 아니라 DB에 남은 효과를 세므로
    `INSERT OR IGNORE` 로 무시된 행·롤백된 행이 자동으로 빠진다.
  - raw DB는 `_progress` 를 같은 방식으로 조회해 어느 API의 몇 개 task가
    다시 쓰였는지까지 남긴다 (파이프라인 트랜잭션과 함께 롤백되므로 정확).
  - 테이블을 통째로 DROP+CREATE 하는 두 곳(news_cleaning 의 CTAS 재빌드,
    build_news_db --rebuild)은 스스로 `run.mark_rebuilt(table, 사유)` 로 알린다.
    `duckdb_tables().table_oid` 로 이걸 자동 탐지하려던 초기 설계는 폐기했다 —
    실측 결과 oid 는 카탈로그 로드 순서로 재부여되는 위치 id 라서 DB를 다시
    열기만 해도 값이 바뀌고(20625 ↔ 22140), 커넥션이 다른 두 스냅샷 사이에서는
    "재생성됐다/아니다"를 전혀 판별하지 못한다.

writer 함수 자체는 계측하지 않는다 — "몇 번 INSERT를 호출했나"는 위 관측과
어긋날 수 있고(무시된 upsert, 롤백된 task), 오버헤드도 크다.

계측 밖 변경(수동 DuckDB CLI, working/ 임시 스크립트)은 run 시작 스냅샷을
`state.json` 과 대조해 `external_change` 이벤트로 남긴다.

스냅샷을 못 찍는 경우(그 순간 다른 커넥션이 DB를 RW로 쥐고 있을 때. 예:
download_all 이 Ctrl-C 로 죽어 커넥션을 닫지 못한 채 빠져나오는 경로)에는
`run_end.snapshot = "unavailable"` 로 남고 변경 내역은 비어 있다. 이때도
`state.json` 기준선은 갱신하지 않으므로, 다음 run 시작(또는 `--check`)에서
그 변경이 `external_change` 로 잡힌다 — 정보가 사라지지는 않고 귀속만 늦어진다.
"""
import argparse
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

import duckdb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

# 출력 경로 — 모듈 전역으로 두어 --selftest 가 임시 디렉터리로 갈아끼울 수 있다.
LOG_PATH = config.AUDIT_LOG_PATH
STATE_PATH = config.AUDIT_STATE_PATH

# DB 파일 → 짧은 키. CLI --db 값이기도 하다.
DB_KEYS = {
    "raw": config.RAW_DB_PATH,
    "analysis": config.ANALYSIS_DB_PATH,
    "news_raw": config.NEWS_RAW_DB_PATH,
    "news_analysis": config.NEWS_ANALYSIS_DB_PATH,
}

# 행이 **쓰인 시각**을 담는 컬럼만 골라 쓴다. news_articles 의 published_at·
# dateline 처럼 기사 자체의 날짜인 컬럼을 잘못 집으면 델타가 무의미해지므로
# 휴리스틱(이름에 _at 포함 등) 대신 allow-list 로 고정한다. 앞쪽이 우선.
WRITE_TS_COLUMNS = (
    "extracted_at",     # bill_text, document_text
    "classified_at",    # bill_classifications, news_classifications
    "filtered_at",      # bill_ai_filter
    "ingested_at",      # news_articles
    "fetched_at",       # document_text, _progress
    "run_timestamp",    # subtopic_assignments
    "built_at",         # news_cleaning_runs
    "created_at",       # news_prompt_versions
    "released_at",      # prompt_versions
    "updated_at",
)

_MAX_ERROR_CHARS = 500


# ────────────────────────────────────────────────────────
# 유틸
# ────────────────────────────────────────────────────────

def _warn(msg: str) -> None:
    """감사 실패는 경고만 — 파이프라인은 계속된다."""
    try:
        sys.stderr.write(f"[db_audit] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — 경고조차 실패하면 조용히 포기
        pass


_GIT_SHA_CACHE: str | None | bool = False   # False = 아직 조회 안 함


def git_sha() -> str | None:
    """저장소 short SHA. git 이 없거나 저장소 밖이면 None."""
    global _GIT_SHA_CACHE
    if _GIT_SHA_CACHE is not False:
        return _GIT_SHA_CACHE  # type: ignore[return-value]
    sha = None
    try:
        r = subprocess.run(
            ["git", "-C", os.path.dirname(os.path.abspath(__file__)),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            sha = r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        sha = None
    _GIT_SHA_CACHE = sha
    return sha


def db_key(db_path: str) -> str:
    """DB 파일 경로 → 짧은 키 ('raw', 'news_analysis', …). 미등록이면 파일명."""
    ap = os.path.abspath(db_path)
    for key, path in DB_KEYS.items():
        if os.path.abspath(path) == ap:
            return key
    return os.path.splitext(os.path.basename(ap))[0]


def _now() -> datetime:
    """naive 로컬 시각 — DB의 TIMESTAMP 컬럼(CURRENT_TIMESTAMP 로 채워짐)과 같은 기준."""
    return datetime.now()


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


def _append_event(event: dict) -> None:
    """이벤트 1건을 JSONL 에 append + fsync. 실패해도 예외를 올리지 않는다."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, default=str)
        # O_APPEND 단일 write — 동시 실행(수집 + 분류)에도 줄이 섞이지 않는다.
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:  # noqa: BLE001
        _warn(f"이벤트 기록 실패(무시): {type(e).__name__}: {e}")


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 — 손상된 state 는 기준선만 잃는다
        _warn(f"state.json 읽기 실패(새로 시작): {type(e).__name__}: {e}")
        return {}


def _save_state(state: dict) -> None:
    """atomic replace — 중간에 죽어도 반쪽 파일이 남지 않는다."""
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = f"{STATE_PATH}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    except Exception as e:  # noqa: BLE001
        _warn(f"state.json 저장 실패(무시): {type(e).__name__}: {e}")


# ────────────────────────────────────────────────────────
# 스냅샷 (read-only, 짧게)
# ────────────────────────────────────────────────────────

@contextlib.contextmanager
def _ro_connection(db_path: str):
    """대상 DB read-only 커넥션. 열 수 없으면 None 을 내준다 (예외 없음).

    실패 사유는 대체로 둘 중 하나이고 둘 다 정상 동작이다:
      - 같은 프로세스가 이미 RW로 열어둠 (ConnectionException)
      - 다른 프로세스가 RW 락을 쥐고 있음 (IOException)
    어느 쪽이든 스냅샷만 포기하고 run 기록은 남긴다.
    """
    con = None
    try:
        if not os.path.isfile(db_path):
            yield None
            return
        try:
            con = duckdb.connect(db_path, read_only=True)
        except Exception as e:  # noqa: BLE001
            _warn(f"스냅샷 생략 — {os.path.basename(db_path)} 를 열 수 없음: "
                  f"{type(e).__name__}: {e}")
            yield None
            return
        yield con
    finally:
        if con is not None:
            with contextlib.suppress(Exception):
                con.close()


def _snapshot(con) -> dict:
    """{table: {rows, oid, sql_hash, ts_col}} — 현재 DB 상태.

    `database_name = current_database()` 로 ATTACH 된 카탈로그를,
    `schema_name = 'main'` 으로 FTS 내부 스키마(fts_main_speeches.*)를 배제한다.
    둘 중 하나라도 빠지면 남의 DB 테이블이 이 DB 이력에 섞이거나
    미수식 COUNT(*) 가 CatalogException 을 낸다.
    """
    tables = con.execute(
        """
        SELECT table_name, sql
        FROM duckdb_tables()
        WHERE database_name = current_database() AND schema_name = 'main'
        ORDER BY table_name
        """
    ).fetchall()

    ts_cols: dict[str, str] = {}
    try:
        rows = con.execute(
            """
            SELECT table_name, column_name
            FROM duckdb_columns()
            WHERE database_name = current_database() AND schema_name = 'main'
              AND data_type LIKE 'TIMESTAMP%'
            """
        ).fetchall()
        by_table: dict[str, set] = {}
        for tname, cname in rows:
            by_table.setdefault(tname, set()).add(cname)
        for tname, cnames in by_table.items():
            for candidate in WRITE_TS_COLUMNS:
                if candidate in cnames:
                    ts_cols[tname] = candidate
                    break
    except Exception as e:  # noqa: BLE001 — 타임스탬프 델타는 부가정보
        _warn(f"타임스탬프 컬럼 조회 실패(델타 생략): {type(e).__name__}: {e}")

    snap: dict[str, dict] = {}
    for tname, sql in tables:
        try:
            rows_n = con.execute(f'SELECT COUNT(*) FROM main."{tname}"').fetchone()[0]
        except Exception as e:  # noqa: BLE001 — 한 테이블 실패가 전체를 막지 않는다
            _warn(f"{tname} COUNT(*) 실패(건너뜀): {type(e).__name__}: {e}")
            continue
        snap[tname] = {
            "rows": int(rows_n),
            "sql_hash": hashlib.md5((sql or "").encode("utf-8")).hexdigest()[:12],
            "ts_col": ts_cols.get(tname),
        }
    return snap


def _touched_rows(con, table: str, ts_col: str, since: datetime) -> int | None:
    """since 이후 write 타임스탬프가 찍힌 행 수. 실패하면 None."""
    try:
        row = con.execute(
            f'SELECT COUNT(*) FROM main."{table}" WHERE "{ts_col}" >= ?',
            [since],
        ).fetchone()
        return int(row[0]) if row else None
    except Exception:  # noqa: BLE001 — 타입 불일치 등은 조용히 포기
        return None


def _progress_summary(con, since: datetime) -> list[dict] | None:
    """raw DB 전용 — 이번 run 이 다시 쓴 수집 task 를 API별로 집계."""
    try:
        rows = con.execute(
            """
            SELECT api_id, name_kr, COUNT(*) AS tasks, SUM(row_count) AS rows
            FROM main."_progress"
            WHERE fetched_at >= ?
            GROUP BY api_id, name_kr
            ORDER BY tasks DESC
            """,
            [since],
        ).fetchall()
    except Exception:  # noqa: BLE001 — _progress 가 없는 DB가 정상
        return None
    if not rows:
        return None
    return [{"api_id": a, "name_kr": n, "tasks": int(t), "rows": int(r or 0)}
            for a, n, t, r in rows]


def _diff(before: dict | None, after: dict | None) -> list[dict]:
    """스냅샷 두 장 비교 → 변경된 테이블만."""
    if before is None or after is None:
        return []
    changes = []
    for tname in sorted(set(before) | set(after)):
        b, a = before.get(tname), after.get(tname)
        if b is None:
            changes.append({"table": tname, "change": "table_created",
                            "rows_before": None, "rows_after": a["rows"],
                            "delta": a["rows"]})
            continue
        if a is None:
            changes.append({"table": tname, "change": "table_dropped",
                            "rows_before": b["rows"], "rows_after": None,
                            "delta": -b["rows"]})
            continue
        delta = a["rows"] - b["rows"]
        schema_changed = b.get("sql_hash") != a.get("sql_hash")
        if delta == 0 and not schema_changed:
            continue  # 변화 없음 — 타임스탬프 델타·mark_rebuilt 는 호출자가 채운다
        change = ("schema_changed" if schema_changed and delta == 0
                  else "rows_added" if delta > 0
                  else "rows_removed")
        entry = {"table": tname, "change": change,
                 "rows_before": b["rows"], "rows_after": a["rows"], "delta": delta}
        if schema_changed:
            entry["schema_changed"] = True
        changes.append(entry)
    return changes


# ────────────────────────────────────────────────────────
# audit_run
# ────────────────────────────────────────────────────────

class AuditRun:
    """한 번의 실행. `audit_run()` 컨텍스트가 만들어 준다."""

    def __init__(self, script: str, db_path: str, argv=None, note: str | None = None):
        self.db_path = os.path.abspath(db_path)
        self.db = db_key(db_path)
        self.script = _rel_script(script)
        self.argv = " ".join(argv) if isinstance(argv, (list, tuple)) else (argv or "")
        self.notes = [note] if note else []
        self.started_at = _now()
        self.run_id = (self.started_at.strftime("%Y%m%dT%H%M%S")
                       + f"_{os.getpid():d}_{self.db}")
        self.before: dict | None = None
        self.after: dict | None = None
        self.changes: list[dict] = []
        self.rebuilt: dict[str, str] = {}

    # -- 사용자 API ------------------------------------------------
    def note(self, text: str) -> None:
        """이 run 에 남길 자유 형식 메모 (예: cleaning_version, 대상 age)."""
        if text:
            self.notes.append(str(text))

    def mark_rebuilt(self, table: str, reason: str = "") -> None:
        """테이블을 통째로 다시 만들었다고 알린다 (DROP+CREATE / CREATE OR REPLACE).

        행 수가 그대로여도 내용이 전부 갈렸다는 사실은 스냅샷 비교로는 알 수
        없다(table_oid 는 위치 id 라 판별 불가 — 모듈 docstring 참조). 그런
        재작성을 하는 곳은 두 군데뿐이고 둘 다 자기가 무슨 짓을 했는지 알므로
        직접 선언하게 한다: news_cleaning 의 CTAS, build_news_db --rebuild.
        """
        if table:
            self.rebuilt[str(table)] = reason or "rebuilt"

    # -- 내부 ------------------------------------------------------
    def _start(self) -> None:
        _append_event({
            "event": "run_start",
            "run_id": self.run_id,
            "db": self.db,
            "db_path": self.db_path,
            "script": self.script,
            "argv": self.argv,
            "started_at": _iso(self.started_at),
            "git_sha": git_sha(),
            "host": socket.gethostname(),
            "pid": os.getpid(),
        })
        with _ro_connection(self.db_path) as con:
            if con is None:
                return
            try:
                self.before = _snapshot(con)
            except Exception as e:  # noqa: BLE001
                _warn(f"시작 스냅샷 실패(무시): {type(e).__name__}: {e}")
                return
        self._report_external_drift()

    def _report_external_drift(self) -> None:
        """계측 밖 변경 탐지 — 지난 스냅샷 이후 누가 조용히 바꿨는가."""
        if self.before is None:
            return
        state = _load_state()
        prev = state.get(self.db)
        if not prev or not prev.get("tables"):
            return
        drift = _diff(prev["tables"], self.before)
        if not drift:
            return
        _append_event({
            "event": "external_change",
            "run_id": self.run_id,
            "db": self.db,
            "detected_at": _iso(_now()),
            "since": prev.get("taken_at"),
            "note": "계측되지 않은 변경 (수동 SQL·working/ 스크립트 등)",
            "tables": drift,
        })

    def _finish(self, status: str, error: str | None) -> None:
        finished_at = _now()
        progress = None
        with _ro_connection(self.db_path) as con:
            if con is not None:
                try:
                    self.after = _snapshot(con)
                    self.changes = _diff(self.before, self.after)
                    changed = {c["table"] for c in self.changes}
                    # 이번 run 이 실제로 남긴 행 수 — 행 수가 그대로인 갱신도 잡는다.
                    for tname, meta in (self.after or {}).items():
                        ts_col = meta.get("ts_col")
                        if not ts_col:
                            continue
                        touched = _touched_rows(con, tname, ts_col, self.started_at)
                        if not touched:
                            continue
                        entry = next((c for c in self.changes if c["table"] == tname), None)
                        if entry is None:
                            before_rows = (self.before or {}).get(tname, {}).get("rows")
                            entry = {"table": tname, "change": "rows_updated",
                                     "rows_before": before_rows,
                                     "rows_after": meta["rows"], "delta": 0}
                            self.changes.append(entry)
                        entry["touched"] = touched
                        entry["ts_column"] = ts_col
                        changed.add(tname)
                    # 스크립트가 스스로 선언한 전면 재작성 (행 수가 같아도 기록)
                    for tname, reason in self.rebuilt.items():
                        entry = next((c for c in self.changes if c["table"] == tname), None)
                        if entry is None:
                            after_rows = (self.after or {}).get(tname, {}).get("rows")
                            before_rows = (self.before or {}).get(tname, {}).get("rows")
                            entry = {"table": tname, "change": "table_rebuilt",
                                     "rows_before": before_rows,
                                     "rows_after": after_rows,
                                     "delta": (after_rows - before_rows)
                                              if isinstance(after_rows, int)
                                              and isinstance(before_rows, int) else 0}
                            self.changes.append(entry)
                        entry["rebuilt"] = reason
                    self.changes.sort(key=lambda c: (-abs(c.get("delta") or 0), c["table"]))
                    progress = _progress_summary(con, self.started_at)
                except Exception as e:  # noqa: BLE001
                    _warn(f"종료 스냅샷 실패(무시): {type(e).__name__}: {e}")

        event = {
            "event": "run_end",
            "run_id": self.run_id,
            "db": self.db,
            "script": self.script,
            "argv": self.argv,
            "status": status,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(finished_at),
            "duration_s": round((finished_at - self.started_at).total_seconds(), 1),
            "git_sha": git_sha(),
            "changed_tables": len(self.changes),
            "tables": self.changes,
        }
        if self.before is None or self.after is None:
            event["snapshot"] = "unavailable"   # DB가 잠겨 있었음 → 변경 내역 불명
        if progress:
            event["collect_tasks"] = progress
        if self.notes:
            event["note"] = " | ".join(self.notes)
        if error:
            event["error"] = error[:_MAX_ERROR_CHARS]
        _append_event(event)

        if self.after is not None:
            state = _load_state()
            state[self.db] = {"taken_at": _iso(finished_at),
                              "db_path": self.db_path,
                              "run_id": self.run_id,
                              "tables": self.after}
            _save_state(state)


def _rel_script(script: str) -> str:
    """__file__ → 저장소 기준 상대경로 (로그 가독성)."""
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        return os.path.relpath(os.path.abspath(script), root)
    except Exception:  # noqa: BLE001
        return str(script)


@contextlib.contextmanager
def audit_run(script: str, db_path: str, *, argv=None, note: str | None = None):
    """DB를 바꾸는 구간을 감싼다. 어떤 경우에도 예외를 삼키지 않고 그대로 전파한다.

    status: ok | error | interrupted | exit:{code}
    """
    run = AuditRun(script, db_path, argv=argv, note=note)
    try:
        run._start()
    except Exception as e:  # noqa: BLE001 — 감사 시작 실패가 실행을 막으면 안 된다
        _warn(f"run 시작 기록 실패(무시): {type(e).__name__}: {e}")
    status, error = "ok", None
    try:
        yield run
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    except SystemExit as e:
        # sys.exit(0) 은 정상 종료. download_all:514(검증 실패) 등은 exit:1.
        code = e.code
        status = "ok" if not code else f"exit:{code}"
        raise
    except BaseException as e:  # noqa: BLE001 — 상태만 남기고 그대로 올린다
        status = "error"
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        try:
            run._finish(status, error)
        except Exception as e:  # noqa: BLE001
            _warn(f"run 마감 기록 실패(무시): {type(e).__name__}: {e}")


# ────────────────────────────────────────────────────────
# 조회 / CLI
# ────────────────────────────────────────────────────────

def read_events(db: str | None = None, since: str | None = None) -> list[dict]:
    """JSONL 전체를 읽어 이벤트 목록으로. 손상된 줄은 건너뛴다."""
    events = []
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if db and ev.get("db") != db:
                    continue
                if since:
                    ts = ev.get("finished_at") or ev.get("started_at") or ev.get("detected_at") or ""
                    if ts[:10] < since:
                        continue
                events.append(ev)
    except FileNotFoundError:
        return []
    return events


def check(db: str | None = None, record: bool = True) -> list[dict]:
    """지금 상태 vs state.json → 계측 밖 변경. record=True 면 이벤트로 남긴다."""
    state = _load_state()
    out = []
    for key, path in DB_KEYS.items():
        if db and key != db:
            continue
        with _ro_connection(path) as con:
            if con is None:
                continue
            try:
                current = _snapshot(con)
            except Exception as e:  # noqa: BLE001
                _warn(f"{key} 스냅샷 실패: {type(e).__name__}: {e}")
                continue
        prev = state.get(key)
        drift = _diff(prev["tables"], current) if prev and prev.get("tables") else []
        if drift:
            out.append({"db": key, "since": prev.get("taken_at"), "tables": drift})
            if record:
                _append_event({
                    "event": "external_change",
                    "run_id": None,
                    "db": key,
                    "detected_at": _iso(_now()),
                    "since": prev.get("taken_at"),
                    "note": "db_audit --check 로 탐지",
                    "tables": drift,
                })
        if record:
            state[key] = {"taken_at": _iso(_now()), "db_path": os.path.abspath(path),
                          "run_id": None if drift else (prev or {}).get("run_id"),
                          "tables": current}
    if record:
        _save_state(state)
    return out


def _fmt_table_line(t: dict) -> str:
    before, after = t.get("rows_before"), t.get("rows_after")
    b = f"{before:,}" if isinstance(before, int) else "-"
    a = f"{after:,}" if isinstance(after, int) else "-"
    delta = t.get("delta") or 0
    bits = [f"{t['table']:<28} {b:>12} → {a:<12}"]
    if delta:
        bits.append(f"({delta:+,})")
    if t.get("touched"):
        bits.append(f"이번 run 기록 {t['touched']:,}행 ({t.get('ts_column')})")
    if t.get("rebuilt"):
        bits.append(f"[전면 재작성: {t['rebuilt']}]")
    if t.get("schema_changed"):
        bits.append("[스키마 변경]")
    if t.get("change") == "table_created":
        bits.append("[신규]")
    elif t.get("change") == "table_dropped":
        bits.append("[삭제됨]")
    return "  " + " ".join(bits)


def cmd_log(args) -> None:
    events = read_events(args.db, args.since)
    runs = [e for e in events if e.get("event") == "run_end"]
    ext = [e for e in events if e.get("event") == "external_change"]
    if not args.all_runs:
        runs = [e for e in runs if e.get("changed_tables") or e.get("snapshot") == "unavailable"]
    merged = sorted(runs + ext,
                    key=lambda e: e.get("finished_at") or e.get("detected_at") or "")
    merged = merged[-args.limit:]
    if not merged:
        print("기록 없음. (변경이 있었던 run 만 표시합니다 — 전체는 --all-runs)")
        return
    for e in merged:
        if e.get("event") == "external_change":
            print(f"\n{e.get('detected_at', '?')}  ⚠ 계측 밖 변경  [{e.get('db')}]"
                  f"  (기준: {e.get('since')})")
            for t in e.get("tables", []):
                print(_fmt_table_line(t))
            continue
        status = e.get("status", "?")
        mark = {"ok": "✓"}.get(status, "✗")
        print(f"\n{e.get('finished_at', '?')}  {mark} {e.get('script')} {e.get('argv', '')}".rstrip()
              + f"  [{e.get('db')}]  {status}  {e.get('duration_s', '?')}s"
              + (f"  {e['git_sha']}" if e.get("git_sha") else ""))
        if e.get("snapshot") == "unavailable":
            print("  (DB가 잠겨 있어 스냅샷을 못 찍음 — 변경 내역 불명. "
                  "프로세스 종료 후 `db_audit.py --check` 로 확인 가능)")
        for t in e.get("tables", []):
            print(_fmt_table_line(t))
        for task in (e.get("collect_tasks") or [])[:10]:
            print(f"    수집 task {task['api_id']} ({task['name_kr']}): "
                  f"{task['tasks']:,}건 재수집, {task['rows']:,}행")
        if e.get("note"):
            print(f"  note: {e['note']}")
        if e.get("error"):
            print(f"  error: {e['error']}")


def cmd_runs(args) -> None:
    events = read_events(args.db, args.since)
    ends = {e.get("run_id"): e for e in events if e.get("event") == "run_end"}
    starts = [e for e in events if e.get("event") == "run_start"]
    starts = starts[-args.limit:]
    if not starts:
        print("기록 없음.")
        return
    print(f"{'시작':<20} {'스크립트':<38} {'DB':<14} {'상태':<10} {'소요':>8}  변경")
    for s in starts:
        end = ends.get(s.get("run_id"))
        script = (s.get("script", "?") + " " + s.get("argv", "")).strip()
        if end:
            status = end.get("status", "?")
            dur = f"{end.get('duration_s', 0)}s"
            changed = end.get("changed_tables", 0)
            changed_s = f"{changed} 테이블" if changed else "-"
            if end.get("snapshot") == "unavailable":
                changed_s = "불명(잠김)"
        else:
            status, dur, changed_s = "중단됨(기록 없음)", "-", "-"
        print(f"{s.get('started_at', '?'):<20} {script[:38]:<38} {s.get('db', '?'):<14} "
              f"{status:<10} {dur:>8}  {changed_s}")


def cmd_check(args) -> None:
    drifts = check(args.db, record=not args.no_record)
    if not drifts:
        print("계측 밖 변경 없음 (마지막 스냅샷과 동일).")
        return
    for d in drifts:
        print(f"\n⚠ [{d['db']}] {d.get('since')} 이후 계측되지 않은 변경:")
        for t in d["tables"]:
            print(_fmt_table_line(t))


def cmd_prune(args) -> None:
    if not args.older_than:
        print("--prune 은 --older-than YYYY-MM-DD 가 필요합니다.")
        return
    kept, dropped = [], 0
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    kept.append(line)     # 못 읽는 줄은 보존
                    continue
                ts = (ev.get("finished_at") or ev.get("started_at")
                      or ev.get("detected_at") or "")
                if ts[:10] < args.older_than:
                    dropped += 1
                else:
                    kept.append(line)
    except FileNotFoundError:
        print("로그 파일 없음.")
        return
    tmp = f"{LOG_PATH}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(kept)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LOG_PATH)
    print(f"{dropped:,}건 삭제, {len(kept):,}건 유지 → {LOG_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="DB 업데이트 감사 로그 조회/점검",
        epilog="MCP·DuckDB에서 직접: SELECT * FROM read_json_auto('"
               + config.AUDIT_LOG_PATH + "')")
    ap.add_argument("--log", action="store_true", help="변경 이력 (기본)")
    ap.add_argument("--runs", action="store_true", help="실행 목록")
    ap.add_argument("--check", action="store_true", help="계측 밖 변경 탐지")
    ap.add_argument("--prune", action="store_true", help="오래된 이벤트 삭제")
    ap.add_argument("--selftest", action="store_true", help="자가 검증 (임시 DB)")
    ap.add_argument("--db", choices=sorted(DB_KEYS), help="DB 필터")
    ap.add_argument("--since", help="YYYY-MM-DD 이후만")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--all-runs", action="store_true", help="변경 없는 run 도 표시")
    ap.add_argument("--older-than", help="--prune 기준일 YYYY-MM-DD")
    ap.add_argument("--no-record", action="store_true",
                    help="--check 시 이벤트·state 를 기록하지 않음")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())
    if args.runs:
        cmd_runs(args)
    elif args.check:
        cmd_check(args)
    elif args.prune:
        cmd_prune(args)
    else:
        cmd_log(args)


# ────────────────────────────────────────────────────────
# 자가 검증 — 계획서의 위험 경로를 임시 DB로 직접 재현
# ────────────────────────────────────────────────────────

def selftest() -> int:  # noqa: C901 — 검증 항목 나열이라 분기가 많다
    """python db_audit.py --selftest — 실패 개수를 exit code 로 반환."""
    import shutil
    import tempfile

    global LOG_PATH, STATE_PATH
    tmpdir = tempfile.mkdtemp(prefix="db_audit_selftest_")
    saved = (LOG_PATH, STATE_PATH)
    LOG_PATH = os.path.join(tmpdir, "db_updates.jsonl")
    STATE_PATH = os.path.join(tmpdir, "state.json")
    db = os.path.join(tmpdir, "scratch.duckdb")
    failures: list[str] = []

    def check_that(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(label)

    def last(event_type: str) -> dict:
        evs = [e for e in read_events() if e.get("event") == event_type]
        return evs[-1] if evs else {}

    def tables_of(ev: dict) -> dict:
        return {t["table"]: t for t in ev.get("tables", [])}

    try:
        con = duckdb.connect(db)
        con.execute("CREATE TABLE t (id INTEGER, extracted_at TIMESTAMP)")
        con.execute("INSERT INTO t VALUES (1, now()), (2, now())")
        con.close()

        print("① 정상 종료 + 행 추가")
        with audit_run("collect/fake.py", db, argv=["--x"]):
            c = duckdb.connect(db)
            c.execute("INSERT INTO t VALUES (3, now())")
            c.close()
        ev = last("run_end")
        t = tables_of(ev).get("t", {})
        check_that("status=ok", ev.get("status") == "ok", str(ev.get("status")))
        check_that("delta=+1", t.get("delta") == 1, str(t))
        check_that("touched=1 (타임스탬프 델타)", t.get("touched") == 1, str(t))

        print("② 예외 → status=error, 그때까지의 변경은 기록")
        try:
            with audit_run("collect/fake.py", db):
                c = duckdb.connect(db)
                c.execute("INSERT INTO t VALUES (4, now())")
                c.close()
                raise ValueError("boom")
        except ValueError:
            pass
        ev = last("run_end")
        check_that("status=error", ev.get("status") == "error", str(ev.get("status")))
        check_that("에러 메시지 보존", "boom" in (ev.get("error") or ""))
        check_that("변경 기록됨", tables_of(ev).get("t", {}).get("delta") == 1)

        print("③ KeyboardInterrupt → status=interrupted")
        try:
            with audit_run("collect/fake.py", db):
                raise KeyboardInterrupt
        except KeyboardInterrupt:
            pass
        check_that("status=interrupted", last("run_end").get("status") == "interrupted")

        print("④ sys.exit(1) → status=exit:1 / sys.exit(0) → ok")
        try:
            with audit_run("collect/fake.py", db):
                sys.exit(1)
        except SystemExit:
            pass
        check_that("status=exit:1", last("run_end").get("status") == "exit:1")
        try:
            with audit_run("collect/fake.py", db):
                sys.exit(0)
        except SystemExit:
            pass
        check_that("status=ok", last("run_end").get("status") == "ok")

        print("⑤ RW 커넥션이 열린 상태 — 스냅샷만 포기하고 예외 없이 진행")
        holder = duckdb.connect(db)
        try:
            with audit_run("collect/fake.py", db):
                holder.execute("INSERT INTO t VALUES (5, now())")
            ev = last("run_end")
            check_that("예외 없이 완료 + snapshot=unavailable",
                       ev.get("snapshot") == "unavailable", str(ev))
        finally:
            holder.close()

        print("⑥ 미완 트랜잭션이 열린 채 마감 — 원본 행이 살아있어야 한다")
        holder = duckdb.connect(db)
        before_rows = holder.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        holder.begin()
        holder.execute("DELETE FROM t")
        with audit_run("collect/fake.py", db):
            pass                      # 감사가 남의 트랜잭션을 확정시키면 안 된다
        holder.rollback()
        after_rows = holder.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        holder.close()
        check_that("행 보존 (남의 트랜잭션 미개입)",
                   before_rows == after_rows, f"{before_rows} → {after_rows}")

        print("⑦ 스키마 변경 / 테이블 신규 탐지")
        with audit_run("analyze/fake.py", db):
            c = duckdb.connect(db)
            c.execute("DROP TABLE t")
            c.execute("CREATE TABLE t (id INTEGER, extracted_at TIMESTAMP, extra VARCHAR)")
            c.execute("INSERT INTO t VALUES (1, now(), 'x')")
            c.execute("CREATE TABLE t2 (id INTEGER)")
            c.close()
        tbl = tables_of(last("run_end"))
        check_that("t 스키마 변경 탐지", tbl.get("t", {}).get("schema_changed") is True,
                   str(tbl.get("t")))
        check_that("t2 신규 탐지", tbl.get("t2", {}).get("change") == "table_created")

        print("⑧ 행 수·스키마 그대로인 전면 재작성 — mark_rebuilt 로 명시")
        with audit_run("analyze/fake.py", db) as run:
            c = duckdb.connect(db)
            c.execute("CREATE OR REPLACE TABLE t AS "
                      "SELECT 1 AS id, NULL::TIMESTAMP AS extracted_at, 'z' AS extra")
            c.close()
            run.mark_rebuilt("t", "CTAS 재빌드")
        t = tables_of(last("run_end")).get("t", {})
        check_that("재작성 기록 (delta=0 인데도)",
                   t.get("rebuilt") == "CTAS 재빌드" and t.get("delta") == 0, str(t))

        print("⑨ 행 수 불변 갱신(UPDATE) — 타임스탬프 델타로 포착")
        with audit_run("analyze/fake.py", db):
            c = duckdb.connect(db)
            c.execute("UPDATE t SET extracted_at = now(), extra = 'y'")
            c.close()
        t = tables_of(last("run_end")).get("t", {})
        check_that("delta=0 인데 touched>0", t.get("delta") == 0 and t.get("touched") == 1, str(t))

        print("⑩ 계측 밖 변경 → --check 가 탐지")
        c = duckdb.connect(db)
        c.execute("INSERT INTO t VALUES (99, now(), 'z')")
        c.close()
        saved_keys = dict(DB_KEYS)
        DB_KEYS.clear()
        DB_KEYS["scratch"] = db
        try:
            drifts = check(record=True)
        finally:
            DB_KEYS.clear()
            DB_KEYS.update(saved_keys)
        check_that("드리프트 탐지", bool(drifts) and drifts[0]["tables"][0]["delta"] == 1,
                   str(drifts))
        check_that("external_change 이벤트 기록", last("external_change").get("db") == "scratch")

        print("⑪ 파일이 통째로 교체돼도(.bak 복원) 로그는 살아남는다")
        bak = db + ".bak"
        shutil.copy2(db, bak)
        n_events_before = len(read_events())
        with audit_run("analyze/fake.py", db):
            c = duckdb.connect(db)
            c.execute("INSERT INTO t VALUES (1000, now(), 'gone')")
            c.close()
            shutil.copy2(bak, db)      # news_cleaning 의 복원 경로와 동일
        check_that("복원 후에도 이벤트 누적", len(read_events()) > n_events_before)
        c = duckdb.connect(db, read_only=True)
        ok = c.execute("SELECT COUNT(*) FROM t WHERE id = 1000").fetchone()[0] == 0
        c.close()
        check_that("복원본 무손상 (감사가 되쓰지 않음)", ok)

    finally:
        LOG_PATH, STATE_PATH = saved
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if failures:
        print(f"실패 {len(failures)}건: " + ", ".join(failures))
    else:
        print("전부 통과.")
    return len(failures)


if __name__ == "__main__":
    main()
