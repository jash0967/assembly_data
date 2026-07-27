"""국회 데이터 DuckDB MCP 서버.

Claude Code에서 자연어로 국회 데이터를 질의할 수 있도록
DuckDB 접근 도구를 제공하는 MCP 서버.

DB 분리 (2026-05-09): assembly_analysis.duckdb를 메인으로 열고
assembly_raw.duckdb를 `raw` 별칭으로 ATTACH (read-only). 사용자는
analysis 측 테이블은 무수식, raw 측은 `raw.<table>`로 접근.

RAG 통합 stdout 보호:
  MCP는 stdin/stdout으로 JSON-RPC 통신하는데, bm25s·sentence-transformers·
  huggingface_hub 등이 stdout에 progress·warning을 직접 print해서 protocol
  깨질 수 있음. (1) 환경변수로 progress bar 차단, (2) 노이즈 모듈을
  FastMCP 시작 전에 미리 import해서 startup 노이즈가 mcp.run() 전에 끝나도록.

경로 환경변수 (§정본 문서 — 2026-07-27 추가):
  데이터·인덱스를 저장소 밖(다른 드라이브·다른 OS)에 두고 서빙할 수 있도록
  아래 변수로 경로를 덮어쓸 수 있다. **하나도 설정하지 않으면 기존과 완전히
  동일한 경로**(저장소 기준 상대 위치)를 쓴다.

    ASSEMBLY_DATA_DIR               data/ 루트. 아래 4개 DB의 기본 부모.
                                    (data/bills_kr, data/news 하위 구조는 유지)
    ASSEMBLY_RAW_DB_PATH            assembly_raw.duckdb        (파일)
    ASSEMBLY_ANALYSIS_DB_PATH       assembly_analysis.duckdb   (파일)
    ASSEMBLY_NEWS_RAW_DB_PATH       news.duckdb                (파일)
    ASSEMBLY_NEWS_ANALYSIS_DB_PATH  news_analysis.duckdb       (파일)
    RAG_DATA_DIR                    RAG 인덱스 루트 (lance_db/ · bm25/ ·
                                    manifest.sqlite · embed_config.json 의 부모).
                                    rag_assembly/config.py 가 **같은 변수를 같은
                                    규칙으로** 읽으므로 서버 진단과 실제 검색
                                    경로가 갈라지지 않는다.

  명명 규칙: 이 모듈의 경로 상수 이름 앞에 `ASSEMBLY_` 를 붙인 것
  (RAW_DB_PATH → ASSEMBLY_RAW_DB_PATH). RAG 인덱스만 rag_assembly 네임스페이스를
  따라 `RAG_` (같은 폴더의 RAG_EMBED_DEVICE 와 동일 계열).
  개별 DB 변수가 ASSEMBLY_DATA_DIR 보다 우선한다. 값은 ~ 와 $VAR/%VAR% 확장 후
  절대경로화된다. 상대경로는 프로세스 CWD 기준이라 권장하지 않는다.
  시작 시 각 경로의 존재 여부를 stderr 한 줄씩 진단 출력한다
  (`python duckdb_mcp_server.py --paths` 로 서버를 띄우지 않고 확인 가능).

  그 밖의 동작 환경변수: MCP_BM25_LOAD_DELAY, MCP_EMBED_WARMUP,
  MCP_EMBED_SUBPROC, RAG_EMBED_DEVICE.

Windows에서 서빙할 때 (2026-07-27):
  - `PYTHONUTF8=1` 을 권장한다. 이 파일은 stderr를 UTF-8로 고정하고
    _subproc_embed.py 도 stdin/stdout을 UTF-8로 고정하지만, 서드파티가 여는
    파일까지는 보장할 수 없다 (cp949 기본 인코딩 사고 예방).
  - stdout(JSON-RPC)은 mcp SDK가 sys.stdout.buffer를 UTF-8 TextIOWrapper로
    직접 감싸므로 로케일과 무관하다.
  - BM25 **빌드**(bm25.py::build)는 multiprocessing fork 전제라 Windows에서
    쓰지 않는다. 서빙(load/search)은 fork를 쓰지 않으므로 무관.
"""
import os
import sys

# Windows(cp949 로케일)에서 한국어·기호가 섞인 진단 로그가 UnicodeEncodeError를
# 내며 스레드를 죽이는 것을 막는다. stderr만 손댄다 — stdout은 FastMCP가
# sys.stdout.buffer를 직접 감싸 protocol 채널로 쓰므로 절대 건드리지 않는다.
# (Linux/UTF-8 환경에서는 사실상 no-op.)
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — 진단 채널 설정 실패가 서버를 막으면 안 된다
    pass

# (1) 진행률·로그 출력 차단 (FastMCP import 전에 설정해야 효과)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TQDM_DISABLE", "1")
# HF Hub HEAD request로 캐시 신선도 검증하면 FastMCP context에서 hang한다.
# 그 차단은 이제 **로드 호출 단위**로 한다 —
# rag_assembly/embedder.py::_build()가 고정 revision 스냅샷이 캐시에 있으면
# SentenceTransformer(..., local_files_only=True)로 연다 (이 프로세스에서
# HF 모델을 로드하는 유일한 경로다).
# 여기서 HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE을 프로세스 전역으로 켜지 않는 이유:
# huggingface_hub이 import 시점에 그 값을 모듈 상수로 굳혀서, 나중에 되돌려도
# 이 프로세스의 **다른 모든** HF 모델(미캐시 모델 포함)이 영구히 다운로드 불가가
# 되고 원인을 알기 어려운 offline 에러로 나타난다.
# (호스트가 명시적으로 HF_HUB_OFFLINE=1을 넘기면 그건 그대로 존중된다.)

# logging 레벨 (HF 비인증 warning 등 차단)
import logging as _logging
_logging.getLogger("huggingface_hub").setLevel(_logging.ERROR)
_logging.getLogger("transformers").setLevel(_logging.ERROR)
_logging.getLogger("sentence_transformers").setLevel(_logging.ERROR)

# (2) 노이즈를 stdout 대신 stderr로 흘려보낸 뒤 RAG 라이브러리 사전 import.
#     mcp.run()이 시작된 후에 stdout으로 출력되면 protocol이 깨지므로,
#     startup 시점에 일괄로 처리.
_PREIMPORTED_API = None  # 아래 preimport가 성공하면 dict로 교체
_PREIMPORT_GET_ENGINE = None  # api._get_engine (BM25 백그라운드 로더가 사용)
_saved_stdout_fd = os.dup(1)
os.dup2(2, 1)  # stdout fd → stderr fd
try:
    _rag_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "rag_assembly")
    if _rag_path not in sys.path:
        sys.path.insert(0, _rag_path)
    try:
        import _bootstrap  # noqa: F401
        # 노이즈 출력 가능성 큰 라이브러리들 미리 import — 캐시되면 이후 조용
        import bm25s  # prints "resource module not available on Windows"
        from api import (search, search_bills, search_bill_metas,
                          search_speeches, search_documents,
                          search_members, lookup_member_by_name,
                          lookup_bill_by_id, stats, _get_engine)
        _PREIMPORTED_API = {
            "search": search,
            "search_bills": search_bills,
            "search_bill_metas": search_bill_metas,
            "search_speeches": search_speeches,
            "search_documents": search_documents,
            "search_members": search_members,
            "lookup_member_by_name": lookup_member_by_name,
            "lookup_bill_by_id": lookup_bill_by_id,
            "stats": stats,
        }
        # BM25 sub-index 로드는 여기서 하지 않는다 (아래 _start_bm25_loader 참조).
        # 동기 로드는 인덱스 크기에 비례해 수십 초까지 늘어나고, 그동안
        # mcp.run()에 도달하지 못해 클라이언트의 MCP startup timeout(30s)을
        # 유발한다. handshake와 분리해 백그라운드 daemon thread로 옮겼다.
        _PREIMPORT_GET_ENGINE = _get_engine
        sys.stderr.write("[mcp] RAG preimport done (BM25 load deferred)\n")
    except Exception as _e:
        _PREIMPORTED_API = None
        sys.stderr.write(f"[mcp] RAG preimport failed: {_e}\n")
finally:
    # 원래 stdout 복구 — 이제부터 MCP protocol 출력은 정상 stdout으로
    os.dup2(_saved_stdout_fd, 1)
    os.close(_saved_stdout_fd)

import json

import duckdb
from mcp.server.fastmcp import FastMCP

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _env_path(var: str, default: str) -> str:
    """경로 환경변수 해석. 미설정·빈 값이면 default (= 기존 동작 그대로).

    설정 시 ~ 와 $VAR/%VAR% 를 확장한 뒤 os.path.abspath 로 정규화한다
    (Windows 드라이브 문자·역슬래시 안전). 조립은 전부 os.path.join 이므로
    구분자를 하드코딩하지 않는다.
    rag_assembly/config.py::_resolve_data_dir() 과 규칙이 같아야 한다.
    """
    raw = (os.environ.get(var) or "").strip()
    if not raw:
        return os.path.abspath(default)
    return os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))


# data/ 루트 — 개별 DB 변수가 이것보다 우선한다.
_DATA_DIR     = _env_path("ASSEMBLY_DATA_DIR", os.path.join(_REPO_ROOT, "data"))
_BILLS_KR_DIR = os.path.join(_DATA_DIR, "bills_kr")
_NEWS_DIR     = os.path.join(_DATA_DIR, "news")
RAW_DB_PATH           = _env_path(
    "ASSEMBLY_RAW_DB_PATH", os.path.join(_BILLS_KR_DIR, "assembly_raw.duckdb"))
ANALYSIS_DB_PATH      = _env_path(
    "ASSEMBLY_ANALYSIS_DB_PATH", os.path.join(_BILLS_KR_DIR, "assembly_analysis.duckdb"))
NEWS_RAW_DB_PATH      = _env_path(
    "ASSEMBLY_NEWS_RAW_DB_PATH", os.path.join(_NEWS_DIR, "news.duckdb"))
NEWS_ANALYSIS_DB_PATH = _env_path(
    "ASSEMBLY_NEWS_ANALYSIS_DB_PATH", os.path.join(_NEWS_DIR, "news_analysis.duckdb"))
# 호환용 alias
DB_PATH = ANALYSIS_DB_PATH

mcp = FastMCP("assembly-db")

# 카탈로그 화이트리스트 — list_tables / describe_table 검색 범위
_CATALOGS = ("assembly_analysis", "raw", "news_analysis", "news_raw")


_ATTACH_ENV = {
    "raw": "ASSEMBLY_RAW_DB_PATH",
    "news_analysis": "ASSEMBLY_NEWS_ANALYSIS_DB_PATH",
    "news_raw": "ASSEMBLY_NEWS_RAW_DB_PATH",
}
_ATTACH_WARNED: set[str] = set()   # 경고는 alias당 1회 (호출마다 도배 금지)


def _open() -> duckdb.DuckDBPyConnection:
    """analysis DB를 read-only로 열고 raw + 뉴스 두 DB를 ATTACH.

    같은 process에서 _open()이 여러 번 호출될 때 storage manager가
    "이미 등록됨" BinderException을 던지는 경우가 있으므로 attach는 idempotent.

    경로가 잘못 지정된 경우(다른 드라이브·부분 복사 등):
      - 메인(analysis)이 없으면 어느 도구도 못 쓰므로 고칠 환경변수를 알려주며 실패.
      - ATTACH 대상이 없으면 그 카탈로그만 빼고 연다 — 셋 중 하나가 빠졌다고
        나머지 DuckDB 도구까지 죽이지 않는다 (stderr에 1회 경고).
    파일이 모두 제자리인 기본 상태에서는 아래 경로가 전부 no-op이다.
    """
    if not os.path.isfile(ANALYSIS_DB_PATH):
        raise RuntimeError(
            f"analysis DB 파일이 없습니다: {ANALYSIS_DB_PATH}\n"
            "환경변수 ASSEMBLY_ANALYSIS_DB_PATH (또는 data/ 통째로 옮겼다면 "
            "ASSEMBLY_DATA_DIR) 로 실제 위치를 지정하세요.")
    con = duckdb.connect(ANALYSIS_DB_PATH, read_only=True)
    for path, alias in (
        (RAW_DB_PATH, "raw"),
        (NEWS_ANALYSIS_DB_PATH, "news_analysis"),
        (NEWS_RAW_DB_PATH, "news_raw"),
    ):
        if not os.path.isfile(path):
            if alias not in _ATTACH_WARNED:
                _ATTACH_WARNED.add(alias)
                sys.stderr.write(
                    f"[mcp] ATTACH 생략 — {alias} DB 파일 없음: {path} "
                    f"→ 환경변수 {_ATTACH_ENV[alias]} 로 지정\n")
                sys.stderr.flush()
            continue
        # 경로는 SQL 문자열 리터럴로 들어가므로 작은따옴표만 escape한다.
        # (역슬래시는 DuckDB 표준 문자열에서 escape 문자가 아니라 Windows 경로가
        #  그대로 안전하다 — 여기서 구분자를 바꾸지 말 것.)
        lit = path.replace("'", "''")
        try:
            con.execute(f"ATTACH '{lit}' AS {alias} (READ_ONLY)")
        except duckdb.BinderException:
            # 이미 attached (DuckDB의 같은-파일 storage manager 중복 등록 방지)
            pass
    return con


@mcp.tool()
def list_tables() -> str:
    """4개 DB의 모든 테이블·뷰 목록을 반환합니다.

    카탈로그:
      - assembly_analysis : 법안 분류·필터 (main connection)
      - raw               : Assembly 37 API + 추출 본문 (read-only ATTACH)
      - news_analysis     : Stage 1+2 적용 뉴스 + 분류 (read-only ATTACH)
      - news_raw          : raw 도메스틱 뉴스 157k (read-only ATTACH)
    """
    placeholders = ", ".join(["?"] * len(_CATALOGS))
    con = _open()
    try:
        rows = con.execute(
            f"""
            SELECT table_catalog, table_name, table_type
            FROM information_schema.tables
            WHERE table_catalog IN ({placeholders})
            ORDER BY table_catalog, table_type, table_name
            """,
            list(_CATALOGS),
        ).fetchall()
    finally:
        con.close()
    out = []
    cur_cat = None
    for cat, name, ttype in rows:
        if cat != cur_cat:
            out.append(f"\n=== {cat} ===")
            cur_cat = cat
        # main connection이 assembly_analysis라 그것만 무수식, 나머지는 prefix
        prefix = f"{cat}." if cat != "assembly_analysis" else ""
        out.append(f"{'[VIEW]' if ttype == 'VIEW' else '[TABLE]'} {prefix}{name}")
    return "\n".join(out).lstrip()


@mcp.tool()
def describe_table(table_name: str) -> str:
    """특정 테이블/뷰의 컬럼 정보와 샘플 데이터를 반환합니다.

    Args:
        table_name: 테이블 또는 뷰 이름. 다른 카탈로그는 '<catalog>.<name>' 형식
            (예: 'raw.v_bill', 'news_analysis.news_articles', 'news_raw.news_articles').
            무수식이면 4개 카탈로그를 순차 검색.
    """
    # '<catalog>.<name>' 같은 카탈로그 접두 처리
    if "." in table_name:
        catalog, _, base_name = table_name.partition(".")
    else:
        base_name = table_name
        catalog = None

    con = _open()
    try:
        # 카탈로그 미지정 시 4개 DB에서 검색 (assembly_analysis 우선)
        if catalog is None:
            for cat in _CATALOGS:
                cols = con.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_catalog = ? AND table_name = ? ORDER BY ordinal_position",
                    [cat, base_name],
                ).fetchall()
                if cols:
                    catalog = cat
                    break
        else:
            cols = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_catalog = ? AND table_name = ? ORDER BY ordinal_position",
                [catalog, base_name],
            ).fetchall()
        if not cols:
            return f"테이블 '{table_name}'을 찾을 수 없습니다."

        # main connection은 assembly_analysis라 그것만 무수식, 나머지는 catalog 접두
        qualified = (
            f'"{base_name}"'
            if catalog == "assembly_analysis"
            else f'{catalog}."{base_name}"'
        )
        count = con.execute(f'SELECT COUNT(*) FROM {qualified}').fetchone()[0]
        sample = con.execute(f'SELECT * FROM {qualified} LIMIT 3').fetchall()
        col_names = [c[0] for c in cols]

        prefix = f"{catalog}." if catalog != "assembly_analysis" else ""
        result = f"=== {prefix}{base_name} ({count:,}건) ===\n\n"
        result += "컬럼:\n"
        for cname, ctype in cols:
            result += f"  {cname} ({ctype})\n"
        result += f"\n샘플 ({min(3, count)}건):\n"
        for row in sample:
            result += "  " + json.dumps(
                dict(zip(col_names, [str(v)[:100] if v else None for v in row])),
                ensure_ascii=False,
            ) + "\n"
    finally:
        con.close()
    return result


@mcp.tool()
def query(sql: str) -> str:
    """DuckDB SQL을 실행하고 결과를 반환합니다. SELECT만 허용.

    분석 테이블은 무수식, raw 테이블은 `raw.<name>` 접두사로 접근.

    Args:
        sql: 실행할 SQL 쿼리 (SELECT만 가능)
    """
    # 선두 주석(-- 또는 /* */)과 공백을 건너뛰고 첫 키워드 추출
    import re
    stripped = re.sub(r"^\s*(?:--[^\n]*\n|/\*.*?\*/)\s*", "", sql, flags=re.DOTALL)
    while True:
        new = re.sub(r"^\s*(?:--[^\n]*\n|/\*.*?\*/)\s*", "", stripped, flags=re.DOTALL)
        if new == stripped:
            break
        stripped = new
    first = stripped.lstrip().upper()
    if not (first.startswith("SELECT") or first.startswith("WITH")
            or first.startswith("(SELECT") or first.startswith("(WITH")):
        return "오류: SELECT/WITH 문만 실행 가능합니다."

    con = _open()
    try:
        result = con.execute(sql).fetchall()
        columns = [desc[0] for desc in con.description]
        rows = [dict(zip(columns, row)) for row in result]

        if not rows:
            return "결과 없음 (0건)"

        output = f"결과: {len(rows)}건\n\n"
        output += json.dumps(rows[:50], ensure_ascii=False, default=str, indent=2)
        if len(rows) > 50:
            output += f"\n\n... 외 {len(rows) - 50}건 생략"
        return output
    except Exception as e:
        return f"SQL 오류: {e}"
    finally:
        con.close()


@mcp.tool()
def get_overview() -> str:
    """국회 데이터베이스 개요를 반환합니다. 어떤 데이터가 있는지 파악할 때 사용하세요."""
    con = _open()
    try:
        def n(qualified: str):
            try:
                return con.execute(f"SELECT COUNT(*) FROM {qualified}").fetchone()[0]
            except Exception:
                return None

        def fmt(x):
            return f"{x:,}" if x is not None else "?"

        # raw 측 (raw.* 접두사)
        v_member = n("raw.v_member")
        v_bill = n("raw.v_bill")
        v_vote = n("raw.v_vote")
        v_vote_summary = n("raw.v_vote_summary")
        v_bill_detail = n("raw.v_bill_detail")
        v_plenary_conf = n("raw.v_plenary_conf")
        v_committee_conf = n("raw.v_committee_conf")
        v_plenary_bill = n("raw.v_plenary_bill")
        speeches = n("raw.speeches")
        billinfodetail = n("raw.billinfodetail")
        bill_text = n("raw.bill_text")
        document_text = n("raw.document_text")

        # analysis 측 (무수식)
        speech_issues = n("speech_issues")
        bill_classifications = n("bill_classifications")
        bill_ai_filter = n("bill_ai_filter")

        try:
            sp_min, sp_max = con.execute(
                "SELECT MIN(conf_date), MAX(conf_date) FROM raw.speeches"
            ).fetchone()
            speech_range = (
                f", {str(sp_min)[:4]}-{str(sp_max)[:4]}"
                if sp_min and sp_max
                else ""
            )
        except Exception:
            speech_range = ""

        try:
            bill_age_min, bill_age_max = con.execute(
                "SELECT MIN(age), MAX(age) FROM raw.bill_text"
            ).fetchone()
            bill_age_range = (
                f"{bill_age_min}-{bill_age_max}대"
                if bill_age_min is not None
                else "ages 19-22"
            )
        except Exception:
            bill_age_range = "ages 19-22"

        return f"""13~22대 국회 Open API 데이터 (DuckDB, 2026-05-09부터 raw·analysis 분리)

DB 구조:
  data/bills_kr/assembly_raw.duckdb       - 수집 + 추출 본문 (37 API + bill_text + document_text + speeches)
  data/bills_kr/assembly_analysis.duckdb  - 분류·태깅·집계 + analysis 통합 뷰

질의 시 raw 측은 `raw.<table>` 접두사, analysis 측은 무수식.

=== raw 측 분석 친화 뷰 ===
raw.v_member: 현재(22대) 국회의원 인적사항 ({fmt(v_member)}명). 조인키: mona_cd
raw.v_bill: 발의법률안 ({fmt(v_bill)}건, 13~22대). 조인키: lead_mona_cd → mona_cd
raw.v_vote: 의원별 본회의 표결 ({fmt(v_vote)}건). 찬성/반대/기권
raw.v_vote_summary: 의안별 표결 집계 ({fmt(v_vote_summary)}건)
raw.v_bill_detail: 의안 상세정보 ({fmt(v_bill_detail)}건)
raw.v_plenary_conf: 본회의 회의록 메타 ({fmt(v_plenary_conf)}건)
raw.v_committee_conf: 위원회 회의록 메타 ({fmt(v_committee_conf)}건)
raw.v_plenary_bill: 본회의 처리안건 ({fmt(v_plenary_bill)}건)

=== raw 측 추출 본문 ===
raw.bill_text: 법안 원문 ({fmt(bill_text)}건, {bill_age_range}). PDF→fitz 추출.
raw.document_text: 회의록·연구보고서 본문 ({fmt(document_text)}건). source 컬럼:
                   minutes_plenary / minutes_committee / minutes_subcommittee /
                   minutes_committee_of_whole / report / research
raw.speeches: 회의록 발언 전문 ({fmt(speeches)}건{speech_range})
raw.billinfodetail: 의안 상세정보 원본 ({fmt(billinfodetail)}건). 대수 구분 없이 통합
raw.nzmimeepazxkubdpn: 발의법률안 raw 테이블 (raw.v_bill의 소스)

=== analysis 측 분석 산출물 ===
v_kr_bills_analysis: KR 법안 분석 통합 뷰. raw.v_bill + raw.bill_text + 분류 + AI 필터 결합.
                     bill_loaders.load_kr_bills()가 이걸 사용.
v_bill_classifications_current: 최신 prompt_version의 분류만 노출.
bill_classifications: 10-속성 분류 ({fmt(bill_classifications)}건, KR/US/EU 통합, prompt_version 버전 관리).
bill_ai_filter: KR Stage-2 GPT 필터 ({fmt(bill_ai_filter)}건, core/adjacent/unrelated).
prompt_versions: 프롬프트 버전 레지스트리.
speech_issues: 27개 카테고리 키워드 태깅 ({fmt(speech_issues)}건). raw.speeches에서 파생.

=== 참고 ===
- 모든 카운트는 호출 시점에 동적으로 산출됨 (캐시 없음).
- 모든 per-age 테이블에 age INTEGER 컬럼 표준화. NULL 없음 (validate_collection이 보장).
- raw.billrcp.age 음수값(-1, -2, -3) = 국가보위입법회의/국가재건최고회의/비상국무회의.
- raw.v_member에는 age 컬럼 없음 (22대 의원만 있음)
- gender 값: '남', '여'
- reelect 값: '초선', '재선', '3선', '4선', '5선', '6선'
- speaker 값에 '의원', '위원' 접미사 포함될 수 있음 → LIKE '%이름%' 사용
"""
    finally:
        con.close()


# ────────────────────────────────────────────────────────
# RAG (rag_assembly) 통합 — 의미 기반 검색
# ────────────────────────────────────────────────────────

_rag_engine = None

# RAG 인덱스 실물 경로 (rag_assembly/config.py의 DATA_DIR·BM25_PKL·LANCE_DIR와 대응).
# BM25Index.__init__은 cfg.BM25_PKL의 '.pkl' 접미사를 떼고 디렉터리로 쓰므로
# 실제 root는 data/bm25 — 접미사 유무 양쪽 다 인정한다.
# RAG_DATA_DIR 은 rag_assembly/config.py 가 읽는 것과 **같은 변수·같은 규칙**이다
# (여기만 바뀌면 진단과 실제 검색 경로가 갈라진다).
_RAG_DATA_DIR = _env_path("RAG_DATA_DIR",
                          os.path.join(_REPO_ROOT, "rag_assembly", "data"))
_RAG_BM25_MANIFESTS = (
    os.path.join(_RAG_DATA_DIR, "bm25", "manifest.json"),
    os.path.join(_RAG_DATA_DIR, "bm25.pkl", "manifest.json"),
)
_RAG_LANCE_TABLE = os.path.join(_RAG_DATA_DIR, "lance_db", "chunks.lance")


# ── 경로 진단 (startup stderr 1줄/경로) ────────────────
# 데이터를 저장소 밖에 두고 서빙할 때 "왜 안 보이는지"를 즉시 알려준다.
# stdout에는 절대 쓰지 않는다 (JSON-RPC 채널).

def _path_diagnostics() -> list[str]:
    """해석된 경로 × 존재 여부 × 고칠 환경변수 이름 (한 경로당 한 줄)."""
    def _any_exists(_p: str) -> bool:
        return any(os.path.exists(q) for q in _RAG_BM25_MANIFESTS)

    rows = (
        ("analysis_db",         ANALYSIS_DB_PATH,       "ASSEMBLY_ANALYSIS_DB_PATH",      os.path.isfile),
        ("raw_db",              RAW_DB_PATH,            "ASSEMBLY_RAW_DB_PATH",           os.path.isfile),
        ("news_analysis_db",    NEWS_ANALYSIS_DB_PATH,  "ASSEMBLY_NEWS_ANALYSIS_DB_PATH", os.path.isfile),
        ("news_raw_db",         NEWS_RAW_DB_PATH,       "ASSEMBLY_NEWS_RAW_DB_PATH",      os.path.isfile),
        ("rag_data_dir",        _RAG_DATA_DIR,          "RAG_DATA_DIR",                   os.path.isdir),
        ("rag_bm25_manifest",   _RAG_BM25_MANIFESTS[0], "RAG_DATA_DIR",                   _any_exists),
        ("rag_lance_table",     _RAG_LANCE_TABLE,       "RAG_DATA_DIR",                   os.path.isdir),
    )
    out = []
    for label, path, var, check in rows:
        try:
            ok = bool(check(path))
        except OSError:
            ok = False
        if ok:
            out.append(f"[mcp] path {label}: OK   {path}")
        else:
            out.append(f"[mcp] path {label}: 없음 {path} "
                       f"→ 환경변수 {var} 로 지정 (ASSEMBLY_DATA_DIR 로 data/ 통째 이동도 가능)")
    return out


def _report_paths_to_stderr() -> None:
    for line in _path_diagnostics():
        sys.stderr.write(line + "\n")
    sys.stderr.flush()


_report_paths_to_stderr()


def _dir_has_entry(path: str) -> bool:
    """디렉터리가 존재하고 항목이 하나 이상 있으면 True (전체 나열 없이 조기 종료)."""
    try:
        with os.scandir(path) as it:
            for _ in it:
                return True
    except OSError:
        return False
    return False


def _rag_index_missing() -> list[str]:
    """RAG 인덱스 구성요소 중 없는 것 목록. 빈 리스트면 정상."""
    missing = []
    if not any(os.path.exists(p) for p in _RAG_BM25_MANIFESTS):
        missing.append(f"BM25 manifest ({_RAG_BM25_MANIFESTS[0]})")
    # LanceDB 테이블은 data/ 하위에 fragment 파일이 있어야 실제 데이터가 있는 것
    if not _dir_has_entry(os.path.join(_RAG_LANCE_TABLE, "data")):
        missing.append("LanceDB chunks 데이터 ("
                       + os.path.join(_RAG_LANCE_TABLE, "data") + ")")
    return missing


def _rag_unavailable_reason() -> str | None:
    """RAG 사용 불가면 사용자에게 보여줄 안내 메시지, 사용 가능하면 None.

    판정 기준은 인덱스 실물 파일 존재 여부이고, startup preimport 결과는 보조 정보.
    벡터 인덱스(LanceDB)가 없으면 검색 자체가 불가능하므로 즉시 안내를 반환한다.
    BM25만 없는 경우는 기존 코드가 vector-only로 degrade하므로 막지 않는다.
    인덱스가 정상적으로 존재하면 None → 기존 동작이 그대로 유지된다.
    """
    missing = _rag_index_missing()
    lance_ok = _dir_has_entry(os.path.join(_RAG_LANCE_TABLE, "data"))
    if not missing or lance_ok:
        return None
    pre = ("startup preimport도 실패한 상태"
           if _PREIMPORTED_API is None else "startup preimport 자체는 성공")
    return (
        f"RAG 인덱스 미구축 ({_RAG_DATA_DIR} 에 BM25·LanceDB 인덱스 없음).\n"
        "인덱스를 다른 위치에 뒀다면 환경변수 RAG_DATA_DIR 로 지정하세요.\n"
        f"없는 구성요소: {', '.join(missing)}\n"
        f"참고: {pre}.\n"
        "DuckDB 도구(list_tables / describe_table / query / get_overview)는 "
        "정상 사용 가능합니다.\n"
        "인덱스 재구축은 rag_assembly/indexer.py 참조."
    )


# ── BM25 백그라운드 로드 (handshake 분리) ─────────────
# BM25는 13개 sub-index를 pickle/bm25s로 읽어들이므로 인덱스가 커질수록
# 로드가 길어진다(현 설계 기준 10초+). 이를 startup에서 동기로 하면
# mcp.run() 진입이 그만큼 늦어져 클라이언트의 MCP startup timeout(30s)에
# 걸린다. 그래서 daemon thread로 분리하고, 로드 중 들어온 rag_* 호출은
# 블로킹 대기 대신 즉시 "로딩 중" 안내를 돌려준다.
#
# stdout 보호: 스레드 안에서 os.dup2 같은 **fd 레벨** redirect는 쓰지 않는다
# (프로세스 전역 fd 1을 바꾸면 FastMCP의 응답 쓰기와 경합 — 과거 실패 이력은
#  _safe_rag_call() 주석 참조). 대신 Python 레벨 sys.stdout 가드
# (_worker_stdout_guard)로 로드 구간의 print()류만 stderr로 돌린다.
# 이게 안전한 근거: mcp/server/stdio.py::stdio_server()가 startup에
# TextIOWrapper(sys.stdout.buffer)를 만들어 AsyncFile로 **객체 참조**를 잡아두고
# 그것으로 응답을 쓴다 — 이후 sys.stdout 이름을 다시 조회하지 않으므로
# sys.stdout 교체는 진행 중인 FastMCP 응답에 영향이 없다. 아직 캡처 전이라
# 창이 겹치는 경우까지는 _StdoutToStderr.buffer가 처리한다 (그 docstring 참조).
# 잔여 위험(막지 않음): os.write(1, ...) / sys.stdout.buffer.write(...) 처럼
# fd·binary buffer에 직접 쓰는 확장 라이브러리는 Python 레벨 가드로 차단 불가.
# 이를 막으려면 프로세스 전역 fd redirect가 필요한데 그건 FastMCP 응답과
# 경합하므로 의도적으로 시도하지 않는다.
import contextlib as _contextlib
import threading as _threading
import time as _time

_BM25_STATE = "absent"     # absent | deferred | loading | ready | failed
_BM25_STARTED_AT: float | None = None
_BM25_ERROR: str | None = None
_BM25_LOCK = _threading.Lock()   # 로드 이중 실행 방지 (백그라운드 ↔ lazy 경로)

# 로더 시작 전 유예 시간(초). BM25 로드는 pickle 등 GIL을 오래 쥐는 구간이
# 있어서, initialize/tools/list handshake가 끝나기 전에 시작하면 응답이
# 밀릴 수 있다. 아주 짧게 미뤄 handshake에 우선권을 준다.
_BM25_LOAD_DELAY_DEFAULT_S = 2.0
# 상한. handshake 양보가 목적이라 수 초면 충분한데, 오타(예: 86400)를 그대로
# 받으면 로더가 하루를 자고 rag_* 가 영영 "로딩 중"에 머문다. 초과 시 clamp.
_BM25_LOAD_DELAY_MAX_S = 60.0


def _read_bm25_load_delay() -> float:
    """MCP_BM25_LOAD_DELAY 파싱. 비수치·음수면 경고 후 기본값, 과대값은 상한 clamp.

    import 시점에 float()이 ValueError를 던지면 서버 프로세스 자체가 뜨지
    못해 DuckDB 도구까지 못 쓰게 되므로, 잘못된 값은 무시하고 살아남는다.
    """
    raw = os.environ.get("MCP_BM25_LOAD_DELAY")
    if raw is None or raw.strip() == "":
        return _BM25_LOAD_DELAY_DEFAULT_S
    try:
        v = float(raw)
    except (TypeError, ValueError):
        sys.stderr.write(
            f"[mcp] MCP_BM25_LOAD_DELAY={raw!r} 는 숫자가 아님 — "
            f"기본값 {_BM25_LOAD_DELAY_DEFAULT_S}초 사용\n")
        sys.stderr.flush()
        return _BM25_LOAD_DELAY_DEFAULT_S
    if v != v or v in (float("inf"), float("-inf")) or v < 0:  # NaN/inf/음수
        sys.stderr.write(
            f"[mcp] MCP_BM25_LOAD_DELAY={raw!r} 는 유효 범위 밖 — "
            f"기본값 {_BM25_LOAD_DELAY_DEFAULT_S}초 사용\n")
        sys.stderr.flush()
        return _BM25_LOAD_DELAY_DEFAULT_S
    if v > _BM25_LOAD_DELAY_MAX_S:
        sys.stderr.write(
            f"[mcp] MCP_BM25_LOAD_DELAY={raw!r} 는 상한 초과 — "
            f"{_BM25_LOAD_DELAY_MAX_S:.0f}초로 제한\n")
        sys.stderr.flush()
        return _BM25_LOAD_DELAY_MAX_S
    return v


_BM25_LOAD_DELAY_S = _read_bm25_load_delay()


def _bm25_index_present() -> bool:
    return any(os.path.exists(p) for p in _RAG_BM25_MANIFESTS)


class _StdoutToStderr:
    """가드 창 동안 sys.stdout 자리를 대신하는 프록시.

    - write()/flush() 등 텍스트 출력 → stderr (print() protocol 오염 차단)
    - .buffer → **진짜 stdout의 binary buffer**를 그대로 노출.

    .buffer를 stderr로 돌리면 안 되는 이유(실측): mcp/server/stdio.py의
    stdio_server()는 mcp.run() 진입 시점에 TextIOWrapper(sys.stdout.buffer)를
    만들어 그 객체로 응답을 쓴다. 로더가 delay=0 등으로 그 캡처보다 먼저
    가드를 잡으면 FastMCP가 stderr의 buffer를 잡아가 **응답 전체가 stderr로**
    새어나간다 (initialize 무응답). 진짜 buffer를 노출해 이 경합을 원천 차단.
    """
    def __init__(self, real_stdout):
        self._real_stdout = real_stdout

    @property
    def buffer(self):
        return self._real_stdout.buffer

    def write(self, s):
        return sys.stderr.write(s)

    def flush(self):
        return sys.stderr.flush()

    def __getattr__(self, name):   # 나머지 파일 API는 stderr에 위임
        return getattr(sys.stderr, name)


@_contextlib.contextmanager
def _worker_stdout_guard():
    """로드 구간 동안 sys.stdout → stderr 프록시. print()류의 protocol 오염 방지.

    프로세스 전역이지만 (a) FastMCP는 startup에 잡아둔 stream 객체로 응답을
    쓰므로 영향이 없고, (b) 구간 전체가 _BM25_LOCK 안이라 중첩·경합이 없으며,
    (c) 캡처 시점이 가드 창과 겹쳐도 .buffer가 진짜 stdout이라 안전하다.
    막지 못하는 것(잔여 위험): os.write(1, ...) / sys.stdout.buffer.write(...)
    처럼 fd·binary buffer에 직접 쓰는 확장 라이브러리 — 상단 주석 참조.
    """
    saved = sys.stdout
    sys.stdout = _StdoutToStderr(saved)
    try:
        yield
    finally:
        sys.stdout = saved


def _register_embed_load_guard() -> None:
    """rag_assembly.embedder 의 '모델 로드' 구간에 위 stdout 가드를 물린다.

    로컬 임베딩 모델(sentence-transformers) 로드는 huggingface_hub·transformers가
    stdout으로 뭔가 뱉을 수 있는 유일한 구간이다. 인코딩 자체는 조용하다.
    가드 창이 겹치지 않도록, 로드를 유발하는 MCP 경로는 모두 _BM25_LOCK 안에서
    부른다 (_embed_query_inproc / _embed_warmup_now).
    """
    try:
        with _worker_stdout_guard():
            import embedder as _rag_embedder   # numpy+config만 — 모델은 lazy
        _rag_embedder.set_load_guard(_worker_stdout_guard)
        sys.stderr.write("[mcp] embed load guard registered\n")
    except Exception as e:  # noqa: BLE001 — 가드 미등록이 서버를 죽이면 안 된다
        sys.stderr.write(f"[mcp] embed load guard 등록 실패(무시): "
                         f"{type(e).__name__}: {e}\n")
    sys.stderr.flush()


_register_embed_load_guard()


# ── 질의 임베딩 모델 warm-up ──────────────────────────
# 첫 rag_search에서 568M 모델을 로드하면 그 호출만 수 초 느려진다(실측 GPU 2s,
# 콜드 캐시·CPU면 수십 초). BM25와 **독립된** 데몬 스레드로 미리 올려 둔다.
# BM25 로더와 별개인 이유: BM25 인덱스가 없거나 preimport가 실패하면 그 스레드는
# 아예 뜨지 않는데, 질의 임베딩은 그 경우에도 필요하다(벡터 단독 검색).
# 실패해도 무해 — 검색 시점에 lazy 로드가 다시 시도된다. BM25 상태와 섞지 않는다.
_EMBED_WARMUP_STATE = "idle"     # idle | loading | ready | failed | disabled
_EMBED_WARMUP_ERROR: str | None = None
# BM25보다 조금 늦게 시작해 handshake·BM25에 우선권을 준다 (어차피 _BM25_LOCK으로
# 직렬화되므로 순서가 뒤집혀도 정합성 문제는 없다).
_EMBED_WARMUP_DELAY_S = 3.0


def _embed_warmup_enabled() -> bool:
    return os.environ.get("MCP_EMBED_WARMUP", "1").strip() not in ("0", "false", "False")


def _embed_warmup_now() -> None:
    """질의 임베딩 모델 선로드. _BM25_LOCK으로 stdout 가드 창을 직렬화한다.

    lock을 공유하는 이유: _worker_stdout_guard()의 안전 전제가 '가드 창이
    겹치지 않는다'이고, 모델 로드도 그 가드를 쓰기 때문이다(_register_embed_load_guard).
    BM25 로드가 lock을 쥐고 있으면 여기서 기다린다 — 데몬 스레드라 무해하다.
    """
    global _EMBED_WARMUP_STATE, _EMBED_WARMUP_ERROR
    if not _embed_warmup_enabled():
        _EMBED_WARMUP_STATE = "disabled"
        return
    t0 = _time.time()
    try:
        import embedder as _rag_embedder
        _EMBED_WARMUP_STATE = "loading"
        with _BM25_LOCK:
            _rag_embedder.load_model()
        _EMBED_WARMUP_STATE = "ready"
        _EMBED_WARMUP_ERROR = None
        sys.stderr.write(
            f"[mcp] embed model warm in {_time.time()-t0:.1f}s "
            f"(device={_rag_embedder.loaded_device()})\n")
    except Exception as e:  # noqa: BLE001
        _EMBED_WARMUP_STATE = "failed"
        _EMBED_WARMUP_ERROR = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[mcp] embed warm-up failed (검색 시 재시도): "
                         f"{_EMBED_WARMUP_ERROR}\n")
    sys.stderr.flush()


def _start_embed_warmup(delay: float | None = None) -> str:
    """질의 임베딩 모델 warm-up을 데몬 스레드로 시작. mcp.run() 진입 직전 호출."""
    global _EMBED_WARMUP_STATE
    if not _embed_warmup_enabled():
        _EMBED_WARMUP_STATE = "disabled"
        sys.stderr.write("[mcp] embed warm-up disabled (MCP_EMBED_WARMUP=0)\n")
        sys.stderr.flush()
        return _EMBED_WARMUP_STATE
    d = _EMBED_WARMUP_DELAY_S if delay is None else delay

    def _worker():
        if d > 0:
            _time.sleep(d)
        _embed_warmup_now()

    _threading.Thread(target=_worker, name="embed-warmup", daemon=True).start()
    sys.stderr.write(f"[mcp] embed warm-up scheduled (delay={d:.1f}s)\n")
    sys.stderr.flush()
    return "scheduled"


def _bm25_load_now(engine=None) -> None:
    """실제 로드 (lock으로 보호). 실패 시 예외를 그대로 올린다.

    engine: AssemblySearch 인스턴스. 생략하면 preimport한 _get_engine()을 쓴다.
    """
    global _BM25_STATE, _BM25_ERROR
    with _BM25_LOCK:
        # 'failed'도 조기 반환 대상이다 (R5). deferred + 느린 실패 로드에서
        # lock을 기다리던 동시 요청들이 각자 전체 로드를 순차 재실행하던 문제
        # (실측 3.00/6.00/9.00s) — 앞선 시도가 남긴 latch를 확인하고 즉시 돌아간다.
        if _BM25_STATE in ("ready", "failed"):
            return
        # 가드는 lock 안에서만 살아 있다 — 백그라운드 로더 경로와
        # deferred lazy 경로 둘 다 여기를 지나므로 한 곳으로 충분하다.
        try:
            with _worker_stdout_guard():
                eng = engine
                if eng is None:
                    if _PREIMPORT_GET_ENGINE is None:
                        raise RuntimeError("rag_assembly preimport 실패 — 엔진 없음")
                    eng = _PREIMPORT_GET_ENGINE()
                eng._ensure_bm25()
        except FileNotFoundError:
            # 인덱스 '부재'는 실패로 latch하지 않는다 (호출자 정책: 조용히
            # vector-only). 재시도 비용도 stat 한 번 수준이라 무해.
            raise
        except Exception as e:  # noqa: BLE001 — latch는 lock을 놓기 전에
            # 실패 확정을 lock 안에서 해 둬야, 대기 중이던 동시 요청이 위의
            # 조기 반환에 걸린다. 예외 자체는 호출자에게 그대로 올린다.
            _BM25_STATE = "failed"
            _BM25_ERROR = f"{type(e).__name__}: {e}"
            raise
        _BM25_STATE = "ready"
        _BM25_ERROR = None


def _bm25_loader_worker(delay: float) -> None:
    global _BM25_STATE, _BM25_ERROR, _BM25_STARTED_AT
    if delay > 0:
        _time.sleep(delay)
    t0 = _time.time()
    # 경과 시간 기준점은 '실제 로드 시작' 시점 (delay 구간은 제외).
    _BM25_STARTED_AT = t0
    try:
        _bm25_load_now()
        sys.stderr.write(f"[mcp] BM25 background load done in {_time.time()-t0:.1f}s\n")
    except Exception as e:  # noqa: BLE001 — 서버는 계속 살아 있어야 한다
        _BM25_STATE = "failed"
        _BM25_ERROR = f"{type(e).__name__}: {e}"
        sys.stderr.write(f"[mcp] BM25 background load failed: {_BM25_ERROR}\n")
    finally:
        sys.stderr.flush()


def _start_bm25_loader(delay: float | None = None) -> str:
    """BM25 백그라운드 로드 시작. mcp.run() 진입 직전에 호출.

    반환값은 시작 직후의 상태 문자열 (테스트·진단용).
    """
    global _BM25_STATE, _BM25_STARTED_AT
    d = _BM25_LOAD_DELAY_S if delay is None else delay
    # 상태 확인 → 전이 → 스레드 기동을 한 lock 안에서 처리 (check-then-set 원자화).
    # _bm25_load_now()도 같은 lock을 쓰지만 워커는 delay 후에야 잡으러 오고,
    # 이 블록은 파일 stat + Thread.start()만 하므로 곧바로 풀린다.
    with _BM25_LOCK:
        if _BM25_STATE in ("loading", "ready"):
            return _BM25_STATE
        if not _bm25_index_present() or _rag_unavailable_reason() is not None:
            # BM25 인덱스가 없거나, 벡터(LanceDB) 인덱스가 없어 RAG 자체가 불가.
            # 기존 _rag_unavailable_reason() 게이트가 담당하므로 로드하지 않는다.
            _BM25_STATE = "absent"
            msg = "[mcp] BM25 index absent/unusable — loader not started\n"
        elif _PREIMPORT_GET_ENGINE is None:
            # preimport 실패 → 엔진 핸들이 없다. 검색 경로의 lazy import에 맡긴다.
            _BM25_STATE = "deferred"
            msg = "[mcp] BM25 loader deferred (RAG preimport failed)\n"
        else:
            _BM25_STATE = "loading"
            # 실제 로드 시작 시점은 워커가 delay를 소진한 뒤 기록한다 (F5).
            _BM25_STARTED_AT = None
            _threading.Thread(target=_bm25_loader_worker, args=(d,),
                              name="bm25-loader", daemon=True).start()
            msg = f"[mcp] BM25 background load started (delay={d:.1f}s)\n"
        state = _BM25_STATE
    sys.stderr.write(msg)
    sys.stderr.flush()
    return state


def _bm25_gate_message() -> str | None:
    """rag_* '검색' 도구 공통 진입 게이트. 즉답할 안내가 있으면 문자열, 없으면 None.

    게이트 대상은 'loading' 하나뿐이다 (BM25 로드가 GIL을 오래 쥐는 동안
    블로킹 대기 대신 즉답). 'failed'는 게이트하지 않는다 — BM25가 없으면
    vector-only로 degrade한다는 기존 정책(_rag_unavailable_reason 참조)에
    맞춰 검색은 통과시키고 _bm25_degraded_notice()로 안내만 덧붙인다.
    """
    if _BM25_STATE == "loading":
        started = _BM25_STARTED_AT
        # max(0, …) — 시스템 시계가 뒤로 조정되면 음수 경과가 찍힌다.
        elapsed = ("곧 시작" if started is None
                   else f"약 {max(0.0, _time.time()-started):.0f}초 경과")
        return (
            f"RAG 인덱스 로딩 중 ({elapsed}) — 잠시 후 재시도해 주세요.\n"
            "BM25 sub-index를 백그라운드로 읽는 중이며, 완료되면 곧바로 검색이 됩니다.\n"
            "DuckDB 도구(list_tables / describe_table / query / get_overview)는 "
            "지금도 정상 사용 가능합니다."
        )
    return None


def _bm25_degraded_notice() -> str:
    """BM25 로드 실패 시 검색 결과 앞에 붙일 안내. 실패가 아니면 빈 문자열."""
    if _BM25_STATE == "failed":
        return (
            f"(BM25 로드 실패 — 벡터 단독 검색: {_BM25_ERROR})\n"
            "키워드 매칭이 빠진 결과이며, 인덱스가 손상된 경우 "
            "rag_assembly/bm25.py 로 재빌드가 필요합니다.\n\n"
        )
    return ""


def _get_rag_engine():
    """rag_assembly.api 싱글턴 반환. startup에서 미리 import한 것을 사용."""
    global _rag_engine
    if _rag_engine is None:
        if _PREIMPORTED_API is not None:
            _rag_engine = _PREIMPORTED_API
        else:
            # fallback (preimport 실패한 경우만): import 노이즈 차단.
            #
            # 여기서 fd 레벨 redirect(os.dup(1) → os.dup2(2,1) → 복원)를 쓰면
            # 안 된다. rag_* 도구는 asyncio.to_thread 워커에서 동시에 실행될 수
            # 있고, 두 번째 워커의 os.dup(1)은 '이미 stderr가 된 fd 1'을 저장해
            # 복원 자체가 오염을 확정시킨다 → fd 1이 영구히 stderr로 고정되고
            # 이후 모든 MCP 응답이 stderr로 새어 서버가 조용히 죽는다
            # (동시 3건 중 2/2회 재현).
            # 대신 같은 파일에서 이미 검증된 Python 레벨 가드를 재사용한다.
            # 가드의 안전 전제("구간 전체가 _BM25_LOCK 안이라 중첩·경합 없음")를
            # 그대로 유지하려고 _BM25_LOCK으로 감싼다 — _bm25_load_now()의 가드
            # 구간과도 상호 배제된다. 호출 순서상(_rag_search_sync가 이 함수를
            # 끝낸 뒤 _bm25_load_now()를 호출) 재진입·deadlock은 없다.
            with _BM25_LOCK:
                if _rag_engine is None:      # lock 대기 중 다른 워커가 채웠으면 skip
                    with _worker_stdout_guard():
                        rag_path = os.path.join(
                            os.path.dirname(os.path.abspath(__file__)),
                            "rag_assembly")
                        if rag_path not in sys.path:
                            sys.path.insert(0, rag_path)
                        import _bootstrap  # noqa: F401
                        from api import (search, search_bills,
                                          search_bill_metas,
                                          search_speeches, search_documents,
                                          search_members,
                                          lookup_member_by_name,
                                          lookup_bill_by_id, stats)
                        _rag_engine = {
                            "search": search, "search_bills": search_bills,
                            "search_bill_metas": search_bill_metas,
                            "search_speeches": search_speeches,
                            "search_documents": search_documents,
                            "search_members": search_members,
                            "lookup_member_by_name": lookup_member_by_name,
                            "lookup_bill_by_id": lookup_bill_by_id,
                            "stats": stats,
                        }
    return _rag_engine


def _safe_rag_call(fn, *args, **kwargs):
    """RAG 호출 wrapper. 노이즈 차단은 env var + logging으로 처리.

    이전 시도(os.dup2 fd redirect)는 프로세스 전역 fd를 바꿔서 FastMCP의
    응답 stdout 쓰기와 충돌. thread-safe 하지 않아 protocol 깨뜨림.
    지금은 startup 시 한 번만 fd redirect로 노이즈 흘려보내고, 호출 시점엔
    env var(HF_HUB_DISABLE_PROGRESS_BARS, TQDM_DISABLE, TRANSFORMERS_VERBOSITY)와
    logging 레벨 설정에 의존.
    """
    return fn(*args, **kwargs)


def _format_rag_results(results: list[dict], max_text: int = 400) -> str:
    if not results:
        return "결과 없음 (0건)"
    out = [f"결과: {len(results)}건"]
    for i, r in enumerate(results, 1):
        meta = r.get("metadata") or {}
        src = meta.get("source", "?")
        ref = (meta.get("bill_id") or meta.get("doc_id")
               or meta.get("conf_id") or meta.get("mona_cd") or "")
        rerank = r.get("rerank_score")
        rerank_s = f" rerank={rerank:.3f}" if rerank is not None else ""
        text = (r.get("text") or "")[:max_text]
        out.append(f"\n[{i}] source={src} ref={ref}{rerank_s}")
        # source-specific metadata 요약
        bits = []
        for k in ("bill_name", "speaker", "doc_source", "name", "party",
                   "age", "dae_num", "committee", "propose_date",
                   "conf_date"):
            v = meta.get(k)
            if v:
                bits.append(f"{k}={v}")
        if bits:
            out.append("    " + " | ".join(bits))
        out.append(f"    {text}")
    return "\n".join(out)


def _embed_query_inproc(query: str) -> list:
    """in-process 질의 임베딩 (기본 경로).

    "query: " prefix는 embedder.embed_query() 안에서만 붙는다.
    모델 로드는 프로세스당 1회 — 아직 안 올라왔을 때만 _BM25_LOCK을 잡아
    stdout 가드 창이 BM25 로드 가드와 겹치지 않게 한다. 이미 로드된 뒤에는
    lock 없이 곧장 인코딩한다(인코딩은 stdout에 쓰지 않는다).
    """
    import embedder as _rag_embedder
    if not _rag_embedder.is_loaded():
        with _BM25_LOCK:
            _rag_embedder.load_model()
    return _rag_embedder.embed_query(query)


def _embed_query_subproc(query: str) -> list:
    """격리 subprocess 질의 임베딩 (fallback).

    질의마다 568M 모델을 새로 로드하므로 느리다. in-process 경로가 실패하거나
    MCP_EMBED_SUBPROC=1 일 때만 쓴다. (옛 Vertex 시절엔 이게 기본 경로였다 —
    google-genai 네트워크 호출이 FastMCP context에서 hang했기 때문. 로컬
    모델에는 그 원인이 없다.)
    """
    import json
    import subprocess
    # 서버 자신을 띄운 인터프리터(.venv/bin/python)를 그대로 사용.
    py = sys.executable
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "rag_assembly", "_subproc_embed.py")
    r = subprocess.run(
        [py, script],
        input=query,
        capture_output=True,
        text=True,
        encoding="utf-8",
        # 옛 60초는 HTTP 1회 기준. 지금은 콜드 캐시에서 모델 로드(CPU 폴백
        # 포함)까지 포함하므로 넉넉히 잡는다.
        timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f"embed subprocess failed: {r.stderr}")
    return json.loads(r.stdout)["vector"]


def _simple_embed_query(query: str) -> list:
    """질의 → 1024-dim 단위벡터 (로컬 arctic-ko).

    기본 in-process, MCP_EMBED_SUBPROC=1이면 격리 subprocess.
    in-process가 터지면 subprocess로 1회 폴백한다.
    """
    if os.environ.get("MCP_EMBED_SUBPROC", "").strip() in ("1", "true", "True"):
        return _embed_query_subproc(query)
    try:
        return _embed_query_inproc(query)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[rag] in-proc embed 실패 → subprocess 폴백: "
                         f"{type(e).__name__}: {e}\n")
        sys.stderr.flush()
        return _embed_query_subproc(query)


def _hybrid_no_rerank(query: str, top_k: int, where: dict | None) -> list:
    """MCP 환경: subprocess로 embed 한 뒤 hybrid 검색 (rerank 없음).
    reranker 로드가 FastMCP context에서 hang하므로 dense+BM25+RRF만 사용."""
    global _BM25_STATE, _BM25_ERROR   # Step 2에서 실패를 latch
    import time
    from api import _get_engine
    # 메타필터를 BM25 결과에 적용하는 판정자는 정본(search.py)의 것을 그대로 쓴다.
    # 여기에 복제본을 두면 $eq/$in 지원 범위가 정본과 갈라진다 (D1 재발 경로).
    from search import _matches_where
    import config as _cfg

    t0 = time.time()
    eng_obj = _get_engine()
    sys.stderr.write(f"[rag] eng_obj acquired {time.time()-t0:.2f}s\n")
    sys.stderr.flush()

    # Step 1a: embed query — 로컬 arctic-ko, "query: " prefix는 embedder 내부에서
    t = time.time()
    sys.stderr.write("[rag] >>> embed_query (local arctic-ko) starting\n")
    sys.stderr.flush()
    qv = _simple_embed_query(query)
    sys.stderr.write(f"[rag] embed_query {time.time()-t:.2f}s (dim={len(qv)})\n")
    sys.stderr.flush()

    # Step 1b: LanceDB vector query
    t = time.time()
    sys.stderr.write(f"[rag] >>> vdb.query starting\n"); sys.stderr.flush()
    raw = eng_obj.vdb.query(qv, top_k=_cfg.VECTOR_TOP_K, where=where)
    sys.stderr.write(f"[rag] vdb.query {time.time()-t:.2f}s\n"); sys.stderr.flush()

    # Step 1c: parse vector results (mimicking AssemblySearch.vector_search)
    v_results = []
    ids = raw["ids"][0] if raw["ids"] else []
    docs = raw["documents"][0] if raw["documents"] else []
    metas = raw["metadatas"][0] if raw["metadatas"] else []
    dists = raw["distances"][0] if raw["distances"] else []
    for i, cid in enumerate(ids):
        v_results.append({
            "chunk_id": cid,
            "text": docs[i] if i < len(docs) else "",
            "metadata": metas[i] if i < len(metas) else {},
            "vector_score": 1.0 - dists[i] if i < len(dists) else 0.0,
            "vector_rank": i,
        })

    # Step 2: BM25 search
    # 게이트를 통과했다면 상태는 ready(로드 완료) / absent / deferred / failed다.
    # deferred(=preimport 실패)일 때만 여기서 lazy 로드가 일어나는데,
    # 백그라운드 로더와의 이중 로드를 막기 위해 같은 lock을 거쳐 간다.
    # failed면 재시도하지 않고 건너뛴다 → dense 결과만으로 degrade
    # (호출자가 _bm25_degraded_notice()로 안내를 덧붙인다).
    t = time.time()
    if _BM25_STATE == "failed":
        b_results = []
        sys.stderr.write("[rag] bm25 skipped (state=failed) — vector-only\n")
        sys.stderr.flush()
    else:
        # R3 note: 상태가 'loading'이면 _bm25_load_now()가 lock에서 블로킹될 수
        # 있으나, 현 호출 그래프에서는 _bm25_gate_message()가 'loading'을 이미
        # 즉답 처리해 여기 도달하지 못한다. 로더 재시작 기능을 추가하면 재검토.
        # '로드'와 '검색'의 예외는 분리해서 다룬다 (R5). 하나로 묶으면 로드가
        # 정상 완료(ready)된 뒤 검색 시점에만 터지는 예외까지 state=failed로
        # latch되어, 이후 영구 벡터 단독 + "BM25 로드 실패" 오귀인이 된다.
        loaded = False
        try:
            _bm25_load_now(eng_obj)
            # lock을 기다리는 동안 다른 스레드가 failed로 latch했으면
            # _bm25_load_now()는 예외 없이 조기 반환한다. 이때 검색으로 넘어가면
            # engine.bm25_search()가 내부 _ensure_bm25()로 실패한 로드를 다시
            # 통째로 재시도한다(실측: 대기 3s + 재로드 3s). 상태를 다시 확인해
            # 그대로 vector-only로 빠진다.
            loaded = _BM25_STATE != "failed"
            if not loaded:
                b_results = []
                sys.stderr.write(
                    "[rag] bm25 skipped (state=failed, 대기 중 latch됨) — vector-only\n")
                sys.stderr.flush()
        except FileNotFoundError:
            # 인덱스 '부재' — 기존 정책 유지: latch 없이 조용히 vector-only.
            b_results = []
        except Exception as e:  # noqa: BLE001
            # 손상 manifest(json.JSONDecodeError) 등 '부재가 아닌' 로드 실패.
            # 하드 실패로 벡터 결과까지 버리지 말고 F2의 vector-only degrade로
            # 흡수한다. state를 failed로 latch해 매 검색마다 전체 로드를
            # 재시도하지 않게 하고, _bm25_degraded_notice()가 사유를 안내한다.
            # (latch는 _bm25_load_now()가 lock 안에서 이미 걸었고, 여기서는
            #  같은 값으로 확정 + 로그만 남긴다.)
            b_results = []
            _BM25_STATE = "failed"
            _BM25_ERROR = f"{type(e).__name__}: {e}"
            sys.stderr.write(f"[rag] bm25 load failed → vector-only: {_BM25_ERROR}\n")
            sys.stderr.flush()
        if loaded:
            try:
                b_results = eng_obj.bm25_search(query, top_k=_cfg.BM25_TOP_K)
            except Exception as e:  # noqa: BLE001
                # 로드는 멀쩡한데 '이 쿼리'만 실패하는 경우(반쯤 재빌드된
                # sub-index의 IndexError, 특정 쿼리 토크나이저 오류 등).
                # 인덱스 전체가 죽은 게 아니므로 latch하지 않는다 — 해당
                # 쿼리만 vector-only로 넘기고 사유는 stderr에만 기록.
                b_results = []
                sys.stderr.write(
                    f"[rag] bm25_search failed for this query → vector-only "
                    f"(state 유지={_BM25_STATE}): {type(e).__name__}: {e}\n")
                sys.stderr.flush()
        sys.stderr.write(f"[rag] bm25_search {time.time()-t:.2f}s ({len(b_results)} hits)\n")
        sys.stderr.flush()

    # Step 2b: 메타필터를 BM25 결과에도 적용 (BM25는 native filter가 없다).
    # 벡터 팔은 vdb.query(where=...)로 이미 걸러져 있는데 여기서 거르지 않으면
    # 필터 밖 소스가 RRF를 타고 결과에 섞인다 — 반드시 RRF **이전**에 건다.
    # 정본 search.py::AssemblySearch.hybrid()와 같은 판정자·같은 위치.
    # metadata가 없는 BM25-only 후보({} 취급)는 필터가 걸린 순간 탈락한다 —
    # 이것도 정본과 동일하다 (소속을 증명할 수 없는 후보를 통과시키지 않는다).
    if where:
        _n_before = len(b_results)
        b_results = [r for r in b_results
                     if _matches_where(r.get("metadata") or {}, where)]
        sys.stderr.write(f"[rag] bm25 where-filter {_n_before} → "
                         f"{len(b_results)} hits (where={where})\n")
        sys.stderr.flush()

    # Step 3: RRF + dedup + fetch text for BM25-only candidates
    t = time.time()
    rrf_k = _cfg.RRF_K
    rrf_scores = {}
    details = {}
    for rank, r in enumerate(v_results):
        cid = r["chunk_id"]
        rrf_scores[cid] = rrf_scores.get(cid, 0) + 1 / (rrf_k + rank)
        details.setdefault(cid, {}).update(r)
        details[cid]["vector_rank"] = rank
    for rank, r in enumerate(b_results):
        cid = r["chunk_id"]
        rrf_scores[cid] = rrf_scores.get(cid, 0) + 1 / (rrf_k + rank)
        details.setdefault(cid, {})
        if "text" not in details[cid] and r.get("metadata"):
            details[cid]["metadata"] = r["metadata"]
        details[cid]["bm25_rank"] = rank
        details[cid]["bm25_score"] = r.get("score", 0)
    candidates = []
    for cid, rrf in sorted(rrf_scores.items(), key=lambda x: -x[1]):
        d = details[cid]
        d["chunk_id"] = cid
        d["rrf_score"] = rrf
        if "text" not in d:
            try:
                fetched = eng_obj.vdb.collection.get(
                    ids=[cid], include=["documents", "metadatas"])
                if fetched["documents"]:
                    d["text"] = fetched["documents"][0]
                    d["metadata"] = (fetched["metadatas"][0]
                                       if fetched["metadatas"] else {})
                else:
                    d["text"] = ""
            except Exception as e:
                sys.stderr.write(f"[rag] fetch failed for {cid}: {e}\n")
                d["text"] = ""
        candidates.append(d)
    sys.stderr.write(f"[rag] RRF+fetch {time.time()-t:.2f}s ({len(candidates)} candidates)\n")
    sys.stderr.flush()
    # reranker는 FastMCP context에서 로드 자체가 hang (transformers/CUDA init 어디선가).
    # 시도해봤지만 HF_HUB_OFFLINE=1도 효과 없음. 영구히 dense+BM25+RRF만 사용.
    return candidates[:top_k]


def _rag_search_sync(query, top_k, where):
    """블로킹 RAG 검색을 sync 함수로 분리. async tool이 asyncio.to_thread로 호출."""
    unavailable = _rag_unavailable_reason()
    if unavailable:
        return unavailable
    gated = _bm25_gate_message()
    if gated:
        return gated
    try:
        _get_rag_engine()
        results = _hybrid_no_rerank(query, top_k, where)
    except FileNotFoundError as e:
        # 인덱스 일부가 실행 도중 사라진 경우 등 — cryptic traceback 대신 안내
        return (f"RAG 인덱스 파일 없음: {e}\n"
                "DuckDB 도구(list_tables / describe_table / query / get_overview)는 "
                "정상 사용 가능합니다.\n"
                "인덱스 재구축은 rag_assembly/indexer.py 참조.")
    except Exception as e:
        return (f"RAG 검색 실패: {type(e).__name__}: {e}\n"
                "DuckDB 도구(list_tables / describe_table / query / get_overview)는 "
                "정상 사용 가능합니다.")
    return _bm25_degraded_notice() + _format_rag_results(results)


@mcp.tool()
async def rag_search(query: str, top_k: int = 5,
                source: str = "") -> str:
    """국회 데이터 (법안 본문·회의록·발언·의원 프로필) 의미 기반 하이브리드 검색.

    LanceDB(벡터) + BM25(키워드) + RRF 결합. MCP 환경에서는 reranker 비활성.
    뉴스는 인덱스에 포함되지 않음.

    Args:
        query: 자연어 쿼리 (한국어 가능)
        top_k: 반환 결과 수 (기본 5, 최대 20 권장)
        source: 필터 — 'bill' / 'bill_meta' / 'document' / 'speech' / 'member'.
                빈 문자열이면 전체.
    """
    import asyncio
    sys.stderr.write(f"[rag_search] ENTRY query={query!r}\n"); sys.stderr.flush()
    where = {"source": source.strip()} if source.strip() else None
    out = await asyncio.to_thread(_rag_search_sync, query, top_k, where)
    sys.stderr.write(f"[rag_search] RETURN len={len(out)}\n"); sys.stderr.flush()
    return out


@mcp.tool()
async def rag_search_bills(query: str, top_k: int = 5, age: int = 0) -> str:
    """법안 본문(reason+full) 의미 검색.

    Args:
        query: 자연어 쿼리
        top_k: 반환 결과 수
        age: 대수 필터 (0=전체)
    """
    import asyncio
    where = {"source": "bill"}
    if age:
        where["age"] = int(age)
    return await asyncio.to_thread(_rag_search_sync, query, top_k, where)


@mcp.tool()
async def rag_search_speeches(query: str, top_k: int = 5,
                         dae_num: str = "") -> str:
    """회의록 발언 의미 검색.

    Args:
        query: 자연어 쿼리
        top_k: 반환 결과 수
        dae_num: 대수 필터 (예: '제22대' 또는 '22'). 빈 문자열이면 전체.
    """
    import asyncio
    where = {"source": "speech"}
    if dae_num.strip():
        where["dae_num"] = dae_num.strip()
    return await asyncio.to_thread(_rag_search_sync, query, top_k, where)


@mcp.tool()
async def rag_search_documents(query: str, top_k: int = 5,
                          doc_source: str = "") -> str:
    """회의록·연구단체보고서·정책여론조사 의미 검색.

    Args:
        query: 자연어 쿼리
        top_k: 반환 결과 수
        doc_source: minutes_committee / minutes_plenary / minutes_subcommittee /
                     minutes_committee_of_whole / report / research. 빈 문자열이면 전체.
    """
    import asyncio
    where = {"source": "document"}
    if doc_source.strip():
        where["doc_source"] = doc_source.strip()
    return await asyncio.to_thread(_rag_search_sync, query, top_k, where)


@mcp.tool()
def rag_stats() -> str:
    """RAG 인덱스 현황 (LanceDB 청크 수, 소스별 분포)."""
    # BM25 게이트를 걸지 않는다 — stats는 LanceDB 카운트만 읽고 BM25를
    # 건드리지 않으므로, BM25 로딩 중·로드 실패와 무관하게 응답할 수 있다.
    unavailable = _rag_unavailable_reason()
    if unavailable:
        return unavailable
    try:
        eng = _get_rag_engine()
        s = _safe_rag_call(eng["stats"])
    except Exception as e:
        return (f"RAG 통계 조회 실패: {type(e).__name__}: {e}\n"
                "DuckDB 도구(list_tables / describe_table / query / get_overview)는 "
                "정상 사용 가능합니다.")
    out = ["=== rag_assembly 인덱스 현황 ==="]
    out.append(f"LanceDB total chunks: {s['vectordb_count']:,}")
    out.append(f"embed_config_version: {s['embed_config_version']}")
    try:
        import config as _rcfg
        import embedder as _remb
        out.append(f"embed model: {_rcfg.EMBED_MODEL} "
                   f"(dim={_rcfg.EMBED_DIM}, query_prefix={_rcfg.EMBED_QUERY_PREFIX!r}, "
                   f"device={_remb.loaded_device() or 'not loaded'}, "
                   f"warmup={_EMBED_WARMUP_STATE})")
        mism = _rcfg.embed_contract_mismatches()
        if mism:
            out.append("⚠ embed_config 계약 불일치: " + "; ".join(mism))
    except Exception as e:  # noqa: BLE001 — 통계 조회가 이것 때문에 죽으면 안 된다
        out.append(f"(embed 설정 조회 실패: {type(e).__name__}: {e})")
    out.append("\n소스별 분포:")
    for k, v in s["chunks_by_source"].items():
        out.append(f"  {k:12s} {v:>10,}")
    return "\n".join(out)


if __name__ == "__main__":
    # 경로 점검 전용 모드 — 서버를 띄우지 않으므로 stdout에 찍어도 안전하다.
    # (Windows 이관 시 "어느 경로를 보고 있는지"를 MCP 클라이언트 없이 확인.)
    if "--paths" in sys.argv[1:]:
        for _line in _path_diagnostics():
            print(_line)
        sys.exit(0)
    # BM25 로드·임베딩 모델 로드 모두 handshake와 분리 — mcp.run() 진입을 막지
    # 않는다. 둘 다 데몬 스레드이고 _BM25_LOCK으로 서로 직렬화된다.
    _start_bm25_loader()
    _start_embed_warmup()
    mcp.run()
