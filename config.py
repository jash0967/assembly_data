import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

ASSEMBLY_API_KEY = os.environ.get("ASSEMBLY_API_KEY", "")
BASE_URL = "https://open.assembly.go.kr/portal/openapi"
PAGE_SIZE = 100
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "assembly.duckdb")
PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "data", "assembly_progress.json")

MAX_AGE = 22  # 현재 22대 국회


@dataclass(frozen=True)
class ApiSpec:
    api_id: str
    name_kr: str
    required_params: dict
    iterate_age: bool
    table_name: str


def _table_name(api_id: str) -> str:
    return api_id.lower()


_RAW = [
    # (api_id, 한글명, 필수파라미터, AGE반복여부)
    # ── 의원 ──
    ("nwvrqwxyaytdsfvhu", "국회의원 인적사항",         {},           False),
    ("nexgtxtmaamffofof", "국회의원 의원이력",         {},           False),
    ("nyzrglyvagmrypezq", "국회의원 위원회 경력",     {},           False),
    ("nzmimeepazxkubdpn", "국회의원 발의법률안",       {"AGE": 22},  True),
    ("nuvypcdgahexhvrjt", "국회의원 상임위 활동",     {},           False),
    # ── 표결 ──
    ("ojepdqqaweusdfbi",  "국회의원 본회의 표결정보", {"AGE": 22},  True),
    ("ncocpgfiaoituanbr", "의안별 표결현황",           {"AGE": 22},  True),
    # ── 의안 ──
    ("BILLRCP",           "의안 접수목록",             {},           False),
    ("BILLINFODETAIL",    "의안 상세정보",             {},           False),
    ("nzivskufaliivfhpb", "역대 의안 통계",           {},           False),
    # ── 정당/위원회 ──
    ("nepjpxkkabqiqpbvk", "정당 및 교섭단체 의석수", {},           False),
    ("nxrvzonlafugpqjuh", "위원회 현황 정보",         {},           False),
    ("nktulghcadyhmiqxi", "위원회 위원 명단",         {},           False),
    # ── 예산 ──
    ("nztwkhgzakucszgls", "사업별 예산 편성 규모",   {},           False),
    # ── 회의록 ──
    ("nzbyfwhwaoanttzje", "본회의 회의록",             {},           False),
    ("ncwgseseafwbuheph", "위원회 회의록",             {},           False),
    ("VCONFSUBCCONFLIST", "소위원회 회의록",           {},           False),
    ("VCONFDETAIL",       "회의록별 상세정보",         {},           False),
    ("VCONFBILLCONFLIST", "의안별 회의록 목록",       {},           False),
]

APIS: list[ApiSpec] = [
    ApiSpec(
        api_id=aid,
        name_kr=name,
        required_params=req,
        iterate_age=iterate,
        table_name=_table_name(aid),
    )
    for aid, name, req, iterate in _RAW
]
