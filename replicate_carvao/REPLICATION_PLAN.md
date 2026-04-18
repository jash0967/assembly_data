# Replication Plan: Carvão et al. (2025) — 118th Congress AI Bills Analysis

## 목표

"Governance at a Crossroads" (SSRN 5131048) Appendix II의 118th Congress AI 법안 정량 분석을 완전 재현한다. 동일한 데이터 소스, 동일한 방법론으로 논문의 Figure 8~35를 복제하고, 결과를 비교 검증한다.

## 데이터 소스

| 소스 | 용도 | 상태 |
|------|------|------|
| Brennan Center AI Legislation Tracker | 150건 AI 법안 목록 | **확보 완료** (154건, 논문 이후 4건 추가) |
| Congress.gov API v3 | 법안 상세정보, 전문, 스폰서, 위원회, 진행이력 | **수집 중** |
| GPT-4o API | NLP 분류 검증 | 미착수 |

## 파이프라인 개요

```
Step 1: 법안 목록 확보 (Brennan Center)          ✅ 완료
   └→ data/brennan_118th_bills.json (154건)

Step 2: Congress.gov API 상세 수집               🔄 진행 중
   └→ data/bills_detail.json
   각 법안별 6개 엔드포인트 호출:
     /bill/118/{type}/{number}           기본 정보 + policy area
     /bill/118/{type}/{number}/cosponsors  공동발의자 목록
     /bill/118/{type}/{number}/committees  위원회 배정
     /bill/118/{type}/{number}/actions     전체 진행 이력
     /bill/118/{type}/{number}/summaries   CRS 요약
     /bill/118/{type}/{number}/text        법안 전문 URL

Step 3: 데이터 전처리                            ⬜ 미착수
   └→ data/bills_processed.json
   - Bill Progression Stage 0~10 분류 (논문 Figure 18 regex)
   - Sponsor party/state 매핑
   - Bipartisanship 계산 (Democratic cosponsor ratio)
   - Cosponsor 중복 제거

Step 4: 법안 전문 다운로드                        ⬜ 미착수
   └→ data/bills_text/
   - text_versions URL에서 Formatted Text 다운로드
   - HTML 태그 제거 → 순수 텍스트

Step 5: 메타데이터 시각화                         ⬜ 미착수
   └→ figures/
   - Figure 8~10, 22~30 재현

Step 6: NLP 분류 (TF-IDF + LDA)                 ⬜ 미착수
   └→ data/tfidf_classification.json
   └→ data/lda_topics.json
   - 법안 전문 TF-IDF → 10개 Policy Attribute 분류
   - LDA 비지도 토픽 모델링 (10개 토픽)
   - TF-IDF vs LDA 교차 히트맵

Step 7: GPT 검증                                ⬜ 미착수
   └→ data/gpt_classification.json
   - GPT-4o-mini로 동일 분류 → NLP 결과와 비교

Step 8: Policy Attribute 시각화                  ⬜ 미착수
   └→ figures/
   - Figure 11~12, 31~35 재현
```

## 재현 대상 Figure 목록

### A. 메타데이터 분석 (Step 5)

| Figure | 내용 | 차트 유형 | 검증 기준 |
|--------|------|---------|----------|
| 8 | Bill sponsorship by party (Congress) | 막대 | D=99, R=50, I=1 |
| 9 | Bipartisanship scatter (Congress) | 산점도 | x=Dem ratio, y=total sponsors |
| 10 | AI bills by state | 막대 | CA=23, 36 states |
| 22 | Monthly distribution | 선 그래프 ×3 | Congress/House/Senate |
| 23 | Sponsorship by chamber | 막대 ×3 | H: D49/R31, S: D50/R19/I1 |
| 24 | Bipartisanship by chamber | 산점도 ×2 | House, Senate 분리 |
| 25 | Bipartisanship Congress total | 산점도 | 전체 |
| 26 | Bills by state | 막대 ×3 | Congress/House/Senate |
| 27 | Most active members (House) | 막대 | 의원별, 색=정당 |
| 28 | Most active members (Senate) | 막대 | Rounds/Markey 7건 |
| 29 | Most active committees | 막대 ×2 | House/Senate |
| 30 | Bill progression | 막대 | Stage 0~10 분포 |

### B. NLP 분류 분석 (Step 6~8)

| Figure | 내용 | 차트 유형 | 검증 기준 |
|--------|------|---------|----------|
| 11/31상 | Primary policy attributes | 막대 | Resp.AI=71, NatSec=24 |
| 12/31하 | Top 3 policy attributes | 막대 | Resp.AI=141, NatSec=106 |
| 20 | TF-IDF bill categories | 막대 | 10개 카테고리 분포 |
| 21 | TF-IDF vs LDA heatmap | 히트맵 | 10×10 교차표 |
| 32 | Primary attribute by party | 막대 ×2 | Republican, Democrat |
| 33 | Top 3 attributes by party | 막대 ×2 | Republican, Democrat |
| 34 | Stage 3+ bills attributes | 막대 | 17건 중 분포 |
| 35 | Legislative calendar bills | 표 | 17건 목록 |

## 10개 Policy Attributes 정의

논문 p.92에서 정의한 분류 체계:

1. **Market efficiency and power concentration (antitrust)** — 시장 경쟁, 독과점, 빅테크 규제
2. **Safety** — AI 시스템 안전성, 테스트, 위험 관리
3. **Responsible and ethical AI** — 책임있는 AI 개발/배포, 윤리, 편향, 투명성
4. **National security** — 국방, 정보기관, 사이버보안, 중국 경쟁
5. **Industrial policy** — AI 산업 육성, R&D 투자, CHIPS, 인프라
6. **Public interest** — 소비자 보호, 공공서비스, 교육, 의료
7. **Labor** — 고용 영향, 자동화, 재교육, 노동자 보호
8. **Copyright** — 지적재산, AI 생성 콘텐츠, 저작권
9. **International collaboration** — 국제 협력, 표준, 동맹
10. **Elections** — 선거, 딥페이크, 정치적 AI 사용, 유권자 보호

## Bill Progression Regex (논문 Figure 18)

```python
STAGE_KEYWORDS = {
    0: [r"\bIntroduced\b", r"Referred to (?!.*Subcommittee).*Committee"],
    1: [r"Referred to Subcommittee\b", r"\bSubcommittee\b"],
    2: [r"\bMark-up\b", r"\bMarkup\b", r"\bMark up\b"],
    3: [r"\bReported\b", r"ordered to be reported", r"\bleas\b", r"\bNays\b"],
    4: [r"Placed on calendar", r"\bcalendar\b", r"Calendar No\."],
    5: [r"Discharged", r"Considered under suspension of the rules", r"Measure laid before Senate"],
    6: [r"Passed Senate", r"Passed House", r"pass the bill"],
    7: [r"Received in the Senate", r"Received in the House"],
    8: [r"Senate agreed to", r"House agreed to"],
    9: [r"Presented to President"],
    10: [r"Signed by President"],
}
```

## 검증 기준 (Expected Values)

### 정확히 일치해야 하는 항목

| 항목 | 논문 값 |
|------|--------|
| Democratic sponsor | 99 |
| Republican sponsor | 50 |
| Independent sponsor | 1 |
| States represented | 36 |
| CA bills | 23 |
| Bills past Stage 3 (calendar) | 17 |
| Bills enacted into law | 0 |
| Senate top: Rounds, Markey | 7, 7 |
| House top: Lieu | 5 |
| Senate committee top: Commerce/Science/Transport | 33 |
| House committee top: Energy & Commerce | 20 |

### 방법론 차이로 오차 허용 항목

| 항목 | 논문 값 | 허용 오차 |
|------|--------|---------|
| Primary: Responsible AI | 71 | ±5 |
| Primary: National Security | 24 | ±5 |
| Primary: Safety | 14 | ±3 |
| TF-IDF 분류 분포 | Figure 20 | 경향성 일치 확인 |
| LDA 토픽 구성 | Figure 21 | 주요 교차점 일치 확인 |

## 파일 구조

```
replicate_carvao/
├── PAPER_SUMMARY.md          ← 논문 정리
├── REPLICATION_PLAN.md       ← 이 파일
├── 01_get_bill_list.py       ← (인라인으로 실행 완료)
├── 02_collect_bill_details.py ← Congress.gov API 수집
├── 03_preprocess.py          ← 전처리 + Stage 분류
├── 04_download_text.py       ← 법안 전문 다운로드
├── 05_metadata_viz.py        ← Figure 8~30 시각화
├── 06_nlp_classify.py        ← TF-IDF + LDA 분류
├── 07_gpt_validate.py        ← GPT-4o 검증
├── 08_policy_viz.py          ← Figure 31~35 시각화
├── data/
│   ├── brennan_118th_bills.json    ← 154건 법안 목록 ✅
│   ├── bills_detail.json           ← API 상세 정보 🔄
│   ├── bills_processed.json        ← 전처리 결과
│   ├── bills_text/                 ← 법안 전문 텍스트
│   ├── tfidf_classification.json   ← TF-IDF 분류 결과
│   ├── lda_topics.json             ← LDA 토픽 결과
│   └── gpt_classification.json     ← GPT 검증 결과
└── figures/
    ├── fig08_party.png ~ fig35_calendar.png
    └── (논문 Figure와 1:1 대응)
```

## 논문과의 차이점 / 리스크

1. **법안 수**: 논문 150건 vs Brennan Center 현재 154건 (4건 추가됨)
   - 대응: 날짜 기반으로 논문 시점 이후 추가분 식별 후 별도 표기

2. **TF-IDF → Policy Attribute 매핑 규칙**: 논문이 정확한 키워드 사전을 공개하지 않음
   - 대응: 각 Attribute의 정의와 맥락에서 키워드 리스트 직접 구축

3. **LDA 하이퍼파라미터**: 토픽 수(10) 외 세부 설정 미공개
   - 대응: 논문의 LDA 토픽 이름으로부터 역추론, coherence score로 최적화

4. **GPT 버전**: 논문은 GPT-4o, 우리는 GPT-4o-mini 사용 가능
   - 대응: GPT-4o-mini로 먼저 실행, 필요시 GPT-4o로 재실행

5. **Brennan Center 선별 기준**: 어떤 법안이 "AI-related"인지의 판단은 Brennan Center 연구자의 수동 식별
   - 대응: Brennan Center 목록을 그대로 사용 (논문과 동일)
