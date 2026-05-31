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
"""
import os
import sys

# (1) 진행률·로그 출력 차단 (FastMCP import 전에 설정해야 효과)
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TQDM_DISABLE", "1")
# HF Hub HEAD request로 캐시 신선도 검증하면 FastMCP context에서 hang.
# offline 모드로 강제 → 로컬 캐시만 사용.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

# logging 레벨 (HF 비인증 warning 등 차단)
import logging as _logging
_logging.getLogger("huggingface_hub").setLevel(_logging.ERROR)
_logging.getLogger("transformers").setLevel(_logging.ERROR)
_logging.getLogger("sentence_transformers").setLevel(_logging.ERROR)

# (2) 노이즈를 stdout 대신 stderr로 흘려보낸 뒤 RAG 라이브러리 사전 import.
#     mcp.run()이 시작된 후에 stdout으로 출력되면 protocol이 깨지므로,
#     startup 시점에 일괄로 처리.
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
        # BM25 13 sub-index 미리 로드 (standalone ~10초). startup에서 끝내야
        # 첫 RAG 호출이 빠름. FastMCP context에 들어가기 전이라 안전.
        _eng = _get_engine()
        _eng._ensure_bm25()
        sys.stderr.write("[mcp] RAG preimport + BM25 load done\n")
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
_BILLS_KR_DIR = os.path.join(_REPO_ROOT, "data", "bills_kr")
_NEWS_DIR     = os.path.join(_REPO_ROOT, "data", "news")
RAW_DB_PATH           = os.path.join(_BILLS_KR_DIR, "assembly_raw.duckdb")
ANALYSIS_DB_PATH      = os.path.join(_BILLS_KR_DIR, "assembly_analysis.duckdb")
NEWS_RAW_DB_PATH      = os.path.join(_NEWS_DIR, "news.duckdb")
NEWS_ANALYSIS_DB_PATH = os.path.join(_NEWS_DIR, "news_analysis.duckdb")
# 호환용 alias
DB_PATH = ANALYSIS_DB_PATH

mcp = FastMCP("assembly-db")

# 카탈로그 화이트리스트 — list_tables / describe_table 검색 범위
_CATALOGS = ("assembly_analysis", "raw", "news_analysis", "news_raw")


def _open() -> duckdb.DuckDBPyConnection:
    """analysis DB를 read-only로 열고 raw + 뉴스 두 DB를 ATTACH.

    같은 process에서 _open()이 여러 번 호출될 때 storage manager가
    "이미 등록됨" BinderException을 던지는 경우가 있으므로 attach는 idempotent.
    """
    con = duckdb.connect(ANALYSIS_DB_PATH, read_only=True)
    for path, alias in (
        (RAW_DB_PATH, "raw"),
        (NEWS_ANALYSIS_DB_PATH, "news_analysis"),
        (NEWS_RAW_DB_PATH, "news_raw"),
    ):
        try:
            con.execute(f"ATTACH '{path}' AS {alias} (READ_ONLY)")
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


def _get_rag_engine():
    """rag_assembly.api 싱글턴 반환. startup에서 미리 import한 것을 사용."""
    global _rag_engine
    if _rag_engine is None:
        if _PREIMPORTED_API is not None:
            _rag_engine = _PREIMPORTED_API
        else:
            # fallback (preimport 실패한 경우만): fd 레벨 redirect 후 import
            import sys, os
            saved = os.dup(1); os.dup2(2, 1)
            try:
                rag_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "rag_assembly")
                if rag_path not in sys.path:
                    sys.path.insert(0, rag_path)
                import _bootstrap  # noqa: F401
                from api import (search, search_bills, search_bill_metas,
                                  search_speeches, search_documents,
                                  search_members, lookup_member_by_name,
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
            finally:
                os.dup2(saved, 1); os.close(saved)
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


def _simple_embed_query(query: str) -> list:
    """FastMCP context에서 모든 네트워크 호출이 hang하는 문제를 회피하기 위해
    subprocess로 외부 Python을 spawn해서 embedding 받아옴.
    """
    import json
    import subprocess
    py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "venv", "Scripts", "python.exe")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "rag_assembly", "_subproc_embed.py")
    r = subprocess.run(
        [py, script],
        input=query,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"embed subprocess failed: {r.stderr}")
    return json.loads(r.stdout)["vector"]


def _hybrid_no_rerank(query: str, top_k: int, where: dict | None) -> list:
    """MCP 환경: subprocess로 embed 한 뒤 hybrid 검색 (rerank 없음).
    reranker 로드가 FastMCP context에서 hang하므로 dense+BM25+RRF만 사용."""
    import time
    from api import _get_engine
    import config as _cfg

    t0 = time.time()
    eng_obj = _get_engine()
    sys.stderr.write(f"[rag] eng_obj acquired {time.time()-t0:.2f}s\n")
    sys.stderr.flush()

    # Step 1a: embed query via raw HTTP (google-genai hangs in FastMCP context)
    t = time.time()
    sys.stderr.write(f"[rag] >>> embed_query (raw HTTP) starting\n"); sys.stderr.flush()
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
    t = time.time()
    try:
        b_results = eng_obj.bm25_search(query, top_k=_cfg.BM25_TOP_K)
    except FileNotFoundError:
        b_results = []
    sys.stderr.write(f"[rag] bm25_search {time.time()-t:.2f}s ({len(b_results)} hits)\n")
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
    _get_rag_engine()
    results = _hybrid_no_rerank(query, top_k, where)
    return _format_rag_results(results)


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
    eng = _get_rag_engine()
    s = _safe_rag_call(eng["stats"])
    out = ["=== rag_assembly 인덱스 현황 ==="]
    out.append(f"LanceDB total chunks: {s['vectordb_count']:,}")
    out.append(f"embed_config_version: {s['embed_config_version']}")
    out.append("\n소스별 분포:")
    for k, v in s["chunks_by_source"].items():
        out.append(f"  {k:12s} {v:>10,}")
    return "\n".join(out)


if __name__ == "__main__":
    mcp.run()
