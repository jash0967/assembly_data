"""한국 도메스틱 뉴스 정화 파이프라인 — 정의 + 빌드.

상세 설계·근거·영향 측정은 같은 폴더의 [news_cleaning.md](news_cleaning.md) 참조.

## 두 단계 파이프라인

### Stage 1 — Boilerplate Removal (본문 정화, 행 보존)
모든 기사(157,886)를 대상으로 본문에 끼어든 비-기사 텍스트 중 AI 키워드를
포함한 부분을 제거. 실제 AI 관련 기사도 이 단계의 대상이다.

- **Rule B1** — YTN footer 라인 제거 (`AI 앵커ㅣY-GO`, `유일하게 AI가 대체 못 하는 직업?` 포함 줄 통째)
- **Rule B2** — MBC 저작권 substring 제거 (`(AI학습 포함)`, `(AI 학습 포함)`)

### Stage 2 — AI Relevance Filter (행 단위 통과/탈락)
Stage 1 결과(정화된 본문) 위에서 AI 관련 기사만 추림. 결과 ≒ 81,121건.

- **Rule R1** — AI 키워드 매칭 (sanitized content + raw title)
- **Rule R2** — 영문 본문 제외 (한글 < 5%, raw content)
- **Rule R3** — 조류인플루엔자 약자 충돌 제외 (raw content/title)
- **Rule R4** — 사이버대학 광고 제외 (raw content/title)
- **Rule R5** — 일반대학 모집 광고 footer 3중 조합 제외 (raw content)
- **Rule R6** — KBS `[사진기사]` placeholder stub 제외 (raw content, 33건)

### Stage 3 — Deduplication (행 collapse, 동일 본문 1행만 유지)
Stage 2 통과 행 중 같은 매체에서 같은 본문(공백 정규화)이 여러 번 ingest된 경우 1행만 유지.

- **Rule D1** — `(provider, MD5(content_no_ws))` 그룹별 keep 1행. 우선순위:
  ① `published_at ASC NULLS LAST` (가장 이른 원본),
  ② `byline IS NULL` (byline 있는 행),
  ③ `provider_link_page IS NULL` (원본 URL 있는 행),
  ④ `news_id` (결정적 동률).

## CLI — 빌드 entry point

    python analyze/news_cleaning.py              # 일반 빌드 (articles 재빌드)
    python analyze/news_cleaning.py --dry-run    # SQL만 출력
    python analyze/news_cleaning.py --stage1-only # Stage 1 영향만 측정

## 워크플로우

이 모듈은 **raw → cleaned articles + cleaning_runs 메타**만 책임진다.
분류(`news_classifications`)는 별도 `analyze/classify_news_kr*.py`가 cleaned DB에
직접 쓴다 — raw DB에는 classification 정보 없음(설계 의도).

빌드가 cleaned DB에 이미 있는 `news_classifications`를 발견하면 dedup·룰 강화로
발생한 orphan만 자동 정리한다. 분류 자체는 생성하거나 마이그레이션하지 않는다.

소비 스크립트(classify_news_kr.py, news_descriptive.py, subtopic_bertopic.py)는
analysis DB의 `content` 컬럼(이미 Stage 1 적용)을 직접 SELECT하므로
이 모듈을 import하지 않는다.

## Public API

- `SANITIZE_CONTENT_SQL(content_expr)` — Stage 1 정화 SQL 식 반환
- `RELEVANCE_WHERE(*, title_expr, sanitized_content_expr, raw_content_expr)` — Stage 2 WHERE 절 반환
- `RULES_APPLIED` — 현재 활성 룰 라벨 리스트 (메타 기록용)
"""

from __future__ import annotations

# Build IO·CLI에만 필요한 imports — 룰 함수 자체는 표준 라이브러리도 안 씀.
# `import config` 위해 _bootstrap이 sys.path를 손봐줘야 한다.
import _bootstrap  # noqa: F401

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

import config

# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 — Boilerplate Removal (본문 정화)
# ──────────────────────────────────────────────────────────────────────────────
#
# Rule B1 — YTN footer 라인 제거 (2026-05-22 도입)
#   YTN 라디오/자막뉴스(Y-GO·Y-ON 시리즈) 본문 끝에 자동 첨부되는 footer:
#     "AI 앵커ㅣY-GO" / "오디오ㅣAI 앵커" / "AI앵커 : Y-GO" 등 약 9,000+건
#   이벤트 광고 텍스트:
#     "YTN AI 앵커 이름 맞히고 AI 스피커 받자!"
#   단발성 관련기사 추천 링크 (2025-09~10 두 달):
#     "유일하게 AI가 대체 못 하는 직업?"  (230건)
#   → 본문에서 위 패턴을 포함한 줄 전체 제거.
#
# Rule B2 — MBC 저작권 substring 제거 (2026-05-21 도입)
#   MBC가 2025년부터 모든 기사 footer에 부착한 "(AI학습 포함) 금지" boilerplate.
#   본문 진짜 표현 'AI 학습'(656건) · 'AI학습'(61건)은 보존하기 위해
#   `(AI학습 포함)` / `(AI 학습 포함)` substring만 정확히 제거.
# ──────────────────────────────────────────────────────────────────────────────

_YTN_FOOTER_RE = "(AI ?앵커|AI가 대체 못 하는)"


def SANITIZE_CONTENT_SQL(content_expr: str) -> str:
    """Stage 1 정화 SQL 식 반환. Rule B1·B2 적용.

    Args:
        content_expr: SQL 컬럼명 또는 식 (예: ``"content"``, ``"raw.content"``).
    Returns:
        정화된 content를 반환하는 DuckDB SQL 식 문자열.

    Examples:
        >>> SANITIZE_CONTENT_SQL("content")[:60]
        "REPLACE(REPLACE(regexp_replace(content, '[^\\\\n]*(AI ?앵커|AI"
    """
    return (
        "REPLACE(REPLACE("
        f"regexp_replace({content_expr}, '[^\\n]*{_YTN_FOOTER_RE}[^\\n]*', '', 'g'),"
        " '(AI학습 포함)', ''), '(AI 학습 포함)', '')"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Stage 2 — AI Relevance Filter (행 단위 통과/탈락)
# ──────────────────────────────────────────────────────────────────────────────
#
# Rule R1 — 키워드 매칭 (OR) — Korean ILIKE + English word boundary
#   title은 raw 그대로 매칭. content는 Stage 1 정화본 기준.
#   - 한국어: '인공지능' / '인공 지능' substring
#   - 영문 AI: word boundary (?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)
#     * 한글 조사('AI가','AI는','AI를')는 자연스럽게 잡힘 (한글 = non-ASCII).
#   - 영문 A.I / A.I.: word boundary (?i)(^|[^A-Za-z0-9])A\.I\.?([^A-Za-z0-9]|$)
#   'artificial intelligence' 풀스펠은 Rule R2(영문 본문 제외)와 완전 중복이라
#   제거됨 (한국어 본문에 풀스펠만 단독 등장하는 케이스 0건 확인, 2026-05-22).
#
# Rule R2 — 영문 본문 제외 (2026-05-22 도입)
#   YTN [K-SCIENCE]·[K-BIZ] 등 외신용 영문 단신 207건 등 한국어 외 본문은
#   본 분석 대상에서 제외. 한글 char 비율 5% 미만 + 200자 이상이면 영문 판정.
#   raw content 기준 (Stage 1 결과와 무관).
#
# Rule R3 — 조류인플루엔자(Avian Influenza) 약자 충돌 제외 (2026-05-22 도입)
#   농림축산식품부 보도자료에서 'AI'를 조류인플루엔자 약자로 사용
#   ('AI 확진', '고병원성 AI', 'AI 양성 반응' 등) → word boundary 정규식 통과.
#   검증: 조류 + 인공지능 부재 = 3,287 false positive,
#         조류 + 인공지능 동시 (보존 대상) = 24건뿐
#   → 조류인플루엔자 키워드 있고 한국어 '인공지능'/'인공 지능' 부재면 제외.
#   raw content/title 기준.
#
# Rule R4 — 사이버대학 광고 통째 제외 (2026-05-24 도입)
#   사이버대학교(주로 중앙일보 보도자료, 일부 한겨레·YTN) 신·편입생 모집 광고가
#   본문에 'AI공학과·인공지능학과·AI융합' 학과명을 포함해 통과:
#   사이버대학 명칭 포함 = 825건 raw, Strict 통과 749건 (90.8%).
#   중앙일보 703건 (93.8%) 집중. sample 검토 결과 진짜 AI 정책 분석 대상 5건 미만.
#   명칭 substring 하나로 사이버대학교·사이버대학원 등 변형 모두 포괄.
#
# Rule R5 — 일반 대학 AI 학과 모집 광고 제외 (footer 3중 조합, 2026-05-24 도입)
#   사이버대학 제외 후 일반 대학 "모집+AI학과명" 동시 등장 = 4,647건이나
#   대부분은 정책 보도·칼럼·정부 보도자료. 단순 키워드 조합으로 제외하면
#   collateral 과도. → 광고 footer 3중 조합으로 정확 매칭:
#     ① (문의 OR 입학처)
#     ② (모집요강 OR 원서접수 OR 접수마감)
#     ③ (장학금 OR 등록금)
#   세 그룹 모두 충족 시 광고로 확정 → 557건 제외 (collateral 5% 미만 추정).
#
# Rule R6 — KBS '[사진기사]' placeholder stub 제외 (2026-05-30 도입)
#   본문이 정확히 '[사진기사]' 한 줄(공백 정규화 후)인 행 = KBS 33건 (2020-10 ~
#   2021-06 8개월 집중). KBS 내부 메타라벨로 추정 — "사진 위주 보도라 본문 텍스트
#   없음" 표시. title에 AI 키워드 있어 R1·R2·R3 통과하나 본문 정보량 0.
#   분류 결과 분포 검증:
#     - none 19건 (57.6%) — title만으로 분류 못 함
#     - Public interest 10건 — 대부분 조류인플루엔자 (R3가 못 잡음, 본문 6자)
#     - Industrial policy 2건, National security 2건
#   raw content_no_ws 정확 매치 1개 (false-positive 0건).
# ──────────────────────────────────────────────────────────────────────────────


def RELEVANCE_WHERE(
    *,
    title_expr: str = "title",
    sanitized_content_expr: str,
    raw_content_expr: str = "content",
) -> str:
    """Stage 2 WHERE 절 반환. Rule R1~R5 적용.

    Args:
        title_expr: title 컬럼 식 (raw 기준 — Stage 1을 거치지 않음).
        sanitized_content_expr: Stage 1 적용된 content 식 — Rule R1의 키워드
            매칭에 사용 (boilerplate 재진입 방지).
        raw_content_expr: raw content 식 — Rule R2 영문 판정,
            Rule R3 조류 약자 충돌, Rule R4·R5 광고 매칭,
            Rule R6 사진기사 placeholder 매칭에 사용.

    Returns:
        괄호로 감싼 SQL WHERE 절 문자열. ``WHERE`` 키워드는 포함하지 않으므로
        호출 측이 ``WHERE`` 접두를 붙여야 한다.
    """
    t = title_expr
    s = sanitized_content_expr
    r = raw_content_expr

    # Rule R1 — 키워드 매칭 (sanitized content + raw title)
    keyword_match = (
        f"("
        f" {s} ILIKE '%인공지능%' OR {t} ILIKE '%인공지능%'"
        f" OR {s} ILIKE '%인공 지능%' OR {t} ILIKE '%인공 지능%'"
        f" OR regexp_matches({s}, '(?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)')"
        f" OR regexp_matches({t}, '(?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)')"
        f" OR regexp_matches({s}, '(?i)(^|[^A-Za-z0-9])A\\.I\\.?([^A-Za-z0-9]|$)')"
        f" OR regexp_matches({t}, '(?i)(^|[^A-Za-z0-9])A\\.I\\.?([^A-Za-z0-9]|$)')"
        f")"
    )

    # Rule R2 — 영문 본문 제외 (raw 기준)
    is_english = (
        f"LENGTH({r}) > 200 "
        f"AND LENGTH(regexp_replace({r}, '[가-힣]', '', 'g'))::FLOAT "
        f"    / LENGTH({r}) > 0.95"
    )

    # Rule R3 — 조류인플루엔자 약자 충돌 (raw 기준)
    is_bird_flu_fp = (
        f"({r} ILIKE '%조류인플루엔자%' OR {r} ILIKE '%조류 인플루엔자%' "
        f" OR {t} ILIKE '%조류인플루엔자%' OR {t} ILIKE '%조류 인플루엔자%') "
        f"AND {r} NOT ILIKE '%인공지능%' AND {t} NOT ILIKE '%인공지능%' "
        f"AND {r} NOT ILIKE '%인공 지능%' AND {t} NOT ILIKE '%인공 지능%'"
    )

    # Rule R4 — 사이버대학 광고 (raw 기준)
    is_cyber_univ_ad = f"{r} ILIKE '%사이버대학%' OR {t} ILIKE '%사이버대학%'"

    # Rule R5 — 일반대학 모집 광고 3중 조합 (raw 기준)
    is_univ_recruit_ad = (
        f"({r} ILIKE '%문의%' OR {r} ILIKE '%입학처%') "
        f"AND ({r} ILIKE '%모집요강%' OR {r} ILIKE '%원서접수%' "
        f"     OR {r} ILIKE '%접수마감%') "
        f"AND ({r} ILIKE '%장학금%' OR {r} ILIKE '%등록금%')"
    )

    # Rule R6 — KBS '[사진기사]' placeholder stub 제외 (raw 기준)
    # 본문이 정확히 '[사진기사]' 한 줄(공백 정규화 후)인 행. 33건 모두 KBS.
    # 본문 텍스트가 없어 GPT가 title만 보고 추정 — none 57.6%, 잘못된 분류 위험.
    is_photo_only_stub = f"REGEXP_REPLACE({r}, '\\s+', '', 'g') = '[사진기사]'"

    return (
        f"({keyword_match} "
        f"AND NOT ({is_english}) "
        f"AND NOT ({is_bird_flu_fp}) "
        f"AND NOT ({is_cyber_univ_ad}) "
        f"AND NOT ({is_univ_recruit_ad}) "
        f"AND NOT ({is_photo_only_stub}))"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Stage 3 — Deduplication (행 collapse, 동일 본문 1행만 유지)
# ──────────────────────────────────────────────────────────────────────────────
#
# Rule D1 — (provider, MD5(content_no_ws)) 그룹별 collapse (2026-05-30 도입)
#   Stage 2 통과 행 중 같은 매체·같은 본문(공백 정규화)이 여러 번 ingest된 경우
#   1행만 유지. KBS·YTN 24h 채널에서 같은 보도가 시각별로 재방송되어 ingest된
#   case가 대부분. R6 적용 후 false-positive 0건 확인 (위험 그룹은 모두 표기
#   변형의 진짜 중복).
#   keep 우선순위: published_at ASC NULLS LAST → byline NOT NULL →
#                  provider_link_page NOT NULL → news_id ASC (결정적).
#   삭제 약 4,400행 예상 (KBS ~2,700, YTN ~1,000, 기타 ~100).
# ──────────────────────────────────────────────────────────────────────────────


def DEDUP_DELETE_SQL(table_name: str = "news_articles") -> str:
    """Stage 3 dedup DELETE 문 반환. 빌드 SQL 안에서 Stage 2 CREATE 후 실행.

    Args:
        table_name: collapse 대상 테이블 (Stage 2 적용본).
    Returns:
        DuckDB DELETE 문 — 그룹마다 rn>1 행을 삭제.
    """
    return f"""
        WITH ranked AS (
          SELECT news_id,
                 ROW_NUMBER() OVER (
                   PARTITION BY provider, MD5(REGEXP_REPLACE(content, '\\s+', '', 'g'))
                   ORDER BY published_at ASC NULLS LAST,
                            (byline IS NULL),
                            (provider_link_page IS NULL),
                            news_id
                 ) AS rn
          FROM {table_name}
        )
        DELETE FROM {table_name}
        WHERE news_id IN (SELECT news_id FROM ranked WHERE rn > 1)
    """


# ──────────────────────────────────────────────────────────────────────────────
# 메타 — build script가 news_cleaning_runs 테이블에 기록
# ──────────────────────────────────────────────────────────────────────────────

RULES_APPLIED = ["B1", "B2", "R1", "R2", "R3", "R4", "R5", "R6", "D1"]


__all__ = [
    "SANITIZE_CONTENT_SQL",
    "RELEVANCE_WHERE",
    "DEDUP_DELETE_SQL",
    "RULES_APPLIED",
]


# ══════════════════════════════════════════════════════════════════════════════
# Build IO — raw `news.duckdb` → `news_analysis.duckdb`
# ══════════════════════════════════════════════════════════════════════════════
#
# 합성 흐름: Stage 1+2+3 적용.
#   1. 사전점검: raw DB가 다른 프로세스에 의해 잠겨있는지 확인
#   2. 자동 백업: 기존 analysis DB를 .bak로 복사
#   3. 트랜잭션 시작
#   4. raw DB ATTACH read-only
#   5. news_articles DROP + (SANITIZE 식 + RELEVANCE WHERE)로 CREATE (Stage 1+2)
#   6. Stage 3 D1 dedup (DELETE)
#   7. news_cleaning_runs 메타 INSERT
#   8. classifications가 있으면 orphan 정리 — classify가 별도로 만든 분류
#   9. 검증 (cleaned 행 수·매체별 카운트)
#  10. COMMIT (또는 ROLLBACK + 백업 복원)
#
# 검증 실패는 hard fail (rollback + 복원), 편차 경고는 soft (warning만 출력).
# ══════════════════════════════════════════════════════════════════════════════


# news_cleaning.md §4 — 매체별 기대값 (현재 룰 기준, 2026-05-28).
# 빌드 결과와 ±1% 이내가 정상. 룰 변경으로 의도적 변동은 warning만 출력.
EXPECTED_TOTAL = 76_645  # Stage 3 D1 적용 후 (이전 81,088)
EXPECTED_BY_PROVIDER = {
    "중앙일보": 32_141,  # D1 -95
    "YTN": 16_767,  # D1 -1,129 (24h 채널 재방송)
    "한겨레": 11_580,  # D1 -10
    "KBS": 10_863,  # D1 -3,181 (지역방송 재방송)
    "MBC": 2_756,  # D1 -27
    "SBS": 2_538,  # D1 -1
}

# ─── 헬퍼 ─────────────────────────────────────────────────────────────────────


def _git_short_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent.parent,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _md5_short(s: str, n: int = 8) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:n]


def _build_cleaning_version(
    sanitize_sql: str, relevance_where: str
) -> tuple[str, str, str]:
    """Return (version, sanitize_hash, relevance_hash).

    version은 sanitize + relevance + RULES_APPLIED(Stage 3 D1 포함) hash 조합으로
    생성 — 룰 셋이 바뀌면 version 자동 갱신.
    """
    sanitize_hash = _md5_short(sanitize_sql)
    relevance_hash = _md5_short(relevance_where)
    rules_hash = _md5_short(",".join(RULES_APPLIED))
    date = datetime.now().strftime("%Y-%m-%d")
    version = f"{date}_{sanitize_hash}_{relevance_hash}_{rules_hash}"
    return version, sanitize_hash, relevance_hash


def _check_raw_not_locked() -> None:
    """raw DB가 다른 프로세스에 의해 write-locked인지 확인."""
    try:
        probe = duckdb.connect(config.NEWS_RAW_DB_PATH, read_only=False)
        probe.close()
    except Exception as e:
        msg = str(e).lower()
        if "lock" in msg or "concurrent" in msg or "in use" in msg:
            raise SystemExit(
                f"raw DB가 다른 프로세스에 의해 쓰기 잠금됨:\n  {config.NEWS_RAW_DB_PATH}\n"
                f"collect/build_news_db.py 등 raw writer가 실행 중인지 확인 후 다시 실행."
            )
        raise


def _backup_analysis_db() -> Path | None:
    """기존 analysis DB가 있으면 epoch.bak으로 백업. 신규 빌드면 None."""
    src = Path(config.NEWS_ANALYSIS_DB_PATH)
    if not src.exists():
        return None
    bak = src.with_suffix(f".duckdb.{int(time.time())}.bak")
    shutil.copy2(src, bak)
    print(f"[backup] {src.name} → {bak.name}")
    return bak


def _restore_from_backup(bak: Path | None) -> None:
    if bak is None or not bak.exists():
        return
    dst = Path(config.NEWS_ANALYSIS_DB_PATH)
    shutil.copy2(bak, dst)
    print(f"[restore] {bak.name} → {dst.name}", file=sys.stderr)


# ─── DDL ──────────────────────────────────────────────────────────────────────

NEWS_CLEANING_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS news_cleaning_runs (
    cleaning_version     VARCHAR PRIMARY KEY,
    built_at             TIMESTAMP NOT NULL,
    rules_applied        VARCHAR[] NOT NULL,
    raw_row_count        INTEGER NOT NULL,
    cleaned_row_count    INTEGER NOT NULL,
    sanitize_sql_hash    VARCHAR NOT NULL,
    relevance_where_hash VARCHAR NOT NULL,
    code_commit_sha      VARCHAR,
    notes                VARCHAR
)
"""

INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_news_provider     ON news_articles(provider)",
    "CREATE INDEX IF NOT EXISTS idx_news_published    ON news_articles(published_at)",
    "CREATE INDEX IF NOT EXISTS idx_news_provider_pub ON news_articles(provider, published_at)",
]

COMMENT_CONTENT_SQL = (
    "COMMENT ON COLUMN news_articles.content IS "
    "'Stage 1 sanitized — YTN footer line removed (Rule B1), MBC (AI학습 포함) "
    "substring removed (Rule B2). For raw content see news_raw.news_articles.content.'"
)


# ─── stage1-only ──────────────────────────────────────────────────────────────


def stage1_only_measure() -> None:
    """Stage 1 영향 측정만 — DB 변경 없음."""
    print(f"[stage1-only] raw = {config.NEWS_RAW_DB_PATH}")
    con = duckdb.connect(config.NEWS_RAW_DB_PATH, read_only=True)
    con.execute("PRAGMA disable_progress_bar")

    sanitized = SANITIZE_CONTENT_SQL("content")
    print()
    print("=== 매체별 본문 변경 행 수 (Stage 1) ===")
    rows = con.execute(f"""
        SELECT provider,
               COUNT(*) AS total,
               SUM(CASE WHEN content <> ({sanitized}) THEN 1 ELSE 0 END) AS changed
        FROM news_articles
        GROUP BY provider
        ORDER BY changed DESC
    """).fetchall()
    print(f"{'provider':<10} {'total':>10} {'changed':>10}  {'%':>5}")
    print("-" * 42)
    for prov, total, changed in rows:
        pct = 100 * changed / total if total else 0
        print(f"{prov:<10} {total:>10,} {changed:>10,}  {pct:>5.1f}")
    con.close()


# ─── dry-run ──────────────────────────────────────────────────────────────────


def dry_run() -> None:
    sanitized = SANITIZE_CONTENT_SQL("content")
    where = RELEVANCE_WHERE(
        title_expr="title",
        sanitized_content_expr=sanitized,
        raw_content_expr="content",
    )
    version, sh, rh = _build_cleaning_version(sanitized, where)
    print(f"[dry-run] cleaning_version = {version}")
    print(f"[dry-run] sanitize_hash    = {sh}")
    print(f"[dry-run] relevance_hash   = {rh}")
    print()
    print("=== CREATE TABLE news_articles AS ===")
    print(f"""SELECT news_id, title,
       ({sanitized}) AS content,
       dateline, published_at, enveloped_at, provider, byline,
       provider_link_page, category, category_incident, hilight,
       printing_page, ingested_at
FROM raw.news_articles
WHERE {where}""")
    print()
    print("=== news_cleaning_runs INSERT ===")
    print(f"  version={version}")
    print(f"  rules_applied={RULES_APPLIED}")


# ─── main build ───────────────────────────────────────────────────────────────


def build() -> str:
    """빌드 후 이번에 쓰인 cleaning_version 을 돌려준다 (감사 로그 note 용)."""
    _check_raw_not_locked()

    sanitized = SANITIZE_CONTENT_SQL("content")
    where = RELEVANCE_WHERE(
        title_expr="title",
        sanitized_content_expr=sanitized,
        raw_content_expr="content",
    )
    version, sanitize_hash, relevance_hash = _build_cleaning_version(sanitized, where)
    commit_sha = _git_short_sha()
    print(f"cleaning_version: {version}")
    print(f"raw DB:           {config.NEWS_RAW_DB_PATH}")
    print(f"analysis DB:      {config.NEWS_ANALYSIS_DB_PATH}")
    print(f"git commit:       {commit_sha or '(no git)'}")
    print()

    bak = _backup_analysis_db()

    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH)
    con.execute("PRAGMA disable_progress_bar")
    con.execute(f"ATTACH '{config.NEWS_RAW_DB_PATH}' AS raw (READ_ONLY)")

    t0 = time.time()

    try:
        con.execute("BEGIN TRANSACTION")

        # 0) cleaning_runs 메타 테이블 (없으면 생성)
        con.execute(NEWS_CLEANING_RUNS_DDL)

        # 1) news_articles: 항상 DROP + CREATE (Stage 1 + Stage 2 합성)
        print("[1/5] news_articles DROP + CREATE (Stage 1+2) ...")
        con.execute("DROP TABLE IF EXISTS news_articles")
        con.execute(f"""
            CREATE TABLE news_articles AS
            SELECT news_id, title,
                   ({sanitized}) AS content,
                   dateline, published_at, enveloped_at, provider, byline,
                   provider_link_page, category, category_incident, hilight,
                   printing_page, ingested_at
            FROM raw.news_articles
            WHERE {where}
        """)
        for idx_sql in INDEXES_SQL:
            con.execute(idx_sql)
        stage2_row_count = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[
            0
        ]

        # 2) Stage 3 dedup (Rule D1): (provider, content_no_ws) 그룹별 1행 유지
        print("[2/5] Stage 3 dedup (D1) ...")
        con.execute(DEDUP_DELETE_SQL("news_articles"))
        cleaned_row_count = con.execute(
            "SELECT COUNT(*) FROM news_articles"
        ).fetchone()[0]
        dedup_dropped = stage2_row_count - cleaned_row_count
        print(
            f"        Stage 2 통과 {stage2_row_count:,} → D1 collapse {cleaned_row_count:,} (drop {dedup_dropped:,})"
        )

        # 3) news_cleaning_runs에 현재 빌드 메타 INSERT
        print("[3/5] news_cleaning_runs INSERT ...")
        raw_row_count = con.execute(
            "SELECT COUNT(*) FROM raw.news_articles"
        ).fetchone()[0]
        con.execute(
            "INSERT OR REPLACE INTO news_cleaning_runs VALUES "
            "(?, CURRENT_TIMESTAMP, ?::VARCHAR[], ?, ?, ?, ?, ?, ?)",
            [
                version,
                RULES_APPLIED,
                raw_row_count,
                cleaned_row_count,
                sanitize_hash,
                relevance_hash,
                commit_sha,
                f"build at {datetime.now().isoformat(timespec='seconds')} (Stage 2 pass={stage2_row_count}, D1 drop={dedup_dropped})",
            ],
        )

        # 4) Classifications가 이미 있으면 (이전 classify 실행) dedup·룰 강화로
        #    잘려나간 행의 orphan 분류를 자동 정리. 없으면 (첫 빌드 또는 raw 초기화)
        #    이 단계 skip — classify 스크립트가 다음 실행 시 채워넣을 것.
        db_name = con.execute("SELECT current_database()").fetchone()[0]
        cls_exists = (
            con.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_catalog=? AND table_name='news_classifications'",
                [db_name],
            ).fetchone()[0]
            > 0
        )
        if cls_exists:
            print("[4/5] orphan classifications 정리 + 검증 ...")
            cls_before = con.execute("SELECT COUNT(*) FROM news_classifications").fetchone()[0]  # type: ignore[index]
            con.execute("""
                DELETE FROM news_classifications
                WHERE news_id NOT IN (SELECT news_id FROM news_articles)
            """)
            post_cls_count: int = con.execute("SELECT COUNT(*) FROM news_classifications").fetchone()[0]  # type: ignore[index]
            print(
                f"     news_classifications: {cls_before:,} → {post_cls_count:,} (orphan {cls_before-post_cls_count:,} 정리)"
            )
        else:
            print("[4/5] news_classifications 부재 — classify 스크립트가 빌드 후 생성")

        # classifications 있는 경우만 orphan 재검증
        if cls_exists:
            orphan: int = con.execute("""
                SELECT COUNT(*) FROM news_classifications c
                LEFT JOIN news_articles a ON a.news_id = c.news_id
                WHERE a.news_id IS NULL
            """).fetchone()[0]  # type: ignore[index]
            print(f"     orphan 재검증: {orphan} (기대 0)")
            if orphan > 0:
                raise RuntimeError(f"orphan classifications {orphan}건 잔존 — 빌드 실패")

        # 5) 검증
        print("[5/5] 검증 ...")
        deviation_pct = 100 * abs(cleaned_row_count - EXPECTED_TOTAL) / EXPECTED_TOTAL
        print(
            f"     cleaned row count: {cleaned_row_count:,} (기대 {EXPECTED_TOTAL:,}, 편차 {deviation_pct:.2f}%)"
        )
        if deviation_pct > 1.0:
            print(f"     ⚠  편차 > 1% — 룰 변경 의도면 정상. commit_sha={commit_sha}")

        provider_rows = con.execute(
            "SELECT provider, COUNT(*) FROM news_articles GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        print(f"     provider 분포:")
        for prov, cnt in provider_rows:
            expected = EXPECTED_BY_PROVIDER.get(prov, 0)
            dev = 100 * abs(cnt - expected) / expected if expected else 0
            flag = "  ⚠" if dev > 1.0 else ""
            print(
                f"       {prov:<10} {cnt:>7,}  (기대 {expected:>7,}, 편차 {dev:>4.1f}%){flag}"
            )

        stage1_changed = con.execute("""
            SELECT COUNT(*)
            FROM news_articles a
            JOIN raw.news_articles r ON r.news_id = a.news_id
            WHERE a.content <> r.content
        """).fetchone()[0]
        print(f"     Stage 1 본문 변경 행: {stage1_changed:,} / {cleaned_row_count:,}")

        con.execute("COMMIT")
        print()
        print(f"COMMIT 완료 (elapsed {time.time()-t0:.1f}s)")

        # COMMENT ON COLUMN (transaction 밖)
        con.execute(COMMENT_CONTENT_SQL)

        print()
        print(f"백업 보존: {bak.name if bak else '(첫 빌드, 백업 없음)'}")
        if bak:
            print(f"  검증 후 삭제하려면: rm {bak}")

    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        con.close()
        _restore_from_backup(bak)
        raise

    con.close()
    return version


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(
        description="한국 도메스틱 뉴스 정화 파이프라인 — raw → news_analysis.duckdb 빌드"
    )
    ap.add_argument("--dry-run", action="store_true", help="SQL만 출력, DB 변경 없음")
    ap.add_argument(
        "--stage1-only",
        action="store_true",
        help="Stage 1 영향 측정만 (raw vs sanitized diff), DB 변경 없음",
    )
    args = ap.parse_args()

    if args.stage1_only:
        stage1_only_measure()
    elif args.dry_run:
        dry_run()
    else:
        # DB를 바꾸는 경로만 감사한다 (--dry-run/--stage1-only 는 read-only).
        # 감사 로그는 news_analysis.duckdb **밖**(data/_audit/)에 쌓이므로
        # 실패 시 _restore_from_backup() 이 파일을 되돌려도 이력이 남는다.
        import db_audit
        with db_audit.audit_run(__file__, config.NEWS_ANALYSIS_DB_PATH,
                                argv=sys.argv[1:]) as _run:
            _run.mark_rebuilt("news_articles",
                              "Stage 1+2 CTAS 재빌드 (DROP + CREATE TABLE AS)")
            version = build()
            _run.note(f"cleaning_version={version}")


if __name__ == "__main__":
    main()
