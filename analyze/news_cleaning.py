"""한국 도메스틱 뉴스 데이터 클리닝 — Strict AI 필터 정의.

상세 설계·근거·영향 측정은 같은 폴더의 [news_cleaning.md](news_cleaning.md)를 참조.

이 모듈은 SQL 조각만 export. 실제 적용은 호출 측에서:
    from news_cleaning import STRICT_WHERE
    con.execute(f"SELECT ... FROM news_articles WHERE {STRICT_WHERE}")

Public exports:
    STRICT_WHERE         최종 SQL WHERE 절 (모든 규칙 결합)
    CLEANED_CONTENT_SQL  본문 전처리 후 SQL 식 (alias `cleaned_content`로 SELECT 시 활용 가능)
    YTN_FOOTER_RE        YTN footer 매칭 regex (참고용 export)

Internal building blocks (테스트·디버깅 시 import 가능):
    _YTN_FOOTER_RE   _CLEAN   _IS_ENGLISH   _IS_BIRD_FLU_FP
    _IS_CYBER_UNIV_AD   _IS_UNIV_RECRUIT_AD

규칙 번호는 흐름 순서와 일치:
    Rule 1·2 — 전처리 (cleaning)
    Rule 3   — 키워드 매칭 (포함 조건)
    Rule 4·5·6·7 — 제외 조건 (AND NOT)
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────
# Rule 1 — YTN AI 앵커 footer · 관련기사 promo 라인 제거 (2026-05-22)
# ──────────────────────────────────────────────────────────────────────
# YTN 라디오/자막뉴스(Y-GO·Y-ON 시리즈) 본문 끝에 자동 첨부되는 footer:
#   "AI 앵커ㅣY-GO" / "오디오ㅣAI 앵커" / "AI앵커 : Y-GO" 등 약 9,000+건
# 이벤트 광고 텍스트:
#   "YTN AI 앵커 이름 맞히고 AI 스피커 받자!"
# 단발성 관련기사 추천 링크 (2025-09~10 두 달):
#   "유일하게 AI가 대체 못 하는 직업?"  (230건)
# → 본문에서 위 패턴을 포함한 줄 전체를 제거 후 매칭.
# ──────────────────────────────────────────────────────────────────────
_YTN_FOOTER_RE = "(AI ?앵커|AI가 대체 못 하는)"
YTN_FOOTER_RE = _YTN_FOOTER_RE  # public alias


# ──────────────────────────────────────────────────────────────────────
# Rule 2 — MBC boilerplate substring 제거 (2026-05-21)
# ──────────────────────────────────────────────────────────────────────
# MBC가 2025년부터 모든 기사 footer에 부착한 "(AI학습 포함) 금지" boilerplate.
# 본문 진짜 표현 'AI 학습'(띄어쓰기 656건) · 'AI학습'(붙임 61건)은 보존하기 위해
# `(AI학습 포함)` / `(AI 학습 포함)` substring만 정확히 제거.
# ──────────────────────────────────────────────────────────────────────

# Rule 1 + Rule 2를 함께 적용한 본문 전처리 SQL 식.
# regex_replace로 footer 라인 제거 후, 두 단계 REPLACE로 MBC substring 제거.
_CLEAN = (
    "REPLACE(REPLACE("
    f"regexp_replace(content, '[^\\n]*{_YTN_FOOTER_RE}[^\\n]*', '', 'g'),"
    " '(AI학습 포함)', ''), '(AI 학습 포함)', '')"
)
CLEANED_CONTENT_SQL = _CLEAN  # public alias


# ──────────────────────────────────────────────────────────────────────
# Rule 3 — 키워드 매칭 (OR) — Korean ILIKE + English word boundary
# ──────────────────────────────────────────────────────────────────────
# title은 raw 그대로 매칭. content는 _CLEAN 적용 후 매칭.
# - 한국어: '인공지능' / '인공 지능' substring
# - 영문 AI: word boundary (?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)
#   * 한글 조사('AI가','AI는','AI를')는 자연스럽게 잡힘 (한글 = non-ASCII).
# - 영문 A.I / A.I.: word boundary (?i)(^|[^A-Za-z0-9])A\.I\.?([^A-Za-z0-9]|$)
#
# 'artificial intelligence' 풀스펠은 Rule 4 (영문 본문 제외)와 완전 중복이라 제거됨
# (한국어 본문에 풀스펠만 단독 등장하는 케이스 0건 확인, 2026-05-22).
# 본 절은 STRICT_WHERE 본체의 OR 절로 직접 결합.
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
# Rule 4 — 영문 본문 제외 (한국 도메스틱 담론 대상 아님, 2026-05-22)
# ──────────────────────────────────────────────────────────────────────
# YTN [K-SCIENCE]·[K-BIZ] 등 외신용 영문 단신 207건 등 한국어 외 본문은
# 본 분석 대상에서 제외. 한글 char 비율 5% 미만 + 200자 이상이면 영문 판정.
# raw content 기준 (cleaning 결과와 무관).
# ──────────────────────────────────────────────────────────────────────
_IS_ENGLISH = (
    "LENGTH(content) > 200 "
    "AND LENGTH(regexp_replace(content, '[가-힣]', '', 'g'))::FLOAT "
    "    / LENGTH(content) > 0.95"
)


# ──────────────────────────────────────────────────────────────────────
# Rule 5 — 조류인플루엔자(Avian Influenza) 약자 충돌 제외 (2026-05-22)
# ──────────────────────────────────────────────────────────────────────
# 농림축산식품부 보도자료에서 'AI'를 조류인플루엔자 약자로 사용
# ('AI 확진', '고병원성 AI', 'AI 양성 반응' 등) → word boundary 정규식 통과.
# 검증:
#   조류 + 인공지능 부재 = 3,287 false positive
#   조류 + 인공지능 동시 (보존 대상) = 24건뿐
# → 조류인플루엔자 키워드 있고 한국어 '인공지능'/'인공 지능' 부재면 제외.
# ──────────────────────────────────────────────────────────────────────
_IS_BIRD_FLU_FP = (
    "(content ILIKE '%조류인플루엔자%' OR content ILIKE '%조류 인플루엔자%' "
    " OR title ILIKE '%조류인플루엔자%' OR title ILIKE '%조류 인플루엔자%') "
    "AND content NOT ILIKE '%인공지능%' AND title NOT ILIKE '%인공지능%' "
    "AND content NOT ILIKE '%인공 지능%' AND title NOT ILIKE '%인공 지능%'"
)


# ──────────────────────────────────────────────────────────────────────
# Rule 6 — 사이버대학 광고 통째 제외 (2026-05-24)
# ──────────────────────────────────────────────────────────────────────
# 사이버대학교(주로 중앙일보 보도자료, 일부 한겨레·YTN) 신·편입생 모집 광고가
# 본문에 'AI공학과·인공지능학과·AI융합' 학과명을 포함해 Strict 통과:
#   사이버대학 명칭 포함 = 825건 raw, Strict 통과 749건 (90.8% 통과율)
#   └ 중앙일보 703건 (93.8%) 집중
# sample 검토 결과 진짜 AI 정책 분석 대상은 5건 미만.
# 명칭 substring 하나로 사이버대학교·사이버대학원 등 변형 모두 포괄.
# ──────────────────────────────────────────────────────────────────────
_IS_CYBER_UNIV_AD = "content ILIKE '%사이버대학%' OR title ILIKE '%사이버대학%'"


# ──────────────────────────────────────────────────────────────────────
# Rule 7 — 일반 대학 AI 학과 모집 광고 제외 (footer 3중 조합, 2026-05-24)
# ──────────────────────────────────────────────────────────────────────
# 사이버대학 제외 후 일반 대학 "모집+AI학과명" 동시 등장 = 4,647건이나
# 대부분은 정책 보도·칼럼·정부 보도자료. 단순 키워드 조합으로 제외하면
# collateral 과도.
# → 광고 footer 시그너처 3중 조합으로 정확 매칭:
#   ① (문의 OR 입학처)
#   ② (모집요강 OR 원서접수 OR 접수마감)
#   ③ (장학금 OR 등록금)
# 세 그룹 모두 충족 시 광고로 확정 → 557건 제외 (collateral 5% 미만 추정).
# ──────────────────────────────────────────────────────────────────────
_IS_UNIV_RECRUIT_AD = (
    "(content ILIKE '%문의%' OR content ILIKE '%입학처%') "
    "AND (content ILIKE '%모집요강%' OR content ILIKE '%원서접수%' "
    "     OR content ILIKE '%접수마감%') "
    "AND (content ILIKE '%장학금%' OR content ILIKE '%등록금%')"
)


# ──────────────────────────────────────────────────────────────────────
# 최종 STRICT_WHERE = Rule 3 (키워드 매칭)
#                     AND NOT Rule 4 (영문) AND NOT Rule 5 (조류)
#                     AND NOT Rule 6 (사이버대학) AND NOT Rule 7 (일반대 광고)
# ──────────────────────────────────────────────────────────────────────
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
  AND NOT ({_IS_BIRD_FLU_FP})
  AND NOT ({_IS_CYBER_UNIV_AD})
  AND NOT ({_IS_UNIV_RECRUIT_AD})
)
"""


__all__ = [
    "STRICT_WHERE",
    "CLEANED_CONTENT_SQL",
    "YTN_FOOTER_RE",
]
