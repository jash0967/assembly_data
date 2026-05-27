import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

ASSEMBLY_API_KEY = os.environ.get("ASSEMBLY_API_KEY", "")
BASE_URL = "https://open.assembly.go.kr/portal/openapi"
PAGE_SIZE = 1000
_ROOT       = os.path.dirname(__file__)
_DATA_DIR   = os.path.join(_ROOT, "data")     # 정본 원본·DB만
_OUTPUT_DIR = os.path.join(_ROOT, "output")   # 파이프라인 산출물
_CACHE_DIR  = os.path.join(_ROOT, ".cache")    # 재생성 가능한 캐시, gitignored

# 정본 데이터 (수집 원본 + DuckDB)
BILLS_KR_DIR = os.path.join(_DATA_DIR, "bills_kr")   # KR Open API: bills + docs + speeches
BILLS_US_DIR = os.path.join(_DATA_DIR, "bills_us")   # US Congress 118/119
BILLS_EU_DIR = os.path.join(_DATA_DIR, "bills_eu")   # EU AI Act + amendments
NEWS_DIR     = os.path.join(_DATA_DIR, "news")       # KR/NYT/Guardian news

# 파이프라인 산출물 (output/)
ANALYSIS_DIR    = os.path.join(_OUTPUT_DIR, "analysis")    # JSON outputs (classifications, topics)
EXPORTS_DIR     = os.path.join(_OUTPUT_DIR, "exports")     # Human-readable markdown
STABILITY_DIR   = os.path.join(_OUTPUT_DIR, "stability")   # BERTopic 안정성 실험 (npy + JSON)
FIGURES_OUT_DIR = os.path.join(_OUTPUT_DIR, "figures")     # plotly/matplotlib 시각화 산출

# 캐시 (재생성 가능, .gitignore에 등재)
CACHE_DIR              = _CACHE_DIR
BERTOPIC_EMBED_CACHE   = os.path.join(_CACHE_DIR, "bertopic_embeddings")

# DB paths
RAW_DB_PATH      = os.path.join(BILLS_KR_DIR, "assembly_raw.duckdb")
ANALYSIS_DB_PATH = os.path.join(BILLS_KR_DIR, "assembly_analysis.duckdb")
NEWS_DB_PATH     = os.path.join(NEWS_DIR,     "news.duckdb")
# Deprecated alias: legacy code paths still importing config.DB_PATH default to
# the analysis DB (which ATTACHes raw read-only). New code should pick
# RAW_DB_PATH or ANALYSIS_DB_PATH explicitly.
DB_PATH = ANALYSIS_DB_PATH
PROGRESS_FILE = os.path.join(BILLS_KR_DIR, "assembly_progress.json")

MAX_AGE = 22  # 현재 22대 국회

# 대수 → (시작연도, 종료연도) — 날짜 기반 수집에 사용
AGE_YEAR_RANGE: dict[int, tuple[int, int]] = {
    1: (1948, 1950),  2: (1950, 1954),  3: (1954, 1958),
    4: (1958, 1960),  5: (1960, 1961),  6: (1963, 1967),
    7: (1967, 1971),  8: (1971, 1972),  9: (1973, 1979),
    10: (1979, 1980), 11: (1981, 1985), 12: (1985, 1988),
    13: (1988, 1992), 14: (1992, 1996), 15: (1996, 2000),
    16: (2000, 2004), 17: (2004, 2008), 18: (2008, 2012),
    19: (2012, 2016), 20: (2016, 2020), 21: (2020, 2024),
    22: (2024, 2028),
}


@dataclass(frozen=True)
class ApiSpec:
    api_id: str
    name_kr: str
    required_params: dict
    strategy: str       # "none", "age", "age_year", "year", "year_host", "daily",
                        # "age_unit", "committee", "lookup_bill", "lookup_bill_age",
                        # "lookup_conf"
    table_name: str
    age_behavior: str   # "per_age" | "current_only" | "by_date" | "by_bill_id" | "ageless"
    age_source: str     # "constant:N" | "param:FIELD" | "column:FIELD" |
                        # "date:FIELD" | "join:TABLE.COL" | "none"
    phase: int = 1      # 1 = 독립 수집, 2 = phase 1 데이터 필요


def _table_name(api_id: str) -> str:
    return api_id.lower()


# Per-API age provenance — see PLAN_db_consolidation.md Appendix A.
# Audit-confirmed against pre_migration.json. Each row declares how `age`
# is derived for that table at write time (or post-insert for join sources).
_RAW = [
    # (api_id, 한글명, 필수파라미터, strategy, age_behavior, age_source, phase)
    # ── 의원 기본 ──
    ("nwvrqwxyaytdsfvhu", "국회의원 인적사항",         {}, "none",      "current_only", "constant:22",          1),
    ("nexgtxtmaamffofof", "국회의원 의원이력",         {}, "none",      "current_only", "column:UNIT_CD",       1),
    ("nyzrglyvagmrypezq", "국회의원 위원회 경력",      {}, "none",      "per_age",      "column:PROFILE_UNIT_CD", 1),
    ("nzmimeepazxkubdpn", "국회의원 발의법률안",       {}, "age",       "per_age",      "param:AGE",            1),
    # NB: collector.py encodes every age-strategy task as `__AGE_n` regardless
    # of the actual API param name (DAE_NUM/REGDAESU/etc.). age_source must
    # match the task_key encoding, so use param:AGE here.
    ("nuvypcdgahexhvrjt", "국회의원 상임위 활동",      {"DAE_NUM": None}, "age",   "per_age",      "param:AGE",            1),
    # ── 의원 활동 ──
    ("negnlnyvatsjwocar", "국회의원 SNS정보",          {}, "none",      "current_only", "constant:22",          1),
    ("nbqbmccpamsvwebkn", "국회의원 정책 세미나 개최", {}, "year_host", "by_date",      "date:HOST_DT",         1),
    ("numwhtqhavaqssfle", "국회의원 연구단체 등록현황", {"REGDAESU": None}, "age",  "per_age",      "param:AGE",            1),
    ("npbzvuwvasdqldskm", "국회의원 기자회견",         {}, "year",      "by_date",      "date:TAKING_DATE",     1),
    # ── 표결 ──
    ("nojepdqqaweusdfbi", "국회의원 본회의 표결정보", {}, "lookup_bill_age", "per_age", "param:AGE",            2),
    ("ncocpgfiaoituanbr", "의안별 표결현황",           {}, "age",       "per_age",      "param:AGE",            1),
    # ── 의안 ──
    ("BILLRCP",           "의안 접수목록",             {}, "none",      "per_age",      "column:ERACO",         1),
    ("BILLINFODETAIL",    "의안 상세정보",             {}, "lookup_bill", "by_bill_id", "join:billrcp.BILL_ID", 2),
    ("nzivskufaliivfhpb", "역대 의안 통계",           {}, "none",      "per_age",      "column:ERACO",         1),
    # ── 청원 ──
    ("nvqbafvaajdiqhehi", "청원 계류현황",             {}, "none",      "current_only", "constant:22",          1),
    ("ncryefyuaflxnqbqo", "청원 처리현황",             {}, "age",       "per_age",      "param:AGE",            1),
    # ── 정당/위원회 ──
    ("nepjpxkkabqiqpbvk", "정당 및 교섭단체 의석수",  {}, "none",      "current_only", "constant:22",          1),
    ("nxrvzonlafugpqjuh", "위원회 현황 정보",          {}, "none",      "current_only", "constant:22",          1),
    ("nktulghcadyhmiqxi", "위원회 위원 명단",          {}, "none",      "current_only", "constant:22",          1),
    # ── 법안 심사 ──
    ("ndiwuqmpambgvnfsj", "위원회 계류법률안",         {}, "committee", "current_only", "constant:22",          1),
    ("nwbpacrgavhjryiph", "본회의 처리안건 법률안",   {}, "age",       "per_age",      "param:AGE",            1),
    ("nrvsawtaauyihadij", "인사청문회",                 {}, "none",      "per_age",      "column:AGE",           1),
    ("nqfvrbsdafrmuzixe", "날짜별 의정활동",           {}, "daily",     "per_age",      "param:AGE",            1),
    ("ngytonzwavydlbbha", "전원위원회 회의록",         {}, "age_year",  "per_age",      "param:AGE",            1),
    # ── 예산 ──
    ("nztwkhgzakucszgls", "사업별 예산 편성 규모",    {}, "none",      "by_date",      "date:YR",              1),
    # ── 회의록 ──
    ("nzbyfwhwaoanttzje", "본회의 회의록",             {}, "age_year",  "per_age",      "param:AGE",            1),
    ("ncwgseseafwbuheph", "위원회 회의록",             {}, "age_year",  "per_age",      "param:AGE",            1),
    ("VCONFSUBCCONFLIST", "소위원회 회의록",           {}, "none",      "per_age",      "column:ERACO",         1),
    ("VCONFDETAIL",       "회의록별 상세정보",         {}, "lookup_conf", "per_age",    "column:ERACO",         2),
    ("VCONFBILLCONFLIST", "의안별 회의록 목록",        {}, "lookup_bill", "per_age",    "column:ERACO",         2),
    # ── 네트워크/맥락 ──
    ("nxcxrdmpaonzzbkic", "의원외교협의회 명단",       {}, "none",      "per_age",      "column:UNIT_CD",       1),
    ("nbicgazsalnfamoyp", "의원친선협회 명단",         {}, "none",      "per_age",      "column:UNIT_CD",       1),
    ("nahfbzwvatmaxscwq", "국회의원 겸직 결정 내역", {}, "none",      "per_age",      "column:ORD_NUM",       1),
    ("nnzoijvcaiexypqaf", "연구단체 활동 실적",       {}, "none",      "per_age",      "column:DIV",           1),
    ("nmfcjtvmajsbhhckf", "국회의원 의정보고서",       {}, "none",      "by_date",      "date:PUBLISH_DT",      1),
    # ── 연구/보고서 ──
    ("nfvmtaqoaldzhobsw", "소규모 연구용역 결과보고서", {}, "age_unit", "per_age",      "column:UNIT_CD",       1),
    ("ncrwiahparxrpodcv", "연구단체 연구활동 보고서", {"REGDAESU": None}, "age", "per_age",      "param:AGE",            1),
]

# Allow-list for validation
_VALID_AGE_BEHAVIORS = {"per_age", "current_only", "by_date", "by_bill_id", "ageless"}
_VALID_AGE_SOURCE_KINDS = {"constant", "param", "column", "date", "join", "none"}


def _validate_raw():
    seen_ids = set()
    for aid, _, _, _, behavior, source, _ in _RAW:
        if aid in seen_ids:
            raise ValueError(f"duplicate api_id in _RAW: {aid}")
        seen_ids.add(aid)
        if behavior not in _VALID_AGE_BEHAVIORS:
            raise ValueError(f"{aid}: invalid age_behavior {behavior!r}")
        kind = source.split(":", 1)[0]
        if kind not in _VALID_AGE_SOURCE_KINDS:
            raise ValueError(f"{aid}: invalid age_source kind {kind!r}")


_validate_raw()


APIS: list[ApiSpec] = [
    ApiSpec(
        api_id=aid,
        name_kr=name,
        required_params=req,
        strategy=strat,
        table_name=_table_name(aid),
        age_behavior=behavior,
        age_source=source,
        phase=phase,
    )
    for aid, name, req, strat, behavior, source, phase in _RAW
]
