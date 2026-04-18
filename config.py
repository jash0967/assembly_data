import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

ASSEMBLY_API_KEY = os.environ.get("ASSEMBLY_API_KEY", "")
BASE_URL = "https://open.assembly.go.kr/portal/openapi"
PAGE_SIZE = 1000
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "assembly.duckdb")
PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "data", "assembly_progress.json")

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
    strategy: str       # "none", "age", "age_year", "year", "daily",
                        # "committee", "lookup_bill", "lookup_conf"
    table_name: str
    phase: int = 1      # 1 = 독립 수집, 2 = phase 1 데이터 필요


def _table_name(api_id: str) -> str:
    return api_id.lower()


_RAW = [
    # (api_id, 한글명, 필수파라미터, strategy, phase)
    # ── 의원 기본 ──
    ("nwvrqwxyaytdsfvhu", "국회의원 인적사항",             {}, "none",  1),
    ("nexgtxtmaamffofof", "국회의원 의원이력",             {}, "none",  1),
    ("nyzrglyvagmrypezq", "국회의원 위원회 경력",         {}, "none",  1),
    ("nzmimeepazxkubdpn", "국회의원 발의법률안",           {}, "age",   1),
    ("nuvypcdgahexhvrjt", "국회의원 상임위 활동",         {"DAE_NUM": None}, "age",   1),
    # ── 의원 활동 ──
    ("negnlnyvatsjwocar", "국회의원 SNS정보",             {}, "none",  1),
    ("nbqbmccpamsvwebkn", "국회의원 정책 세미나 개최",    {}, "year_host",  1),
    ("numwhtqhavaqssfle", "국회의원 연구단체 등록현황",    {"REGDAESU": None}, "age",  1),
    ("npbzvuwvasdqldskm", "국회의원 기자회견",             {}, "year",  1),
    # ── 표결 ──
    ("nojepdqqaweusdfbi", "국회의원 본회의 표결정보",     {}, "lookup_bill_age", 2),
    ("ncocpgfiaoituanbr", "의안별 표결현황",               {}, "age",   1),
    # ── 의안 ──
    ("BILLRCP",           "의안 접수목록",                 {}, "none",  1),
    ("BILLINFODETAIL",    "의안 상세정보",                 {}, "lookup_bill", 2),
    ("nzivskufaliivfhpb", "역대 의안 통계",               {}, "none",  1),
    # ── 청원 ──
    ("nvqbafvaajdiqhehi", "청원 계류현황",                 {}, "none",  1),
    ("ncryefyuaflxnqbqo", "청원 처리현황",                 {}, "age",   1),
    # ── 정당/위원회 ──
    ("nepjpxkkabqiqpbvk", "정당 및 교섭단체 의석수",     {}, "none",  1),
    ("nxrvzonlafugpqjuh", "위원회 현황 정보",             {}, "none",  1),  # ← committee 전에 수집
    ("nktulghcadyhmiqxi", "위원회 위원 명단",             {}, "none",  1),
    # ── 법안 심사 ──
    ("ndiwuqmpambgvnfsj", "위원회 계류법률안",             {}, "committee", 1),
    ("nwbpacrgavhjryiph", "본회의 처리안건 법률안",       {}, "age",   1),
    ("nrvsawtaauyihadij", "인사청문회",                     {}, "none",  1),
    ("nqfvrbsdafrmuzixe", "날짜별 의정활동",               {}, "daily", 1),
    ("ngytonzwavydlbbha", "전원위원회 회의록",             {}, "age_year", 1),
    # ── 예산 ──
    ("nztwkhgzakucszgls", "사업별 예산 편성 규모",       {}, "none",  1),
    # ── 회의록 ──
    ("nzbyfwhwaoanttzje", "본회의 회의록",                 {}, "age_year", 1),
    ("ncwgseseafwbuheph", "위원회 회의록",                 {}, "age_year", 1),
    ("VCONFSUBCCONFLIST", "소위원회 회의록",               {}, "none",  1),
    ("VCONFDETAIL",       "회의록별 상세정보",             {}, "lookup_conf", 2),
    ("VCONFBILLCONFLIST", "의안별 회의록 목록",           {}, "lookup_bill", 2),
    # ── 네트워크/맥락 ──
    ("nxcxrdmpaonzzbkic", "의원외교협의회 명단",           {}, "none",  1),
    ("nbicgazsalnfamoyp", "의원친선협회 명단",             {}, "none",  1),
    ("nahfbzwvatmaxscwq", "국회의원 겸직 결정 내역",     {}, "none",  1),
    ("nnzoijvcaiexypqaf", "연구단체 활동 실적",           {}, "none",  1),
    ("nmfcjtvmajsbhhckf", "국회의원 의정보고서",           {}, "none",  1),
    # ── 연구/보고서 ──
    ("nfvmtaqoaldzhobsw", "소규모 연구용역 결과보고서",   {}, "age_unit", 1),
    ("ncrwiahparxrpodcv", "연구단체 연구활동 보고서",     {"REGDAESU": None}, "age", 1),
]

APIS: list[ApiSpec] = [
    ApiSpec(
        api_id=aid,
        name_kr=name,
        required_params=req,
        strategy=strat,
        table_name=_table_name(aid),
        phase=phase,
    )
    for aid, name, req, strat, phase in _RAW
]
