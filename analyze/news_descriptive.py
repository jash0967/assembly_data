"""news.duckdb descriptive statistics — API 비용 0.

figures/regenerate_all.py가 import해서 그림을 그릴 수 있도록 public 함수만 제공.
CLI 모드(`--summary`)는 단순 콘솔 요약 출력.

Public functions (모두 @lru_cache, 반복 호출 안전):
    load_monthly_counts()          -> dict[provider, list[(yyyymm, count)]]
    load_provider_year_matrix()    -> (providers, years, matrix np.ndarray)
    load_content_length_quartiles()-> dict[provider, dict[stat, value]]
    load_category_breakdown()      -> (providers, majors, matrix)
    load_top_categories(limit=30)  -> list[(tag, count)]
    load_keyword_trend(keywords)   -> dict[keyword, list[(yyyymm, hits)]]
    load_ai_coverage_by_provider() -> dict[provider, (total, no_kw)]
    KEYWORDS                       -> dict[group, list[keyword]]
"""

from __future__ import annotations

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
import numpy as np

import config


# 매체 출력 순서 (시각화 일관성)
PROVIDER_ORDER = ["KBS", "MBC", "SBS", "YTN", "중앙일보", "한겨레"]


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


def _con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(config.NEWS_DB_PATH, read_only=True)
    # 1~3분 쿼리에서 stderr가 progress bar로 도배되는 걸 방지
    try:
        con.execute("PRAGMA disable_progress_bar")
    except Exception:
        pass
    return con


# ──────────────────────────────────────────────────────────────────────
# Strict AI 필터 — boilerplate 제거 + word boundary + 영문 본문 제외
# ──────────────────────────────────────────────────────────────────────
# - content에서 MBC boilerplate substring '(AI학습 포함)' / '(AI 학습 포함)'만
#   정확히 제거 후 매칭.
#   * 본문 실사용 'AI 학습' (띄어쓰기)·'AI학습' (붙임) 같은 진짜 표현은 보존.
#   * 본문 사용 분포: 'AI 학습' 띄어쓰기 656건 vs 'AI학습' 붙임 61건 (조사 결과).
# - 한국어: 인공지능 / 인공 지능 (ILIKE)
# - 영문: AI / A.I / A.I. (word boundary)
# - title 또는 cleaned content 어디든 매칭되면 키워드 통과
# - 추가: 본문이 영문(한글 5% 미만, 200자 이상)인 경우 한국 도메스틱 담론 대상이
#   아니므로 제외. YTN [K-SCIENCE]·[K-BIZ] 등 외신용 단신 207건 제거.
#
# 'artificial intelligence' 풀스펠 키워드는 검증 결과 영문 본문 제외 조항과
# 완전 중복이라 제거됨 (한국어 본문에 풀스펠만 단독 등장하는 케이스 0건 확인).
_CLEAN = (
    "REPLACE(REPLACE(content, '(AI학습 포함)', ''), '(AI 학습 포함)', '')"
)
# 영문 본문 판정: 한글 char가 본문의 5% 미만이고 본문이 200자 이상
_IS_ENGLISH = (
    "LENGTH(content) > 200 "
    "AND LENGTH(regexp_replace(content, '[가-힣]', '', 'g'))::FLOAT "
    "    / LENGTH(content) > 0.95"
)
STRICT_WHERE = rf"""
(
  (
   {_CLEAN} ILIKE '%인공지능%' OR title ILIKE '%인공지능%'
   OR {_CLEAN} ILIKE '%인공 지능%' OR title ILIKE '%인공 지능%'
   OR regexp_matches({_CLEAN},
                     '(?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)')
   OR regexp_matches(title,
                     '(?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)')
   OR regexp_matches({_CLEAN},
                     '(?i)(^|[^A-Za-z0-9])A\.I\.?([^A-Za-z0-9]|$)')
   OR regexp_matches(title,
                     '(?i)(^|[^A-Za-z0-9])A\.I\.?([^A-Za-z0-9]|$)')
  )
  AND NOT ({_IS_ENGLISH})
)
"""


@lru_cache(maxsize=1)
def load_strict_filter_counts() -> dict[str, dict]:
    """매체별 (총건, Strict 통과건, 통과율).

    반환: {provider: {'total': n, 'pass': k, 'rate': k/n}}.
    """
    con = _con()
    try:
        rows = con.execute(f"""
            SELECT provider, COUNT(*),
                   SUM(CASE WHEN {STRICT_WHERE} THEN 1 ELSE 0 END)
            FROM news_articles
            GROUP BY provider ORDER BY provider
        """).fetchall()
    finally:
        con.close()
    out: dict[str, dict] = {}
    for prov, total, passn in rows:
        out[prov] = {
            "total": int(total),
            "pass": int(passn),
            "rate": passn / total if total else 0.0,
        }
    return out


@lru_cache(maxsize=1)
def load_strict_monthly_counts() -> dict[str, list[tuple[str, int]]]:
    """Strict 통과 기사 월별 매체별 카운트."""
    con = _con()
    try:
        rows = con.execute(f"""
            SELECT provider, strftime(published_at, '%Y-%m') AS ym, COUNT(*) AS c
            FROM news_articles
            WHERE {STRICT_WHERE}
            GROUP BY provider, ym
            ORDER BY provider, ym
        """).fetchall()
    finally:
        con.close()
    out: dict[str, list[tuple[str, int]]] = {p: [] for p in PROVIDER_ORDER}
    for prov, ym, c in rows:
        out.setdefault(prov, []).append((ym, c))
    return out


@lru_cache(maxsize=1)
def load_strict_provider_year_matrix() -> tuple[list[str], list[int], np.ndarray]:
    """Strict 통과 매체×연도 카운트 매트릭스."""
    con = _con()
    try:
        rows = con.execute(f"""
            SELECT provider,
                   CAST(EXTRACT(YEAR FROM published_at) AS INTEGER) AS y,
                   COUNT(*) AS c
            FROM news_articles
            WHERE {STRICT_WHERE}
            GROUP BY provider, y
            ORDER BY provider, y
        """).fetchall()
    finally:
        con.close()
    providers = PROVIDER_ORDER
    years = sorted({r[1] for r in rows})
    p_idx = {p: i for i, p in enumerate(providers)}
    y_idx = {y: i for i, y in enumerate(years)}
    mat = np.zeros((len(providers), len(years)), dtype=np.int64)
    for prov, y, c in rows:
        if prov in p_idx:
            mat[p_idx[prov], y_idx[y]] = c
    return providers, years, mat


@lru_cache(maxsize=1)
def load_strict_category_breakdown() -> tuple[list[str], list[str], np.ndarray]:
    """Strict 통과 기사의 매체×대분류 카테고리 매트릭스.

    load_category_breakdown과 동일한 SQL 패턴 사용 (UNNEST는 subquery에서,
    regexp_extract는 outer SELECT에서). 그래야 두 함수의 키 인코딩이 일치.
    """
    con = _con()
    try:
        rows = con.execute(f"""
            WITH unnested AS (
                SELECT provider, UNNEST(CAST(category AS VARCHAR[])) AS tag
                FROM news_articles
                WHERE {STRICT_WHERE} AND category != '[]'::JSON
            )
            SELECT provider,
                   regexp_extract(
                       trim(BOTH '"' FROM CAST(tag AS VARCHAR)),
                       '^([^>]+)',
                       1) AS major,
                   COUNT(*) AS c
            FROM unnested
            GROUP BY provider, major
            ORDER BY provider, c DESC
        """).fetchall()
    finally:
        con.close()
    providers = PROVIDER_ORDER
    totals: dict[str, int] = {}
    for _, mj, c in rows:
        if mj:
            totals[mj] = totals.get(mj, 0) + c
    majors = [m for m, _ in sorted(totals.items(), key=lambda kv: -kv[1])]
    p_idx = {p: i for i, p in enumerate(providers)}
    m_idx = {m: i for i, m in enumerate(majors)}
    mat = np.zeros((len(providers), len(majors)), dtype=np.int64)
    for prov, mj, c in rows:
        if prov in p_idx and mj in m_idx:
            mat[p_idx[prov], m_idx[mj]] = c
    return providers, majors, mat


@lru_cache(maxsize=1)
def load_monthly_counts() -> dict[str, list[tuple[str, int]]]:
    """매체별 월별 보도량. 반환: {provider: [(yyyymm, count), ...]}."""
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
        out.setdefault(prov, []).append((ym, c))
    return out


@lru_cache(maxsize=1)
def load_provider_year_matrix() -> tuple[list[str], list[int], np.ndarray]:
    """매체×연도 카운트 매트릭스. 반환: (providers, years, matrix[len(providers), len(years)])."""
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
    mat = np.zeros((len(providers), len(years)), dtype=np.int64)
    for prov, y, c in rows:
        if prov in p_idx:
            mat[p_idx[prov], y_idx[y]] = c
    return providers, years, mat


@lru_cache(maxsize=1)
def load_content_length_quartiles() -> dict[str, dict[str, float]]:
    """매체별 본문 길이 분위수. 반환: {provider: {avg, p05, p25, p50, p75, p95, max}}."""
    con = _con()
    try:
        rows = con.execute("""
            SELECT
                provider,
                AVG(LENGTH(content))                                AS avg,
                quantile_cont(LENGTH(content), 0.05)               AS p05,
                quantile_cont(LENGTH(content), 0.25)               AS p25,
                quantile_cont(LENGTH(content), 0.50)               AS p50,
                quantile_cont(LENGTH(content), 0.75)               AS p75,
                quantile_cont(LENGTH(content), 0.95)               AS p95,
                MAX(LENGTH(content))                                AS max
            FROM news_articles
            GROUP BY provider
            ORDER BY provider
        """).fetchall()
    finally:
        con.close()
    out: dict[str, dict[str, float]] = {}
    for prov, avg, p05, p25, p50, p75, p95, mx in rows:
        out[prov] = {
            "avg": float(avg), "p05": float(p05), "p25": float(p25),
            "p50": float(p50), "p75": float(p75), "p95": float(p95),
            "max": float(mx),
        }
    return out


@lru_cache(maxsize=1)
def load_category_breakdown() -> tuple[list[str], list[str], np.ndarray]:
    """매체×대분류 카테고리 카운트. 반환: (providers, majors, matrix).

    대분류는 태그 `>` 앞부분. 빈 카테고리는 'EMPTY' 컬럼에 카운트.
    """
    con = _con()
    try:
        rows = con.execute("""
            WITH expanded AS (
                SELECT
                    provider,
                    CASE
                        WHEN category = '[]'::JSON THEN '__EMPTY__'
                        ELSE regexp_extract(
                                trim(BOTH '"' FROM CAST(tag AS VARCHAR)),
                                '^([^>]+)',
                                1)
                    END AS major
                FROM (
                    SELECT provider, category,
                           UNNEST(CAST(category AS VARCHAR[])) AS tag
                    FROM news_articles
                    WHERE category != '[]'::JSON
                    UNION ALL
                    SELECT provider, category, NULL AS tag
                    FROM news_articles
                    WHERE category = '[]'::JSON
                )
            )
            SELECT provider, major, COUNT(*)
            FROM expanded
            GROUP BY provider, major
            ORDER BY provider, COUNT(*) DESC
        """).fetchall()
    finally:
        con.close()

    providers = PROVIDER_ORDER
    majors_set: set[str] = set()
    for _, mj, _ in rows:
        if mj is not None:
            majors_set.add(mj)
    # 자주 등장하는 대분류를 앞에 두기 위해 총 카운트로 정렬
    totals: dict[str, int] = {}
    for _, mj, c in rows:
        if mj is not None:
            totals[mj] = totals.get(mj, 0) + c
    majors = [m for m, _ in sorted(totals.items(), key=lambda kv: -kv[1])]
    # __EMPTY__ 를 맨 뒤로
    if "__EMPTY__" in majors:
        majors.remove("__EMPTY__")
        majors.append("__EMPTY__")

    p_idx = {p: i for i, p in enumerate(providers)}
    m_idx = {m: i for i, m in enumerate(majors)}
    mat = np.zeros((len(providers), len(majors)), dtype=np.int64)
    for prov, mj, c in rows:
        if prov in p_idx and mj in m_idx:
            mat[p_idx[prov], m_idx[mj]] = c
    return providers, majors, mat


@lru_cache(maxsize=4)
def load_top_categories(limit: int = 30) -> list[tuple[str, int]]:
    """세부 카테고리 태그 top N."""
    con = _con()
    try:
        rows = con.execute(f"""
            SELECT trim(BOTH '"' FROM CAST(tag AS VARCHAR)) AS clean_tag,
                   COUNT(*) AS c
            FROM (
                SELECT UNNEST(CAST(category AS VARCHAR[])) AS tag
                FROM news_articles
            )
            GROUP BY clean_tag
            ORDER BY c DESC
            LIMIT {int(limit)}
        """).fetchall()
    finally:
        con.close()
    return [(t, int(c)) for t, c in rows]


def _build_keyword_case_clause(keyword: str, alias: str) -> str:
    """단일 키워드에 대한 SUM(CASE WHEN ...) 표현. ASCII 짧은 단어는 word boundary 적용."""
    # 작은따옴표만 SQL 이스케이프 (별다른 메타문자는 LIKE/regexp에서 무난)
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

    keywords: {group: [keyword, ...]} dict. None이면 모듈 상수 KEYWORDS 사용.
    반환: {keyword: [(yyyymm, hits), ...]}.
    """
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
        GROUP BY ym
        ORDER BY ym
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


@lru_cache(maxsize=1)
def load_ai_coverage_by_provider() -> dict[str, tuple[int, int]]:
    """매체별 (총 기사, AI 키워드 미포함 기사).

    "AI 키워드 포함"의 정의 (사용자 명시, 5개 패턴 OR):
        1. 인공지능       (ILIKE 부분 매칭)
        2. 인공 지능      (띄어쓰기 포함, ILIKE 부분 매칭)
        3. AI            (word boundary, 대소문자 무관) — AIDS·paid 제외
        4. A.I.          (word boundary)
        5. A.I           (word boundary, 단독으로 등장한 경우)
    title 또는 content 어느 한쪽에 있으면 "포함"으로 판정.
    """
    con = _con()
    try:
        rows = con.execute(r"""
            SELECT provider,
                   COUNT(*) AS total,
                   SUM(CASE
                       -- 1) 인공지능
                       WHEN content ILIKE '%인공지능%'
                         OR title   ILIKE '%인공지능%'
                       -- 2) 인공 지능
                         OR content ILIKE '%인공 지능%'
                         OR title   ILIKE '%인공 지능%'
                       -- 3) AI (word boundary)
                         OR regexp_matches(content, '(?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)')
                         OR regexp_matches(title,   '(?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)')
                       -- 4) A.I.
                         OR regexp_matches(content, '(?i)(^|[^A-Za-z0-9])A\.I\.([^A-Za-z0-9]|$)')
                         OR regexp_matches(title,   '(?i)(^|[^A-Za-z0-9])A\.I\.([^A-Za-z0-9]|$)')
                       -- 5) A.I  (마지막 점 없음)
                         OR regexp_matches(content, '(?i)(^|[^A-Za-z0-9])A\.I([^A-Za-z0-9]|$)')
                         OR regexp_matches(title,   '(?i)(^|[^A-Za-z0-9])A\.I([^A-Za-z0-9]|$)')
                       THEN 0 ELSE 1 END) AS no_kw
            FROM news_articles
            GROUP BY provider
            ORDER BY provider
        """).fetchall()
    finally:
        con.close()
    return {prov: (int(total), int(no_kw)) for prov, total, no_kw in rows}


def _print_summary() -> None:
    print("== news.duckdb descriptive summary ==\n", flush=True)

    monthly = load_monthly_counts()
    total = sum(c for v in monthly.values() for _, c in v)
    print(f"Total rows:            {total:,}")
    print(f"Providers:             {len(monthly)}")
    for p in PROVIDER_ORDER:
        n = sum(c for _, c in monthly.get(p, []))
        print(f"  {p:<10} {n:>10,}")
    print()

    providers, years, mat = load_provider_year_matrix()
    print(f"Year range: {years[0]} ~ {years[-1]}")
    print()

    print("Content length quartiles by provider:")
    qs = load_content_length_quartiles()
    print(f"  {'provider':<10} {'avg':>8} {'p25':>8} {'p50':>8} {'p75':>8} {'p95':>8} {'max':>8}")
    for p in PROVIDER_ORDER:
        if p in qs:
            q = qs[p]
            print(f"  {p:<10} {q['avg']:>8.0f} {q['p25']:>8.0f} {q['p50']:>8.0f} "
                  f"{q['p75']:>8.0f} {q['p95']:>8.0f} {q['max']:>8.0f}")
    print()

    print("Top 10 detailed categories:")
    for tag, c in load_top_categories(limit=10):
        print(f"  {c:>8,}  {tag}")
    print()

    print("AI keyword coverage by provider:")
    cov = load_ai_coverage_by_provider()
    print(f"  {'provider':<10} {'total':>10} {'no_kw':>10} {'pct':>6}")
    for p in PROVIDER_ORDER:
        if p in cov:
            tot, no_kw = cov[p]
            pct = no_kw / tot * 100 if tot else 0
            print(f"  {p:<10} {tot:>10,} {no_kw:>10,} {pct:>5.1f}%")


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true", help="quick console summary")
    args = ap.parse_args()
    if args.summary:
        _print_summary()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
