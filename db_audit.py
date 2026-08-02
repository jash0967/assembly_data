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
run 시작·종료에 read-only 스냅샷을 찍어 비교한다. 테이블마다 네 가지를 본다:

  1. `COUNT(*)`                     → rows_before / rows_after / delta
  2. `duckdb_tables().sql` 해시     → 컬럼 추가·타입 변경 탐지
  3. **내용 지문** (멀티셋 해시)     → 행 수·스키마가 그대로인 내용 교체 탐지
  4. **저장 지문** (물리 배치 해시)  → 내용까지 같은 재작성, 그리고 3을 건너뛴
                                       테이블의 대체 신호

여기에 행 단위 write 타임스탬프(`extracted_at`, `classified_at`, `ingested_at`
…)로 `COUNT(*) FILTER (WHERE ts >= run_started_at)` → **이번 run 이 실제로
남긴 행 수**를 더한다. 시도한 문(statement) 수가 아니라 DB에 남은 효과를
세므로 `INSERT OR IGNORE` 로 무시된 행·롤백된 행이 자동으로 빠진다.
raw DB는 `_progress` 도 같은 방식으로 조회해 어느 API의 몇 개 task가 다시
쓰였는지까지 남긴다 (파이프라인 트랜잭션과 함께 롤백되므로 정확).

**내용 지문 (3)** — `bit_xor(hash(cols…))`, `sum(hash(cols…))`, `COUNT(*)` 세
값의 조합. 행 순서에 무관한 멀티셋 해시라서, 같은 데이터를 순서만 바꿔 다시
쓰는 것(CTAS ORDER BY, DELETE+동일내용 재INSERT)을 변경으로 오인하지 않는다.

  - XOR **만으로는 안 된다**: 완전히 동일한 두 행이 서로 상쇄되어 XOR=0 이
    되는 것을 실측했다. 중복행이 있는 테이블에서 짝수 개 증감이 통째로
    사라진다. SUM(HUGEINT 누산이라 오버플로 없음)을 함께 둬서 막는다.
  - 컬럼을 **명시적으로 나열**한다. `hash(x) FROM tbl x` 처럼 행 별칭을 쓰면
    동명의 컬럼이 있을 때 별칭이 가려져 그 컬럼 하나만 해싱된다(실측). 나머지
    컬럼의 변경을 통째로 놓치는 조용한 오탐지 경로다.
  - 커밋됐지만 체크포인트가 안 끝난 쓰기(.wal 만 남기고 프로세스가 즉사한
    경우)도 read-only 재접속 시 WAL 이 재생되므로 그대로 잡힌다(실측).
  - BLOB·STRUCT·MAP·LIST·DECIMAL(38,10)·UUID·INTERVAL 전부 해시된다. 빈
    테이블은 XOR/SUM 이 NULL 이라 "0:0:0" 으로 정규화한다.
  - 지문에는 알고리즘 태그(`fp_algo`, duckdb 버전 포함)를 같이 저장한다.
    DuckDB 의 `hash()` 는 버전 간 안정성이 보장되지 않으므로, 태그가 다르면
    "변경됨"이 아니라 **"비교 불가"** 로 처리한다. 업그레이드 직후 전 테이블이
    변경으로 뜨는 사고를 막는다.

**비용과 그 처리** — assembly_raw(7.4GB, 41테이블) 전량 내용 해시는 10.0초,
그중 `document_text` 7.6초 + `bill_text` 1.9초가 대부분이고 나머지 39개 합이
0.6초다. 스냅샷은 run 당 2회이므로 전량 해시는 작은 실행에 20초를 얹는다.
그래서 **직전 실행에서 측정된 테이블별 소요시간을 state.json 에 남겨 두고,
`config.AUDIT_CONTENT_HASH_BUDGET_S`(기본 1.0초)를 넘는 테이블은 다음 실행부터
내용 해시를 건너뛴다.** 처음 보는 테이블은 항상 해시해서 비용을 학습한다.
한 run 안에서 시작·종료 스냅샷은 **같은 판단 기준(같은 hint 사전)** 을 쓰므로
"시작만 해시하고 종료는 건너뛰어 비교 불능" 이 되지 않는다.
건너뛴 테이블은 `content=null, content_skip="cost"` 로 남고, 비교 결과는
"변경 없음"이 아니라 **`content_unknown`** 이 된다 — 조용한 누락이 없다.
그리고 이 비싼 두 테이블은 마침 write 타임스탬프(`extracted_at`/`fetched_at`)를
가지고 있어 5번 신호가, 저장 지문(4)이 각각 독립적으로 덮는다.

크기는 비용 대리지표가 못 된다: `document_text` 는 storage_info 기준 38블록
(10MB)인데 7.6초가 걸린다(긴 문자열이 out-of-line 저장이라 블록에 안 잡힘).
그래서 바이트 임계가 아니라 **측정된 시간**으로 게이트한다.

**저장 지문 (4)** — `pragma_storage_info()` 전체(row_group·segment·block_id·
compression·stats)의 md5. 41테이블 전량 0.08초로 사실상 공짜다. 실측 확인:
RW로 열었다 아무것도 안 하고 닫아도(체크포인트 발생) 불변, 한 테이블에 쓰면
그 테이블만 움직이고 무관한 테이블은 흔들리지 않는다, 긴 텍스트 내부만 바꾼
경우에도 움직인다. 내용까지 동일한 재작성(CTAS 재빌드, DELETE+동일내용
재INSERT)은 저장 지문만 움직이므로 `rewritten_identical` 로 따로 분류해
"다시 쓰긴 했으나 데이터는 그대로"를 "손대지 않음"과 구별한다. 이건 실질
변경이 아니므로 `changed_tables` 에 세지 않고 `--log` 기본 목록도 채우지 않는다.

`duckdb_tables().table_oid` 로 재작성을 탐지하려던 초기 설계는 폐기했다 —
실측 결과 oid 는 카탈로그 로드 순서로 재부여되는 위치 id 라서 DB를 다시
열기만 해도 값이 바뀌고(20625 ↔ 22140), 커넥션이 다른 두 스냅샷 사이에서는
"재생성됐다/아니다"를 전혀 판별하지 못한다. `mark_rebuilt()` 는 이제 탐지에
필수가 아니고(3·4번이 자동으로 잡는다) **사람이 읽을 사유를 붙이는 용도**로만
남는다 — 호출하던 곳은 그대로 두면 된다.

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

# 내용 지문 알고리즘 태그. DuckDB 의 hash() 는 버전 간 안정성이 보장되지 않으므로
# 버전을 태그에 넣고, 태그가 다른 두 지문은 "다르다"가 아니라 "비교 불가"로 본다.
FP_ALGO = f"xor+sum+count/duckdb{duckdb.__version__}"

# 빈 테이블은 bit_xor/sum 이 NULL — 고정 문자열로 정규화한다.
_EMPTY_FP = "0:0:0"


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


def _q(ident: str) -> str:
    """SQL 식별자 인용. 내부 큰따옴표는 중복으로 이스케이프."""
    return '"' + str(ident).replace('"', '""') + '"'


def _lit(text: str) -> str:
    """SQL 문자열 리터럴 인용 (pragma_storage_info 인자용)."""
    return "'" + str(text).replace("'", "''") + "'"


def _storage_fp(con, table: str) -> str | None:
    """물리 배치 지문 — row_group·segment·block_id·compression·stats 전체의 md5.

    전 테이블 합쳐 0.1초 수준이라 항상 찍는다. 논리 내용이 같아도 다시 쓰면
    움직이므로, 내용 지문이 "같다"고 할 때 '재작성했으나 데이터는 동일'과
    '손대지 않음'을 가른다. 내용 해시를 비용 때문에 건너뛴 테이블에서는
    유일한 변경 신호가 된다.
    """
    try:
        row = con.execute(
            "SELECT md5(string_agg(_si::VARCHAR, '|' ORDER BY _si::VARCHAR)) "
            f"FROM pragma_storage_info({_lit(table)}) _si"
        ).fetchone()
    except Exception:  # noqa: BLE001 — 뷰·임시테이블 등은 storage_info 가 없다
        return None
    return row[0] if row and row[0] else None


def _content_fp(con, table: str, cols: list[str]) -> tuple[str | None, float | None]:
    """행 순서에 무관한 멀티셋 내용 지문 → ("xor:sum:count", 소요초).

    컬럼을 명시적으로 나열하는 것이 중요하다. `hash(x) FROM tbl x` 로 행 별칭을
    쓰면 동명의 컬럼이 있을 때 별칭이 가려져 그 컬럼만 해싱된다(실측).
    XOR 단독은 동일한 두 행이 상쇄되므로 SUM 을 함께 쓴다.
    """
    if not cols:
        return None, None
    expr = "hash(" + ", ".join(_q(c) for c in cols) + ")"
    t0 = time.perf_counter()
    try:
        row = con.execute(
            f"SELECT bit_xor({expr})::VARCHAR, sum({expr})::VARCHAR, COUNT(*) "
            f"FROM main.{_q(table)}"
        ).fetchone()
    except Exception as e:  # noqa: BLE001 — 해시 불가 타입 등은 비교 불가로 남긴다
        _warn(f"{table} 내용 지문 실패(비교 불가로 기록): {type(e).__name__}: {e}")
        return None, None
    elapsed = time.perf_counter() - t0
    if not row:
        return None, elapsed
    x, s, n = row
    if not n:
        return _EMPTY_FP, elapsed
    return f"{x or 0}:{s or 0}:{n}", elapsed


def _snapshot(con, cost_hints: dict | None = None) -> dict:
    """{table: {rows, sql_hash, ts_col, storage, content, fp_algo, …}} — 현재 DB 상태.

    `database_name = current_database()` 로 ATTACH 된 카탈로그를,
    `schema_name = 'main'` 으로 FTS 내부 스키마(fts_main_speeches.*)를 배제한다.
    둘 중 하나라도 빠지면 남의 DB 테이블이 이 DB 이력에 섞이거나
    미수식 COUNT(*) 가 CatalogException 을 낸다.

    cost_hints: {table: 직전 측정 소요초}. 예산(config.AUDIT_CONTENT_HASH_BUDGET_S)을
    넘긴 테이블은 내용 해시를 건너뛴다. 처음 보는 테이블은 항상 해시해 비용을
    학습한다. 한 run 의 시작·종료 스냅샷에 **같은 사전**을 넘겨야 비교가 성립한다.
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
    all_cols: dict[str, list[str]] = {}
    try:
        rows = con.execute(
            """
            SELECT table_name, column_name, data_type, column_index
            FROM duckdb_columns()
            WHERE database_name = current_database() AND schema_name = 'main'
            ORDER BY table_name, column_index
            """
        ).fetchall()
        by_table: dict[str, set] = {}
        for tname, cname, dtype, _idx in rows:
            all_cols.setdefault(tname, []).append(cname)
            if str(dtype).upper().startswith("TIMESTAMP"):
                by_table.setdefault(tname, set()).add(cname)
        for tname, cnames in by_table.items():
            for candidate in WRITE_TS_COLUMNS:
                if candidate in cnames:
                    ts_cols[tname] = candidate
                    break
    except Exception as e:  # noqa: BLE001 — 컬럼 조회 실패 시 지문 없이 진행
        _warn(f"컬럼 조회 실패(내용 지문·타임스탬프 델타 생략): {type(e).__name__}: {e}")

    budget = _content_budget()
    hints = cost_hints or {}
    snap: dict[str, dict] = {}
    for tname, sql in tables:
        try:
            rows_n = con.execute(f"SELECT COUNT(*) FROM main.{_q(tname)}").fetchone()[0]
        except Exception as e:  # noqa: BLE001 — 한 테이블 실패가 전체를 막지 않는다
            _warn(f"{tname} COUNT(*) 실패(건너뜀): {type(e).__name__}: {e}")
            continue
        entry: dict = {
            "rows": int(rows_n),
            "sql_hash": hashlib.md5((sql or "").encode("utf-8")).hexdigest()[:12],
            "ts_col": ts_cols.get(tname),
            "storage": _storage_fp(con, tname),
        }
        cols = all_cols.get(tname) or []
        prev_cost = hints.get(tname)
        if budget <= 0:
            entry["content"], entry["content_skip"] = None, "disabled"
        elif isinstance(prev_cost, (int, float)) and prev_cost > budget:
            entry["content"] = None
            entry["content_skip"] = "cost"
            entry["fp_cost"] = round(float(prev_cost), 3)   # 학습한 비용은 유지
        elif not cols:
            entry["content"], entry["content_skip"] = None, "no_columns"
        else:
            fp, elapsed = _content_fp(con, tname, cols)
            if fp is None:
                entry["content"], entry["content_skip"] = None, "error"
            else:
                entry["content"] = fp
                entry["fp_algo"] = FP_ALGO
            if elapsed is not None:
                entry["fp_cost"] = round(elapsed, 3)
        snap[tname] = entry
    return snap


def _content_budget() -> float:
    """테이블당 내용 해시 예산(초). 설정이 깨져 있으면 기본 1.0."""
    try:
        return float(getattr(config, "AUDIT_CONTENT_HASH_BUDGET_S", 1.0))
    except (TypeError, ValueError):
        return 1.0


def _cost_hints(state: dict, db: str) -> dict:
    """state.json 에 남은 테이블별 측정 소요시간 → 다음 스냅샷의 게이트 입력."""
    prev = (state or {}).get(db) or {}
    out = {}
    for tname, meta in (prev.get("tables") or {}).items():
        cost = (meta or {}).get("fp_cost")
        if isinstance(cost, (int, float)):
            out[tname] = float(cost)
    return out


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


# 실질 변경이 아닌 분류 — changed_tables 집계와 --log 기본 목록에서 제외한다.
_SOFT_CHANGES = frozenset({"rewritten_identical"})


def _cmp_fp(b: dict, a: dict, key: str) -> str:
    """지문 두 개 비교 → 'same' | 'differ' | 'unknown'.

    한쪽이라도 없으면(비용으로 건너뜀·해시 실패·구버전 state) 'unknown'.
    내용 지문은 알고리즘 태그가 다르면 값이 달라도 'unknown' — DuckDB 를
    올리면 hash() 결과가 바뀔 수 있어 전 테이블 오탐지가 되기 때문이다.
    """
    bv, av = b.get(key), a.get(key)
    if bv is None or av is None:
        return "unknown"
    if key == "content" and b.get("fp_algo") != a.get("fp_algo"):
        return "unknown"
    return "same" if bv == av else "differ"


def _diff(before: dict | None, after: dict | None) -> list[dict]:
    """스냅샷 두 장 비교 → 변경된 테이블만.

    행 수·스키마가 그대로여도 내용 지문이 다르면 `content_changed`,
    내용은 같은데 물리 배치만 다르면 `rewritten_identical`,
    내용 비교가 불가능한데 물리 배치가 움직였으면 `content_unknown` 이다.
    마지막 것이 핵심 — "모르는 것"을 "변경 없음"으로 접지 않는다.
    """
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
        content = _cmp_fp(b, a, "content")
        storage = _cmp_fp(b, a, "storage")

        if delta > 0:
            change = "rows_added"
        elif delta < 0:
            change = "rows_removed"
        elif schema_changed:
            change = "schema_changed"
        elif content == "differ":
            change = "content_changed"
        elif content == "same":
            # 내용이 확실히 같다 — 물리적으로 다시 썼는지만 남는다.
            change = "rewritten_identical" if storage == "differ" else None
        else:  # content == "unknown"
            change = "content_unknown" if storage == "differ" else None

        if change is None:
            continue  # 변화 없음 — 타임스탬프 델타·mark_rebuilt 는 호출자가 채운다
        entry = {"table": tname, "change": change,
                 "rows_before": b["rows"], "rows_after": a["rows"], "delta": delta}
        if schema_changed:
            entry["schema_changed"] = True
        if content != "same":
            entry["content_cmp"] = content
        if a.get("content_skip"):
            entry["content_skip"] = a["content_skip"]
        changes.append(entry)
    return changes


def _substantive(changes: list[dict]) -> list[dict]:
    """실질 변경만 (물리 재작성뿐인 항목 제외)."""
    return [c for c in changes if c.get("change") not in _SOFT_CHANGES]


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
        # 시작·종료 스냅샷이 같은 게이트 판단을 하도록 run 시작 시점에 한 번만
        # 읽어 고정한다. 중간에 갱신하면 "시작만 해시하고 종료는 건너뜀"이 된다.
        self._hints: dict = {}

    # -- 사용자 API ------------------------------------------------
    def note(self, text: str) -> None:
        """이 run 에 남길 자유 형식 메모 (예: cleaning_version, 대상 age)."""
        if text:
            self.notes.append(str(text))

    def mark_rebuilt(self, table: str, reason: str = "") -> None:
        """테이블을 통째로 다시 만들었다고 알린다 (DROP+CREATE / CREATE OR REPLACE).

        **탐지에는 더 이상 필요하지 않다.** 내용 지문·저장 지문이 행 수가 같은
        재작성을 자동으로 잡는다(모듈 docstring 참조). 이 호출은 사람이 읽을
        사유("CTAS 재빌드" 같은)를 로그에 붙이고, 내용까지 동일해 기본 목록에서
        빠질 재작성을 실질 변경으로 승격시키는 용도로 남는다.
        기존 호출부(news_cleaning 의 CTAS, build_news_db --rebuild)는 그대로 둔다.
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
        self._hints = _cost_hints(_load_state(), self.db)
        with _ro_connection(self.db_path) as con:
            if con is None:
                return
            try:
                self.before = _snapshot(con, self._hints)
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
        drift = _substantive(_diff(prev["tables"], self.before))
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
                    self.after = _snapshot(con, self._hints)
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
                        elif entry.get("change") in _SOFT_CHANGES:
                            # 물리 재작성뿐인 줄 알았는데 이번 run 이 남긴 행이 있다
                            # → 실질 변경으로 승격 (기본 목록에서 빠지면 안 된다).
                            entry["change"] = "rows_updated"
                        entry["touched"] = touched
                        entry["ts_column"] = ts_col
                        changed.add(tname)
                    # 스크립트가 스스로 붙인 재작성 사유 (탐지는 지문이 이미 했다)
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
                        elif entry.get("change") in _SOFT_CHANGES:
                            entry["change"] = "table_rebuilt"   # 선언이 있으면 실질 변경
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
            # 실질 변경만 센다 — 내용까지 동일한 물리 재작성은 rewritten_tables 로.
            "changed_tables": len(_substantive(self.changes)),
            "tables": self.changes,
        }
        rewritten = [c["table"] for c in self.changes
                     if c.get("change") in _SOFT_CHANGES]
        if rewritten:
            event["rewritten_tables"] = rewritten
        skipped = sorted(t for t, m in (self.after or {}).items()
                         if m.get("content_skip") == "cost")
        if skipped:
            # 무엇을 안 봤는지 반드시 남긴다 — 조용한 축소 금지.
            event["content_hash_skipped"] = skipped
            event["content_hash_budget_s"] = _content_budget()
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
                current = _snapshot(con, _cost_hints(state, key))
            except Exception as e:  # noqa: BLE001
                _warn(f"{key} 스냅샷 실패: {type(e).__name__}: {e}")
                continue
        prev = state.get(key)
        # 내용까지 동일한 물리 재작성은 데이터 변경이 아니므로 드리프트로 안 센다.
        drift = _substantive(_diff(prev["tables"], current)) if prev and prev.get("tables") else []
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
    change = t.get("change")
    bits = [f"{t['table']:<28} {b:>12} → {a:<12}"]
    if delta:
        bits.append(f"({delta:+,})")
    if change == "content_changed" and not t.get("touched"):
        # 타임스탬프 델타가 이미 "이번 run 이 N행 남겼다"고 말해 주는 경우엔
        # 표시하지 않는다. 지문이 **유일한** 근거일 때만 눈에 띄게 남긴다.
        bits.append("★ 내용 변경 (행 수 동일)")
    elif change == "content_unknown":
        why = {"cost": "비용 초과로 내용 해시 생략",
               "disabled": "내용 해시 비활성",
               "error": "내용 해시 실패",
               "no_columns": "컬럼 없음"}.get(t.get("content_skip"), "내용 비교 불가")
        bits.append(f"? 물리 재작성 감지 — 내용 확인 불가 ({why})")
    elif change == "rewritten_identical":
        bits.append("· 재작성됐으나 내용 동일")
    if t.get("touched"):
        bits.append(f"이번 run 기록 {t['touched']:,}행 ({t.get('ts_column')})")
    if t.get("rebuilt"):
        bits.append(f"[전면 재작성: {t['rebuilt']}]")
    if t.get("schema_changed"):
        bits.append("[스키마 변경]")
    if change == "table_created":
        bits.append("[신규]")
    elif change == "table_dropped":
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
        if e.get("content_hash_skipped"):
            print(f"  (내용 해시 생략 — 테이블당 {e.get('content_hash_budget_s')}초 예산 초과: "
                  + ", ".join(e["content_hash_skipped"])
                  + ". 저장 지문·타임스탬프 델타로만 관측)")
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
            n_rw = len(end.get("rewritten_tables") or [])
            if n_rw:
                changed_s += f" (+재작성 {n_rw})" if changed else f"재작성만 {n_rw}"
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

        print("⑧-b 선언 없는 전면 재작성 — 내용 지문이 자동으로 잡는다")
        c = duckdb.connect(db)
        c.execute("CREATE OR REPLACE TABLE big AS "
                  "SELECT i AS id, ('v' || i) AS txt FROM range(3000) s(i)")
        c.close()
        with audit_run("analyze/fake.py", db):     # 기준선 확보용 no-op run
            pass
        with audit_run("analyze/fake.py", db):     # mark_rebuilt 없음
            c = duckdb.connect(db)
            c.execute("CREATE OR REPLACE TABLE big AS "
                      "SELECT i AS id, ('CHANGED' || i) AS txt FROM range(3000) s(i)")
            c.close()
        ev = last("run_end")
        t = tables_of(ev).get("big", {})
        check_that("mark_rebuilt 없이 content_changed 탐지",
                   t.get("change") == "content_changed" and t.get("delta") == 0, str(t))
        check_that("실질 변경으로 집계", ev.get("changed_tables", 0) >= 1, str(ev.get("changed_tables")))

        print("⑧-c 행 순서만 바꾼 재작성 — 오탐지하면 안 된다")
        with audit_run("analyze/fake.py", db):
            c = duckdb.connect(db)
            c.execute("CREATE OR REPLACE TABLE big AS SELECT * FROM big ORDER BY id DESC")
            c.close()
        ev = last("run_end")
        t = tables_of(ev).get("big", {})
        check_that("순서만 바뀌면 content_changed 아님",
                   t.get("change") != "content_changed", str(t))
        check_that("재작성 사실은 남음 (rewritten_identical)",
                   t.get("change") == "rewritten_identical", str(t))
        check_that("실질 변경으로는 안 셈", ev.get("changed_tables") == 0,
                   str(ev.get("changed_tables")))

        print("⑧-d DELETE + 동일내용 재INSERT (download_all.save_rows 패턴)")
        with audit_run("collect/fake.py", db):
            c = duckdb.connect(db)
            c.execute("CREATE OR REPLACE TABLE _bak AS SELECT * FROM big")
            c.execute("DELETE FROM big")
            c.execute("INSERT INTO big SELECT * FROM _bak")
            c.execute("DROP TABLE _bak")
            c.close()
        t = tables_of(last("run_end")).get("big", {})
        check_that("동일내용 재삽입은 content_changed 아님",
                   t.get("change") != "content_changed", str(t))

        print("⑧-e DELETE + 다른내용 재INSERT (실제로 놓쳤던 케이스)")
        with audit_run("collect/fake.py", db):
            c = duckdb.connect(db)
            c.execute("CREATE OR REPLACE TABLE _bak AS SELECT id, 'NEW' AS txt FROM big")
            c.execute("DELETE FROM big")
            c.execute("INSERT INTO big SELECT * FROM _bak")
            c.execute("DROP TABLE _bak")
            c.close()
        t = tables_of(last("run_end")).get("big", {})
        check_that("행 수 동일 + 내용 교체 → content_changed",
                   t.get("change") == "content_changed" and t.get("delta") == 0, str(t))

        print("⑧-f 중복행 상쇄 — XOR 단독이면 놓치는 경우")
        c = duckdb.connect(db)
        c.execute("CREATE OR REPLACE TABLE dup AS SELECT 1 AS a UNION ALL SELECT 1")
        c.close()
        with audit_run("analyze/fake.py", db):
            pass                                   # 기준선
        with audit_run("analyze/fake.py", db):
            c = duckdb.connect(db)
            c.execute("CREATE OR REPLACE TABLE dup AS SELECT 2 AS a UNION ALL SELECT 2")
            c.close()
        t = tables_of(last("run_end")).get("dup", {})
        check_that("중복 2행 → 다른 중복 2행 탐지 (SUM 항이 있어야 통과)",
                   t.get("change") == "content_changed", str(t))

        print("⑧-g 다중 컬럼 — 첫 컬럼 외의 변경도 잡는가 (별칭 가림 회귀)")
        c = duckdb.connect(db)
        c.execute("CREATE OR REPLACE TABLE multi AS "
                  "SELECT i AS x, i AS y, i AS z FROM range(500) s(i)")
        c.close()
        with audit_run("analyze/fake.py", db):
            pass
        with audit_run("analyze/fake.py", db):
            c = duckdb.connect(db)
            c.execute("UPDATE multi SET z = z + 1")   # 첫 컬럼 x 는 그대로
            c.close()
        t = tables_of(last("run_end")).get("multi", {})
        check_that("마지막 컬럼만 바뀌어도 탐지", t.get("change") == "content_changed", str(t))

        print("⑧-h 내용 해시 생략 시 — '변경 없음'이 아니라 '확인 불가'")
        saved_budget = config.AUDIT_CONTENT_HASH_BUDGET_S
        try:
            with audit_run("analyze/fake.py", db):
                pass                                # 기준선 (예산 정상)
            # 직전 측정 비용을 넘도록 예산을 극단적으로 낮춘다
            config.AUDIT_CONTENT_HASH_BUDGET_S = 1e-9
            with audit_run("analyze/fake.py", db):
                c = duckdb.connect(db)
                c.execute("UPDATE multi SET z = z + 100")
                c.close()
            ev = last("run_end")
            t = tables_of(ev).get("multi", {})
            check_that("생략된 테이블은 content_unknown",
                       t.get("change") == "content_unknown", str(t))
            check_that("생략 사실이 로그에 남음",
                       "multi" in (ev.get("content_hash_skipped") or []),
                       str(ev.get("content_hash_skipped")))
        finally:
            config.AUDIT_CONTENT_HASH_BUDGET_S = saved_budget

        print("⑧-i 지문 알고리즘 태그가 다르면 '변경'이 아니라 '비교 불가'")
        fake_b = {"rows": 1, "sql_hash": "x", "storage": "s1",
                  "content": "1:1:1", "fp_algo": "old"}
        fake_a = {"rows": 1, "sql_hash": "x", "storage": "s2",
                  "content": "9:9:9", "fp_algo": "new"}
        d = _diff({"z": fake_b}, {"z": fake_a})
        check_that("태그 불일치 → content_unknown",
                   d and d[0]["change"] == "content_unknown", str(d))

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
