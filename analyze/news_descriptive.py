"""국내 6개 매체 AI 보도 종합 분석 — 보고서 §4.1 데이터·시각화·마크다운 백본.

원래 descriptive-only 의도라 파일명이 `news_descriptive`지만, 이제 국내 언론
AI 보도의 **전체 분석**을 한 파일에 담는다:

    1. 보도량 시계열 (월별 추이 + 12개월 이동평균 + 이벤트 마커)
    2. AI 기본법 통과 전후 윈도우 (±24개월 매체별 월평균 변화)
    3. 정책 속성 구성 (매체별 / 연도별 — 10속성 Carvão 프레임)
    4. 키워드 추세
    5. BERTopic 소주제 통계 — **placeholder** (별도 파이프라인 완성 후 결합)

산출물은 전부 config 상수 경로로 나간다:
    - 시각화 PNG  → config.FIGURES_OUT_DIR  (output/figures/news_*.png)
    - 최종 마크다운 → config.OUTPUT_DIR     (output/news_analysis.md)

**기준 데이터**: data/news/news_analysis.duckdb (Stage 1+2 적용 cleaned subset).
분류는 `news_classifications`의 *현재 버전*(최신 classified_at의
prompt_version × cleaning_version)만 본다 — 재분류가 진행 중이어도 완료분만
반영하고, 커버리지(classified / total)를 함께 보고한다.

Public data functions (모두 @lru_cache, 반복 호출 안전):
    load_classification_coverage() -> dict
    load_monthly_counts()          -> dict[provider, list[(yyyymm, count)]]
    load_monthly_total_ma(window)  -> list[(yyyymm, count, ma)]
    load_provider_year_matrix()    -> (providers, years, matrix)
    load_event_window(date, months)-> dict
    load_attr_distribution()       -> list[(attr_en, count, share_excl_none)]
    load_attr_by_provider()        -> (providers, attrs_en, count_matrix)
    load_attr_by_year()            -> (years, attrs_en, share_matrix)
    load_keyword_trend(keywords)   -> dict[keyword, list[(yyyymm, hits)]]
    load_subtopic_stats()          -> None   # TODO: BERTopic 결합

CLI:
    python analyze/news_descriptive.py --summary   # 콘솔 요약
    python analyze/news_descriptive.py --figures   # output/figures/news_*.png
    python analyze/news_descriptive.py --report    # output/news_analysis.md
    python analyze/news_descriptive.py --all       # figures + report
"""

from __future__ import annotations

import csv
import os
import re
import sys
from functools import lru_cache

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Self-contained sys.path setup so module works whether run as script or imported.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import duckdb

import config
from prompts import ATTRIBUTES  # 정본 10속성 (영문 라벨)

# ──────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────

# 매체 출력 순서 (시각화 일관성)
PROVIDER_ORDER = ["KBS", "MBC", "SBS", "YTN", "중앙일보", "한겨레"]

# 영문 Carvão 속성 → 보고서용 짧은 한글 라벨.
ATTR_KR: dict[str, str] = {
    "Industrial policy": "산업정책",
    "Public interest": "공익",
    "National security": "안보",
    "Labor": "노동",
    "Responsible and ethical AI": "책임과 윤리",
    "Market efficiency and power concentration (antitrust)": "시장경쟁",
    "International collaboration": "국제협력",
    "Elections": "선거",
    "Safety": "안전성",
    "Copyright": "저작권",
    "none": "미분류",
}

# 보고서 §4.1 표시 순서 (KR 뉴스 비중 내림차순). prompts.ATTRIBUTES와 동일 집합.
ATTR_EN_ORDER = [
    "Industrial policy", "Public interest", "National security", "Labor",
    "Responsible and ethical AI",
    "Market efficiency and power concentration (antitrust)",
    "International collaboration", "Elections", "Safety", "Copyright",
]
assert set(ATTR_EN_ORDER) == set(ATTRIBUTES), "ATTR_EN_ORDER가 prompts.ATTRIBUTES와 불일치"

# 시계열 이벤트 마커 (담론 변곡점).
EVENTS: list[tuple[str, str]] = [
    ("ChatGPT 출시", "2022-11-30"),
    ("AI 기본법 통과", "2024-12-26"),
]
AIBASIC_DATE = "2024-12-26"  # 그림6 윈도우 기준


KEYWORDS: dict[str, list[str]] = {
    "AI 기술": [
        "인공지능", "AI", "딥러닝", "머신러닝", "신경망",
        "ChatGPT", "GPT", "생성형", "LLM", "거대언어모델",
        "트랜스포머", "멀티모달", "에이전트",
    ],
    "정책·규제": [
        "AI 기본법", "AI법", "규제", "윤리", "신뢰",
        "안전", "투명성", "편향", "차별", "책임", "거버넌스",
    ],
    "응용·사회": [
        "딥페이크", "자율주행", "안면인식", "챗봇", "교육",
        "노동", "일자리", "저작권", "개인정보", "선거",
    ],
    "주체": [
        "OpenAI", "구글", "메타", "Anthropic", "엔비디아",
        "삼성", "네이버", "카카오", "LG", "현대",
    ],
}


# ──────────────────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────────────────

def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH, read_only=True)
    # 1~3분 쿼리에서 stderr가 progress bar로 도배되는 걸 방지
    try:
        con.execute("PRAGMA disable_progress_bar")
    except Exception:
        pass
    return con


@lru_cache(maxsize=1)
def _current_version() -> tuple[str, str]:
    """분류의 '현재 버전' = 최신 classified_at의 (prompt_version, cleaning_version).

    재분류가 진행 중이어도 가장 최근 빌드의 완료분만 본다. 분류가 하나도 없으면
    ('', '') 반환 (DB가 재분류 초기 상태일 수 있음)."""
    con = _con()
    try:
        row = con.execute("""
            SELECT prompt_version, cleaning_version
            FROM news_classifications
            GROUP BY 1, 2
            ORDER BY MAX(classified_at) DESC
            LIMIT 1
        """).fetchone()
    finally:
        con.close()
    return (row[0], row[1]) if row else ("", "")


def _clf_cte() -> str:
    """현재 버전의 성공 분류만 기사 메타와 조인한 CTE `clf`.

    사용처: f-string으로 본문에 끼워 넣은 뒤 뒤에 추가 SELECT를 붙인다.
    파라미터(prompt_version, cleaning_version)는 별도로 바인딩한다."""
    return """
        WITH clf AS (
            SELECT a.news_id, a.provider, a.published_at,
                   c.primary_attr, c.secondary_attr, c.tertiary_attr
            FROM news_classifications c
            JOIN news_articles a USING (news_id)
            WHERE c.prompt_version = ? AND c.cleaning_version = ?
              AND c.error IS NULL
        )
    """


# ──────────────────────────────────────────────────────────────────────────
# 1. 분류 커버리지 (재분류 진행 중 상태 보고)
# ──────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_classification_coverage() -> dict:
    """현재 버전 분류 커버리지. 반환:
        {total_articles, classified, pct, prompt_version, cleaning_version,
         date_min, date_max}.
    """
    pv, cv = _current_version()
    con = _con()
    try:
        total = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
        dmin, dmax = con.execute(
            "SELECT MIN(published_at), MAX(published_at) FROM news_articles"
        ).fetchone()
        clf = 0
        if pv:
            clf = con.execute(
                """SELECT COUNT(*) FROM news_classifications
                   WHERE prompt_version = ? AND cleaning_version = ?
                     AND error IS NULL""",
                [pv, cv],
            ).fetchone()[0]
    finally:
        con.close()
    return {
        "total_articles": int(total),
        "classified": int(clf),
        "pct": (clf / total * 100) if total else 0.0,
        "prompt_version": pv,
        "cleaning_version": cv,
        "date_min": dmin,
        "date_max": dmax,
    }


# ──────────────────────────────────────────────────────────────────────────
# 2. 보도량 시계열
# ──────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_monthly_counts() -> dict[str, list[tuple[str, int]]]:
    """매체별 월별 보도량 (cleaned subset 전체, 분류 여부 무관).
    반환: {provider: [(yyyymm, count), ...]}."""
    con = _con()
    try:
        rows = con.execute("""
            SELECT provider, strftime(published_at, '%Y-%m') AS ym, COUNT(*) AS c
            FROM news_articles
            GROUP BY provider, ym
            ORDER BY provider, ym
        """).fetchall()
    finally:
        con.close()
    out: dict[str, list[tuple[str, int]]] = {p: [] for p in PROVIDER_ORDER}
    for prov, ym, c in rows:
        out.setdefault(prov, []).append((ym, int(c)))
    return out


@lru_cache(maxsize=8)
def load_monthly_total_ma(window: int = 12) -> list[tuple[str, int, float]]:
    """전체 월별 보도량 + N개월 후행 이동평균. 빠진 달은 0으로 채워 연속 인덱스 보장.
    반환: [(yyyymm, count, moving_avg), ...] (시간순)."""
    con = _con()
    try:
        rows = con.execute("""
            SELECT strftime(published_at, '%Y-%m') AS ym, COUNT(*) AS c
            FROM news_articles
            GROUP BY ym ORDER BY ym
        """).fetchall()
    finally:
        con.close()
    if not rows:
        return []
    counts = {ym: int(c) for ym, c in rows}
    months = _month_range(rows[0][0], rows[-1][0])
    series = [(m, counts.get(m, 0)) for m in months]
    out: list[tuple[str, int, float]] = []
    for i, (m, c) in enumerate(series):
        lo = max(0, i - window + 1)
        chunk = [x[1] for x in series[lo:i + 1]]
        out.append((m, c, sum(chunk) / len(chunk)))
    return out


@lru_cache(maxsize=1)
def load_provider_year_matrix() -> tuple[list[str], list[int], list[list[int]]]:
    """매체×연도 카운트 매트릭스. 반환: (providers, years, matrix[provider][year])."""
    con = _con()
    try:
        rows = con.execute("""
            SELECT provider,
                   CAST(EXTRACT(YEAR FROM published_at) AS INTEGER) AS y,
                   COUNT(*) AS c
            FROM news_articles
            GROUP BY provider, y
            ORDER BY provider, y
        """).fetchall()
    finally:
        con.close()
    providers = PROVIDER_ORDER
    years = sorted({r[1] for r in rows})
    p_idx = {p: i for i, p in enumerate(providers)}
    y_idx = {y: i for i, y in enumerate(years)}
    mat = [[0] * len(years) for _ in providers]
    for prov, y, c in rows:
        if prov in p_idx:
            mat[p_idx[prov]][y_idx[y]] = int(c)
    return providers, years, mat


@lru_cache(maxsize=8)
def load_event_window(event_date: str = AIBASIC_DATE, months: int = 24) -> dict:
    """이벤트 전후 ±N개월 매체별 월평균 보도량 변화 (그림6).

    월평균 = 윈도우 내 합계 / 윈도우 내 '데이터가 존재하는 달 수'. (데이터 범위
    경계에서 윈도우가 잘려도 robust). 반환:
        {event_date, months, partial: bool,
         providers: {prov: {before_avg, after_avg, pct, before_months, after_months}},
         overall: {before_avg, after_avg, pct}}.
    """
    con = _con()
    try:
        rows = con.execute(
            """
            SELECT provider, strftime(published_at, '%Y-%m') AS ym, COUNT(*) c
            FROM news_articles
            WHERE published_at >= CAST(? AS DATE) - (CAST(? AS INTEGER) * INTERVAL 1 MONTH)
              AND published_at <  CAST(? AS DATE) + (CAST(? AS INTEGER) * INTERVAL 1 MONTH)
            GROUP BY provider, ym
            """,
            [event_date, months, event_date, months],
        ).fetchall()
        # 전체 데이터 범위가 윈도우를 다 덮는지 (partial 여부 판단용)
        dmin, dmax = con.execute(
            "SELECT MIN(published_at), MAX(published_at) FROM news_articles"
        ).fetchone()
    finally:
        con.close()

    ev_ym = event_date[:7]

    def _accumulate(filter_prov):
        before_sum = after_sum = 0
        before_m, after_m = set(), set()
        for prov, ym, c in rows:
            if filter_prov is not None and prov != filter_prov:
                continue
            if ym < ev_ym:
                before_sum += c
                before_m.add(ym)
            else:
                after_sum += c
                after_m.add(ym)
        b_avg = before_sum / len(before_m) if before_m else 0.0
        a_avg = after_sum / len(after_m) if after_m else 0.0
        pct = ((a_avg - b_avg) / b_avg * 100) if b_avg else float("nan")
        return {
            "before_avg": b_avg, "after_avg": a_avg, "pct": pct,
            "before_months": len(before_m), "after_months": len(after_m),
        }

    providers = {p: _accumulate(p) for p in PROVIDER_ORDER}
    overall = _accumulate(None)

    # 윈도우가 데이터 범위를 벗어나면 partial=True (월평균이 잘린 표본 기반)
    from datetime import date
    y, m, d = (int(x) for x in event_date.split("-"))
    lo_y, lo_m = y - months // 12, m - months % 12
    if lo_m <= 0:
        lo_y -= 1
        lo_m += 12
    hi_y, hi_m = y + months // 12, m + months % 12
    if hi_m > 12:
        hi_y += 1
        hi_m -= 12
    partial = bool(
        dmin and dmax and (
            date(lo_y, lo_m, 1) < dmin.date().replace(day=1)
            or date(hi_y, hi_m, 1) > dmax.date().replace(day=1)
        )
    )

    return {
        "event_date": event_date, "months": months, "partial": partial,
        "providers": providers, "overall": overall,
    }


# ──────────────────────────────────────────────────────────────────────────
# 3. 정책 속성 구성 (news_classifications)
# ──────────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_attr_distribution() -> list[tuple[str, int, float]]:
    """현재 버전 1차 속성 분포. share는 'none/미분류 제외' 분모 기준 (보고서 정의).
    반환: [(attr_en, count, share_excl_none), ...] — count 내림차순. 'none' 행도
    포함되며 그 share는 0.0으로 표기 (분모에서 빠지므로)."""
    pv, cv = _current_version()
    if not pv:
        return []
    con = _con()
    try:
        rows = con.execute(
            _clf_cte() + """
            SELECT primary_attr, COUNT(*) c
            FROM clf GROUP BY 1 ORDER BY c DESC
            """,
            [pv, cv],
        ).fetchall()
    finally:
        con.close()
    denom = sum(int(c) for a, c in rows if a != "none")
    out = []
    for a, c in rows:
        share = (int(c) / denom * 100) if (denom and a != "none") else 0.0
        out.append((a, int(c), share))
    return out


@lru_cache(maxsize=1)
def load_attr_by_provider() -> tuple[list[str], list[str], list[list[int]]]:
    """매체×1차속성 카운트 매트릭스 (그림7). none 포함.
    반환: (providers, attrs_en, matrix[provider][attr])  — attrs_en은
    ATTR_EN_ORDER + (존재 시) 'none'."""
    pv, cv = _current_version()
    attrs = ATTR_EN_ORDER + ["none"]
    providers = PROVIDER_ORDER
    mat = [[0] * len(attrs) for _ in providers]
    if not pv:
        return providers, attrs, mat
    con = _con()
    try:
        rows = con.execute(
            _clf_cte() + """
            SELECT provider, primary_attr, COUNT(*) c
            FROM clf GROUP BY 1, 2
            """,
            [pv, cv],
        ).fetchall()
    finally:
        con.close()
    p_idx = {p: i for i, p in enumerate(providers)}
    a_idx = {a: i for i, a in enumerate(attrs)}
    for prov, attr, c in rows:
        if prov in p_idx and attr in a_idx:
            mat[p_idx[prov]][a_idx[attr]] = int(c)
    return providers, attrs, mat


@lru_cache(maxsize=1)
def load_attr_by_year() -> tuple[list[int], list[str], list[list[float]], list[list[int]]]:
    """연도×1차속성 share·count 매트릭스 (share는 none 제외 분모). 그림7·연도추세용.
    반환: (years, attrs_en, share_matrix[year][attr], count_matrix[year][attr])
          — attrs_en = ATTR_EN_ORDER."""
    pv, cv = _current_version()
    attrs = ATTR_EN_ORDER
    if not pv:
        return [], attrs, [], []
    con = _con()
    try:
        rows = con.execute(
            _clf_cte() + """
            SELECT CAST(EXTRACT(YEAR FROM published_at) AS INTEGER) y,
                   primary_attr, COUNT(*) c
            FROM clf GROUP BY 1, 2
            """,
            [pv, cv],
        ).fetchall()
    finally:
        con.close()
    years = sorted({int(y) for y, a, c in rows})
    y_idx = {y: i for i, y in enumerate(years)}
    a_idx = {a: i for i, a in enumerate(attrs)}
    counts = [[0] * len(attrs) for _ in years]
    denom = [0] * len(years)
    for y, attr, c in rows:
        yi = y_idx[int(y)]
        if attr != "none":
            denom[yi] += int(c)
        if attr in a_idx:
            counts[yi][a_idx[attr]] = int(c)
    share = [
        [(counts[yi][ai] / denom[yi] * 100 if denom[yi] else 0.0)
         for ai in range(len(attrs))]
        for yi in range(len(years))
    ]
    return years, attrs, share, counts


# ──────────────────────────────────────────────────────────────────────────
# 4. 키워드 추세
# ──────────────────────────────────────────────────────────────────────────

def _build_keyword_case_clause(keyword: str, alias: str) -> str:
    """단일 키워드에 대한 SUM(CASE WHEN ...) 표현. ASCII 짧은 단어는 word boundary 적용.

    cleaned DB의 `content` 컬럼은 Stage 1 적용본이므로 boilerplate 매칭이 자연
    배제된다 (별도 cleaning 인터폴레이션 불필요)."""
    safe = keyword.replace("'", "''")
    is_short_ascii = len(keyword) <= 3 and keyword.isascii() and keyword.isalpha()
    if is_short_ascii or keyword.upper() == "AI":
        patt = re.escape(keyword).replace("'", "''")
        clause = (
            f"SUM(CASE WHEN regexp_matches(content, '(?i)(^|[^A-Za-z0-9])"
            f"{patt}([^A-Za-z0-9]|$)') THEN 1 ELSE 0 END) AS {alias}"
        )
    else:
        clause = (
            f"SUM(CASE WHEN content ILIKE '%{safe}%' "
            f"OR title ILIKE '%{safe}%' THEN 1 ELSE 0 END) AS {alias}"
        )
    return clause


def load_keyword_trend(
    keywords: dict[str, list[str]] | None = None,
) -> dict[str, list[tuple[str, int]]]:
    """월별로 각 키워드를 포함한 기사 수.
    keywords: {group: [keyword, ...]}. None이면 모듈 상수 KEYWORDS.
    반환: {keyword: [(yyyymm, hits), ...]}."""
    if keywords is None:
        keywords = KEYWORDS
    flat: list[str] = []
    for kws in keywords.values():
        for k in kws:
            if k not in flat:
                flat.append(k)
    aliases = [f"kw_{i:03d}" for i in range(len(flat))]
    clauses = [_build_keyword_case_clause(k, a) for k, a in zip(flat, aliases)]
    sql = f"""
        SELECT strftime(published_at, '%Y-%m') AS ym,
               {", ".join(clauses)}
        FROM news_articles
        GROUP BY ym ORDER BY ym
    """
    con = _con()
    try:
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    out: dict[str, list[tuple[str, int]]] = {k: [] for k in flat}
    for row in rows:
        ym = row[0]
        for i, kw in enumerate(flat):
            out[kw].append((ym, int(row[1 + i])))
    return out


# ──────────────────────────────────────────────────────────────────────────
# 5. BERTopic 소주제 — PLACEHOLDER
# ──────────────────────────────────────────────────────────────────────────

def load_subtopic_stats():
    """[PLACEHOLDER] BERTopic 소주제 통계.

    analyze/subtopic_bertopic.py 파이프라인이 속성별 소주제·분기별 Top-10을
    산출하면 여기서 그 결과를 읽어 (속성 × 소주제 × 시기) 통계로 결합한다.
    현재는 미구현 — None 반환."""
    return None


# ──────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────

def _month_range(start_ym: str, end_ym: str) -> list[str]:
    """'YYYY-MM' 두 끝점 사이의 모든 달 (포함)."""
    sy, sm = (int(x) for x in start_ym.split("-"))
    ey, em = (int(x) for x in end_ym.split("-"))
    out = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y += 1
            m = 1
    return out


def kr(attr_en: str) -> str:
    """영문 속성 라벨 → 한글. 미등록은 원문 유지."""
    return ATTR_KR.get(attr_en, attr_en)


# ──────────────────────────────────────────────────────────────────────────
# 시각화 (matplotlib → config.FIGURES_OUT_DIR)
# ──────────────────────────────────────────────────────────────────────────

def _setup_korean_font():
    """한글 폰트 등록. config.KO_FONT_PATH(번들/캐시) → 시스템 설치 → 마운트 순.
    아무 것도 못 찾으면 경고만 하고 진행 (한글이 □로 보일 수 있음)."""
    import matplotlib
    import matplotlib.font_manager as fm
    import matplotlib.pyplot as plt

    plt.rcParams["axes.unicode_minus"] = False

    # 1) 캐시/번들 폰트 파일
    path = config.KO_FONT_PATH
    if not os.path.exists(path):
        # 2) WSL 마운트 등에서 한 번 자동 복사 시도 (재생성 가능 자산)
        for cand in _discover_font_files():
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                import shutil
                shutil.copy(cand, path)
                break
            except Exception:
                continue
    if os.path.exists(path):
        try:
            fm.fontManager.addfont(path)
            plt.rcParams["font.family"] = fm.FontProperties(fname=path).get_name()
            return
        except Exception:
            pass
    # 3) 시스템 설치 폰트 이름
    names = {f.name for f in fm.fontManager.ttflist}
    for cand in ["NanumGothic", "Noto Sans CJK KR", "Malgun Gothic", "AppleGothic"]:
        if cand in names:
            plt.rcParams["font.family"] = cand
            return
    print("  [warn] 한글 폰트를 찾지 못함 — 그림의 한글이 깨질 수 있습니다.", flush=True)


def _discover_font_files() -> list[str]:
    """시스템·마운트에서 NanumGothic.ttf 후보 경로 탐색."""
    cands = [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/mnt/d/backup_2025/나눔 글꼴/나눔고딕/NanumFontSetup_TTF_GOTHIC/NanumGothic.ttf",
    ]
    return [c for c in cands if os.path.exists(c)]


def _fig_path(name: str) -> str:
    os.makedirs(config.FIGURES_OUT_DIR, exist_ok=True)
    return os.path.join(config.FIGURES_OUT_DIR, name)


def fig_decade_trend() -> str:
    """그림5: 전체 월별 보도량 + 12개월 이동평균 + 이벤트 마커."""
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime

    series = load_monthly_total_ma(12)
    xs = [datetime.strptime(m, "%Y-%m") for m, _, _ in series]
    counts = [c for _, c, _ in series]
    ma = [a for _, _, a in series]

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.bar(xs, counts, width=20, color="#cdd8e6", label="월별 보도량")
    ax.plot(xs, ma, color="#1f4e79", lw=2.2, label="12개월 이동평균")
    for label, d in EVENTS:
        dt = datetime.strptime(d, "%Y-%m-%d")
        ax.axvline(dt, color="#c0392b", ls="--", lw=1.2, alpha=0.8)
        ax.text(dt, ax.get_ylim()[1] * 0.95, "  " + label, rotation=90,
                va="top", ha="left", fontsize=9, color="#c0392b")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_ylabel("기사 수")
    ax.set_title("국내 6개 언론사 AI 보도 월별 추이 + 12개월 이동평균")
    ax.legend(loc="upper left")
    plt.tight_layout()
    out = _fig_path("news_decade_trend.png")
    plt.savefig(out, dpi=130, bbox_inches="tight")
    plt.close()
    return out


def fig_event_window() -> str:
    """그림6: AI 기본법 통과 ±24개월 매체별 월평균 변화율 (%)."""
    import matplotlib.pyplot as plt

    win = load_event_window(AIBASIC_DATE, 24)
    provs = [p for p in PROVIDER_ORDER if p in win["providers"]]
    pcts = [win["providers"][p]["pct"] for p in provs]

    fig, ax = plt.subplots(figsize=(9, 5))
    colors = ["#2e7d32" if v >= 0 else "#c62828" for v in pcts]
    bars = ax.bar(provs, pcts, color=colors)
    for b, v in zip(bars, pcts):
        ax.text(b.get_x() + b.get_width() / 2, v + (3 if v >= 0 else -3),
                f"{v:+.0f}%", ha="center",
                va="bottom" if v >= 0 else "top", fontsize=9)
    ov = win["overall"]["pct"]
    ax.axhline(ov, color="#1f4e79", ls="--", lw=1.3,
               label=f"전체 평균 {ov:+.0f}%")
    sub = " (데이터 범위 절단 — 부분 윈도우)" if win["partial"] else ""
    ax.set_ylabel("월평균 보도량 변화율 (%)")
    ax.set_title(f"AI 기본법 통과({AIBASIC_DATE}) 전후 ±24개월 매체별 변화{sub}")
    ax.legend()
    plt.tight_layout()
    out = _fig_path("news_event_window.png")
    plt.savefig(out, dpi=130, bbox_inches="tight")
    plt.close()
    return out


def fig_provider_attr() -> str:
    """그림7: 매체별 정책 속성 구성 (100% 누적 막대, none 제외)."""
    import matplotlib.pyplot as plt

    providers, attrs, mat = load_attr_by_provider()
    # none 제외 share
    attrs_no_none = [a for a in attrs if a != "none"]
    none_i = attrs.index("none") if "none" in attrs else None
    shares = []  # [provider][attr]
    for pi, _ in enumerate(providers):
        denom = sum(mat[pi][ai] for ai, a in enumerate(attrs) if a != "none")
        shares.append([
            (mat[pi][attrs.index(a)] / denom * 100 if denom else 0.0)
            for a in attrs_no_none
        ])

    fig, ax = plt.subplots(figsize=(11, 6))
    cmap = plt.get_cmap("tab10")
    bottoms = [0.0] * len(providers)
    for ai, a in enumerate(attrs_no_none):
        vals = [shares[pi][ai] for pi in range(len(providers))]
        ax.barh(providers, vals, left=bottoms, color=cmap(ai % 10), label=kr(a))
        bottoms = [bottoms[pi] + vals[pi] for pi in range(len(providers))]
    ax.set_xlabel("속성 비중 (%, 미분류 제외)")
    ax.set_xlim(0, 100)
    ax.invert_yaxis()
    ax.set_title("국내 6개 언론사 매체별 정책 속성 구성")
    ax.legend(ncol=2, fontsize=8, loc="lower right")
    plt.tight_layout()
    out = _fig_path("news_provider_attr.png")
    plt.savefig(out, dpi=130, bbox_inches="tight")
    plt.close()
    return out


def fig_attr_year_trend() -> str:
    """연도별 정책 속성 share 추세 (라인, none 제외 분모)."""
    import matplotlib.pyplot as plt

    years, attrs, share, _counts = load_attr_by_year()
    fig, ax = plt.subplots(figsize=(12, 6))
    cmap = plt.get_cmap("tab10")
    for ai, a in enumerate(attrs):
        ys = [share[yi][ai] for yi in range(len(years))]
        ax.plot(years, ys, marker="o", ms=3, color=cmap(ai % 10), label=kr(a))
    ax.set_xlabel("연도")
    ax.set_ylabel("속성 비중 (%, 미분류 제외)")
    ax.set_title("연도별 정책 속성 구성 추세")
    ax.legend(ncol=2, fontsize=8)
    plt.tight_layout()
    out = _fig_path("news_attr_year_trend.png")
    plt.savefig(out, dpi=130, bbox_inches="tight")
    plt.close()
    return out


def fig_subtopic():
    """[PLACEHOLDER] BERTopic 소주제 시각화 — 미구현."""
    return None


def generate_figures() -> list[str]:
    """모든 figure를 config.FIGURES_OUT_DIR에 생성. 생성된 경로 리스트 반환."""
    _setup_korean_font()
    paths = []
    for fn in (fig_decade_trend, fig_event_window, fig_provider_attr,
               fig_attr_year_trend):
        try:
            p = fn()
            paths.append(p)
            print(f"  → {p}", flush=True)
        except Exception as e:
            print(f"  [skip] {fn.__name__}: {e}", flush=True)
    # BERTopic placeholder
    print("  [TODO] BERTopic 소주제 figure — 파이프라인 완성 후 추가", flush=True)
    # 출판사 인계용 원자료 CSV는 그림과 항상 함께 생성
    print("Figure source data (출판사 인계용):", flush=True)
    export_figure_sources()
    return paths


# ──────────────────────────────────────────────────────────────────────────
# 출판사 인계용 그림 원자료 (CSV → config.FIGURES_SOURCE_DIR)
# ──────────────────────────────────────────────────────────────────────────
#
# 시각화 PNG는 최종 보고서에 직접 들어가지 않는다 — 출판사가 자체 작도하므로
# 각 그림이 그린 *원 데이터*를 그림과 1:1로 대응하는 CSV로 넘긴다.
# Excel 호환을 위해 utf-8-sig(BOM) 인코딩으로 쓴다.

def _source_path(name: str) -> str:
    os.makedirs(config.FIGURES_SOURCE_DIR, exist_ok=True)
    return os.path.join(config.FIGURES_SOURCE_DIR, name)


def _write_csv(name: str, header: list[str], rows: list[list]) -> str:
    path = _source_path(name)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def src_decade_trend() -> str:
    """그림: news_decade_trend → 월·월별보도량·12개월이동평균."""
    rows = [[m, c, round(ma, 2)] for m, c, ma in load_monthly_total_ma(12)]
    return _write_csv("news_decade_trend.csv",
                      ["month", "monthly_count", "moving_avg_12m"], rows)


def src_event_window() -> str:
    """그림: news_event_window → 매체별 AI기본법 ±24개월 전/후 월평균·변화율."""
    win = load_event_window(AIBASIC_DATE, 24)
    rows = []
    for p in PROVIDER_ORDER:
        if p in win["providers"]:
            d = win["providers"][p]
            rows.append([p, round(d["before_avg"], 2), round(d["after_avg"], 2),
                         round(d["pct"], 2), d["before_months"], d["after_months"]])
    ov = win["overall"]
    rows.append(["전체", round(ov["before_avg"], 2), round(ov["after_avg"], 2),
                 round(ov["pct"], 2), "", ""])
    return _write_csv(
        "news_event_window.csv",
        ["provider", "before_avg_per_month", "after_avg_per_month",
         "pct_change", "before_months_n", "after_months_n"], rows)


def src_provider_attr() -> str:
    """그림: news_provider_attr → (매체 × 속성) long format: count + none제외 share."""
    providers, attrs, mat = load_attr_by_provider()
    rows = []
    for pi, prov in enumerate(providers):
        denom = sum(mat[pi][ai] for ai, a in enumerate(attrs) if a != "none")
        for ai, a in enumerate(attrs):
            cnt = mat[pi][ai]
            share = (cnt / denom * 100) if (denom and a != "none") else ""
            rows.append([prov, a, kr(a), cnt,
                         round(share, 2) if share != "" else ""])
    return _write_csv(
        "news_provider_attr.csv",
        ["provider", "attribute_en", "attribute_kr", "count",
         "share_excl_none_pct"], rows)


def src_attr_year_trend() -> str:
    """그림: news_attr_year_trend → (연도 × 속성) long format: count + none제외 share."""
    years, attrs, share, counts = load_attr_by_year()
    rows = []
    for yi, y in enumerate(years):
        for ai, a in enumerate(attrs):
            rows.append([y, a, kr(a), counts[yi][ai], round(share[yi][ai], 2)])
    return _write_csv(
        "news_attr_year_trend.csv",
        ["year", "attribute_en", "attribute_kr", "count",
         "share_excl_none_pct"], rows)


def _write_source_manifest(coverage: dict) -> str:
    """출판사 인계 README — 각 CSV ↔ 그림 ↔ 컬럼 설명."""
    dmin = coverage["date_min"].strftime("%Y-%m") if coverage["date_min"] else "?"
    dmax = coverage["date_max"].strftime("%Y-%m") if coverage["date_max"] else "?"
    L = [
        "# 그림 원자료 (출판사 인계용)\n",
        "국내 언론 AI 보도 분석 시각화의 *원 데이터*. PNG는 참고용이며, 작도는",
        "아래 CSV를 사용한다. 모든 CSV는 utf-8-sig(BOM) 인코딩.\n",
        f"- 데이터: `data/news/news_analysis.duckdb` (cleaned subset, {dmin}~{dmax})",
        f"- 기사 {coverage['total_articles']:,}건 / 분류 완료 {coverage['classified']:,}건"
        f" ({coverage['pct']:.1f}%)",
        f"- 분류 버전: {coverage['prompt_version']} / {coverage['cleaning_version']}",
    ]
    if coverage["pct"] < 99.5:
        L.append("\n> ⚠️ 재분류 진행 중 — 속성 기반 CSV는 잠정치. 분류 완료 후 재생성 요망.")
    L += [
        "\n## 파일 ↔ 그림\n",
        "| CSV | 그림 | 내용 |",
        "|---|---|---|",
        "| `news_decade_trend.csv` | 월별 보도 추이 | month, 월별 보도량, 12개월 이동평균 |",
        "| `news_event_window.csv` | AI 기본법 ±24개월 | 매체별 전/후 월평균·변화율(%) (마지막 행=전체) |",
        "| `news_provider_attr.csv` | 매체별 속성 구성 | (매체×속성) long, count + 미분류제외 share(%) |",
        "| `news_attr_year_trend.csv` | 연도별 속성 추세 | (연도×속성) long, count + 미분류제외 share(%) |",
        "\n## 공통 주석\n",
        "- `share_excl_none_pct`: 미분류(none)를 분모에서 제외한 비중(%).",
        "- 속성 라벨: `attribute_en`(Carvão 정본 영문) ↔ `attribute_kr`(보고서 한글).",
        "- `news_event_window`의 월평균은 윈도우 내 '데이터가 존재하는 달'만 평균"
        " (데이터 범위 절단 시 부분 표본).",
    ]
    path = _source_path("README.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


def export_figure_sources() -> list[str]:
    """모든 그림의 원자료 CSV + 매니페스트를 config.FIGURES_SOURCE_DIR에 생성."""
    paths = []
    for fn in (src_decade_trend, src_event_window, src_provider_attr,
               src_attr_year_trend):
        try:
            p = fn()
            paths.append(p)
            print(f"  → {p}", flush=True)
        except Exception as e:
            print(f"  [skip] {fn.__name__}: {e}", flush=True)
    try:
        p = _write_source_manifest(load_classification_coverage())
        paths.append(p)
        print(f"  → {p}", flush=True)
    except Exception as e:
        print(f"  [skip] manifest: {e}", flush=True)
    return paths


# ──────────────────────────────────────────────────────────────────────────
# 마크다운 리포트 (→ config.OUTPUT_DIR/news_analysis.md)
# ──────────────────────────────────────────────────────────────────────────

def build_report(with_figures: bool = True) -> str:
    """국내 언론 AI 보도 분석 마크다운을 output/news_analysis.md에 작성.
    with_figures=True면 figure도 함께 생성하고 본문에 상대경로로 임베드."""
    cov = load_classification_coverage()
    fig_paths = generate_figures() if with_figures else []
    fig_rel = {os.path.basename(p): os.path.relpath(p, config.OUTPUT_DIR)
               for p in fig_paths}

    L: list[str] = []
    L.append("# 국내 언론 AI 보도 분석\n")
    L.append("> 자동 생성: `python analyze/news_descriptive.py --report`. "
             "기준 데이터 `data/news/news_analysis.duckdb` (Stage 1+2 cleaned subset).\n")
    L.append("> 본문 PNG는 참고용. 출판사 인계용 그림 **원자료 CSV**는 "
             "`output/figures/source/`에 있다 (`README.md` 참조).\n")

    # 0. 데이터 커버리지
    L.append("## 0. 데이터 커버리지\n")
    dmin = cov["date_min"].strftime("%Y-%m") if cov["date_min"] else "?"
    dmax = cov["date_max"].strftime("%Y-%m") if cov["date_max"] else "?"
    L.append(f"- 분석 대상 기사: **{cov['total_articles']:,}건** ({dmin} ~ {dmax})")
    L.append(f"- 분류 완료: **{cov['classified']:,}건** "
             f"({cov['pct']:.1f}% of total)")
    L.append(f"- 분류 버전: `{cov['prompt_version']}` / cleaning `{cov['cleaning_version']}`")
    if cov["pct"] < 99.5:
        L.append(f"\n> ⚠️ **재분류 진행 중** — 현재 {cov['pct']:.1f}%만 분류 완료. "
                 "속성 기반 수치는 완료분 기준 잠정치이며, 분류 종료 후 재생성 필요.")
    L.append("")

    # 1. 보도량 추이
    L.append("## 1. 보도량 시계열\n")
    series = load_monthly_total_ma(12)
    if series:
        total = sum(c for _, c, _ in series)
        peak = max(series, key=lambda x: x[1])
        L.append(f"- 총 {total:,}건, 월 최대 {peak[1]:,}건 ({peak[0]})")
        yr = load_provider_year_matrix()
        provs, years, mat = yr
        first_y, last_y = years[0], years[-1]
        first_tot = sum(mat[pi][0] for pi in range(len(provs)))
        last_tot = sum(mat[pi][-1] for pi in range(len(provs)))
        L.append(f"- 연 보도량 {first_y}년 {first_tot:,}건 → {last_y}년 {last_tot:,}건")
    if "news_decade_trend.png" in fig_rel:
        L.append(f"\n![월별 추이]({fig_rel['news_decade_trend.png']})")
    L.append("")

    # 2. AI 기본법 윈도우
    L.append("## 2. AI 기본법 통과 전후 (±24개월)\n")
    win = load_event_window(AIBASIC_DATE, 24)
    ov = win["overall"]
    L.append(f"- 전체 월평균 {ov['before_avg']:.0f} → {ov['after_avg']:.0f}건/월 "
             f"(**{ov['pct']:+.1f}%**)")
    if win["partial"]:
        L.append("- ⚠️ 데이터 범위가 윈도우를 다 덮지 못해 부분 표본 기반 (월평균은 존재하는 달만 평균)")
    L.append("\n| 매체 | 전 평균 | 후 평균 | 변화율 |")
    L.append("|---|---:|---:|---:|")
    for p in PROVIDER_ORDER:
        if p in win["providers"]:
            d = win["providers"][p]
            L.append(f"| {p} | {d['before_avg']:.0f} | {d['after_avg']:.0f} | {d['pct']:+.1f}% |")
    if "news_event_window.png" in fig_rel:
        L.append(f"\n![이벤트 윈도우]({fig_rel['news_event_window.png']})")
    L.append("")

    # 3. 속성 구성
    L.append("## 3. 정책 속성 구성\n")
    dist = load_attr_distribution()
    if dist:
        L.append("### 3.1 전체 분포 (미분류 제외 share)\n")
        L.append("| 속성 | 건수 | 비중 |")
        L.append("|---|---:|---:|")
        for a, c, s in dist:
            sval = "—" if a == "none" else f"{s:.1f}%"
            L.append(f"| {kr(a)} | {c:,} | {sval} |")
        L.append("")
        L.append("### 3.2 매체별 구성\n")
        if "news_provider_attr.png" in fig_rel:
            L.append(f"![매체별 속성]({fig_rel['news_provider_attr.png']})\n")
        L.append("### 3.3 연도별 추세\n")
        if "news_attr_year_trend.png" in fig_rel:
            L.append(f"![연도별 추세]({fig_rel['news_attr_year_trend.png']})\n")
    else:
        L.append("> 분류 데이터 없음 (재분류 초기 상태).\n")

    # 4. BERTopic placeholder
    L.append("## 4. 소주제 분석 (BERTopic) — 준비 중\n")
    L.append("> **[PLACEHOLDER]** `analyze/subtopic_bertopic.py` 파이프라인 완성 후, "
             "속성별 소주제·분기별 Top-10 동적 랭킹을 이 절에 결합한다. "
             "`load_subtopic_stats()` 참조.\n")

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(config.OUTPUT_DIR, "news_analysis.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"  → {out_path}", flush=True)
    return out_path


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def _print_summary() -> None:
    print("== 국내 언론 AI 보도 분석 요약 (cleaned subset) ==\n", flush=True)
    cov = load_classification_coverage()
    print(f"기사 총수:   {cov['total_articles']:,}")
    print(f"분류 완료:   {cov['classified']:,} ({cov['pct']:.1f}%)  "
          f"[{cov['prompt_version']} / {cov['cleaning_version']}]")
    if cov["pct"] < 99.5:
        print("  ⚠️ 재분류 진행 중 — 속성 수치는 잠정치")
    print()

    monthly = load_monthly_counts()
    print("매체별 보도량:")
    for p in PROVIDER_ORDER:
        n = sum(c for _, c in monthly.get(p, []))
        print(f"  {p:<10} {n:>10,}")
    print()

    win = load_event_window(AIBASIC_DATE, 24)
    ov = win["overall"]
    print(f"AI 기본법 ±24개월: {ov['before_avg']:.0f} → {ov['after_avg']:.0f}/월 "
          f"({ov['pct']:+.1f}%)" + ("  [부분 윈도우]" if win["partial"] else ""))
    print()

    dist = load_attr_distribution()
    if dist:
        print("정책 속성 분포 (미분류 제외 share):")
        for a, c, s in dist:
            sval = "  (미분류)" if a == "none" else f"{s:>5.1f}%"
            print(f"  {c:>8,} {sval}  {kr(a)}")
    else:
        print("정책 속성 분포: (분류 데이터 없음)")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="국내 언론 AI 보도 종합 분석")
    ap.add_argument("--summary", action="store_true", help="콘솔 요약 출력")
    ap.add_argument("--figures", action="store_true",
                    help="output/figures/news_*.png + 원자료 CSV 생성")
    ap.add_argument("--sources", action="store_true",
                    help="출판사 인계용 원자료 CSV만 생성 (그림 없이)")
    ap.add_argument("--report", action="store_true",
                    help="output/news_analysis.md 생성 (figure + 원자료 포함)")
    ap.add_argument("--all", action="store_true", help="--figures + --report")
    args = ap.parse_args()

    did = False
    if args.summary:
        _print_summary(); did = True
    if args.report or args.all:
        # build_report → generate_figures → export_figure_sources 까지 일괄
        print("Report:"); build_report(with_figures=True); did = True
    elif args.figures:
        print("Figures:"); generate_figures(); did = True
    elif args.sources:
        print("Figure source data (출판사 인계용):")
        export_figure_sources(); did = True
    if not did:
        ap.print_help()


if __name__ == "__main__":
    main()
