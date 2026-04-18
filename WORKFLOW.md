# 작업 파이프라인: 한·미·EU AI 입법·뉴스 비교 분석

> 이 문서는 본 프로젝트의 모든 데이터 흐름, 필터 기준, 분류 방법, 산출물을 단일 참고자료로 기록.
> 보고서 [report_expanded_draft.md](report_expanded_draft.md)와 함께 본 파이프라인이 정본.

---

## 0. 프로젝트 개요

**연구 질문**: 한·미·EU의 AI 거버넌스를 입법 활동과 언론 담론 측면에서 정책 속성별로 어떻게 비교할 것인가?

**데이터 8종**:
1. 한국 국회 19~22대 법안
2. 미국 의회 118·119대 법안
3. EU AI Act 본문 + 수정안
4. Guardian (영국 뉴스)
5. NYT (미국 뉴스)
6. Naver (한국 뉴스)

**분석 프레임워크**: Carvão et al. (2025) *"Governance at a Crossroads"* — Harvard Kennedy School Working Paper. 10개 정책 속성 체계 (Figure 5 / Appendix II p.92).

**LLM**: `gpt-4.1-mini` (OpenAI API). 분류·필터·요약 공통.

---

## 1. 10개 정책 속성 (공통 분류 체계)

Carvão Appendix II 기준 정본:

| # | 영문 원본 | 한국어 대응 |
|---|-----------|------------|
| 1 | Market efficiency and power concentration (antitrust) | 시장경쟁/독과점 |
| 2 | Safety | AI안전 |
| 3 | Responsible and ethical AI | 책임/윤리AI |
| 4 | National security | 국가안보 |
| 5 | Industrial policy | 산업정책 |
| 6 | Public interest | 공익/소비자보호 |
| 7 | Labor | 노동/고용 |
| 8 | Copyright | 저작권/지식재산 |
| 9 | International collaboration | 국제협력 |
| 10 | Elections | 선거/민주주의 |

**분류 프롬프트 정본**: [prompts.py](prompts.py) — `SYSTEM_PROMPT` 하나를 뉴스·법안 모두에 동일 적용.
- Carvão 논문이 각 속성을 명시적으로 정의하지 않아, 논문 본문 맥락에서 정의·예시·혼동 규칙을 재구성한 영문 v2 프롬프트 사용
- 한·영 이중 맥락 예시 포함 (예: FTC / 공정거래위원회, NSF / 과기정통부)
- 경계 규칙 5개 (산업정책 vs 시장경쟁, 안전 vs 윤리, 공익 vs 노동, 딥페이크의 맥락, 국제협력 vs 국가안보)
- `none` 판정 4 기준 (제품 홍보·주가 분석·기술 동향·도구적 언급)

---

## 2. 데이터 수집 파이프라인

### 2.1 한국 국회 법안 — [download_bills.py](download_bills.py) + [collector.py](collector.py)

1. 열린국회정보 Open API (`Assembly API`)로 19~22대 법안 메타데이터 수집
2. 국민참여입법센터·국회의안정보시스템 크롤링으로 제안이유·주요내용 텍스트 확보
3. 법안별 개별 JSON 저장: `data/bill_txt_{19,20,21,22}/PRC_*.json`
4. 각 JSON 필드: `bill_id`, `bill_name`, `proposer`, `propose_date`, `committee`, `reason_and_content`, `full_text` 등

**규모**:
- 19대: 15,426건
- 20대: 21,593건
- 21대: 23,639건
- 22대: 16,446건 (2026-04-18 기준)

### 2.2 미국 의회 법안

- **118대 (2023-01 ~ 2025-01)**: [replicate_carvao/02_collect_bill_details.py](replicate_carvao/02_collect_bill_details.py) — Brennan Center for Justice가 선별한 AI 법안 154건 대상. Congress.gov API로 상세 수집.
- **119대 (2025-01 ~ )**: [replicate_carvao/us119_run_all.py](replicate_carvao/us119_run_all.py) — 동일 파이프라인, 53건.
- 본문 텍스트: `replicate_carvao/data/bills_text/` (118th), `us119_bills_text/` (119th)
- 메타데이터: `bills_processed.json`, `us119_bills_processed.json`

### 2.3 EU AI Act + 수정안

- [eu_01_collect_ai_act.py](eu_01_collect_ai_act.py): EUR-Lex에서 AI Act 조문 116개 수집 → `data/eu_ai_act_articles.json`
- [eu_02_collect_amendments.py](eu_02_collect_amendments.py): European Parliament 제출 수정안 771건 수집 → `data/eu_amendments.json`

### 2.4 뉴스 3소스

#### Guardian — [collect_guardian.py](collect_guardian.py)
- **API**: Guardian Content API
- **쿼리**: `"artificial intelligence"`, `"A.I."`, `AI`
- **기간**: 2016-03 ~ 2026-04 (11개 시간 슬롯)
- **섹션 필터** (14개): technology, business, politics, world, us-news, uk-news, australia-news, science, law, global-development, environment, society, commentisfree, education, media
- **출력**: `data/guardian_articles_raw.json` (11,120건)
- 필드: `id`, `title`, `trail_text`, `url`, `section`, `pub_date`, `word_count`, `query`

#### NYT — [collect_nyt.py](collect_nyt.py)
- **API**: NYT Archive API (월별 전체 기사 다운로드)
- **1차 필터**: 정규식 매칭 `artificial intelligence`, `A.I.`, `AI` (headline + abstract + snippet + lead_paragraph + keywords)
- **2차 필터 (desk 화이트리스트 12개)**: Business, Washington, Foreign, Science, Politics, National, SundayBusiness, OpEd, Climate, Investigative, Express, NYTNow. 빈 desk도 포함.
- **기간**: 2016-03 ~ 2026-04
- **캐시**: `data/nyt_archive/{year}_{month}.json`
- **출력**: `data/nyt_articles_raw.json` (3,108건)

#### Naver — 여러 단계 반복
**배경**: Naver Search API는 쿼리당 1,000건 상한, 기간 필터 미지원. 다단계 수집을 거쳐 최종 확정.

**최종 수집 방식** ([collect_naver_v3.py](collect_naver_v3.py)):
1. **언론사 도메인 × 키워드 쿼리** (16 매체 × 2 broad 키워드 = 32 쿼리):
   - 쿼리 형식: `"chosun.com 인공지능"` — 도메인명을 쿼리에 넣으면 네이버가 해당 언론사 기사 위주로 반환 (82~98% 도메인 일치)
   - 매체 리스트: chosun.com, joongang.co.kr, donga.com, hani.co.kr, khan.co.kr (종합일간지) / mk.co.kr, hankyung.com, heraldcorp.com, edaily.co.kr (경제지) / zdnet.co.kr, etnews.com, bloter.net, ddaily.co.kr (IT 전문) / yna.co.kr, news1.kr, newsis.com (통신사)
   - 키워드: `인공지능`, `AI`
2. **2년 기간 필터**: 2024-04-18 ~ 2026-04-17 (network·인덱스 편차로 장기 수집 어려움)
3. **특수 필터**:
   - 뉴시스 포토기사 제외 (`/view/NISI` URL 패턴)
   - 연합뉴스 영문판 `en.yna.co.kr` 제외
4. **URL + 제목 dedup**
5. **세부 주제 확장** ([collect_subtopic_expand.py](collect_subtopic_expand.py)): 8 매체 × 10 세부 주제 키워드(AI 규제·윤리·안전·저작권·국가안보·노동·선거·딥페이크·챗GPT·생성형 AI) = 80 쿼리로 통신사 비중 보완
6. **본문 기반 검증** ([fetch_bodies.py](fetch_bodies.py)): `n.news.naver.com` 네이버 뉴스 페이지 fetch → 본문에서 AI/인공지능 3회 이상 등장 확인
7. **제목 필터 최종 적용**: 제목에 `AI`/`인공지능`/`인공 지능`/`A.I.` 중 하나 포함 ([export_titles_title_filter.py](export_titles_title_filter.py))

**출력**: `data/naver_articles_title_filtered.json` (449건, 8개 매체)
- 매체별 분포: 중앙(208), 경향(92), 조선(67), 연합(31), 동아(21), 뉴시스(13), 한겨레(11), 뉴스1(6)

**분석에 사용하는 매체**: **8개** (일간지 조·중·동·한·경 + 통신사 연합·뉴스1·뉴시스)
- IT 전문지(zdnet/etnews/bloter/ddaily) 및 경제지(mk/한경/헤럴드/이데일리)는 수집은 됐으나 본문 검증·제목 필터 통과 후 10건 미만으로 최종 분석에서 제외됨

**관련 데이터 파일**:
- 원본 수집: `data/naver_articles_v3_raw.json` (27,720건, 8개 매체 전)
- 클린본 (광범위): `data/naver_articles_v3_clean.json` (3,379건, 뉴시스 포토 제거)
- 본문 검증: `data/naver_articles_filtered.json` (1,207건, 본문 AI 3회 이상)
- 세부주제 통합: `data/naver_articles_final.json` (1,307건)
- **제목 필터 최종 (정본)**: `data/naver_articles_title_filtered.json` (449건)
- 본문 fetch 캐시: `data/naver_bodies_cache.json`

---

## 3. AI 관련성 필터 (법안 대상)

### 3.1 한국 국회 법안 — 2단계 필터 (정본)

**스크립트**: [classify_bills.py](classify_bills.py) 내 `stage1_keyword_filter_kr()` + `stage2_gpt_filter_kr()`

#### Stage 1 — 키워드 후보 선별
- 패턴: `인공지능|AI|A\.I`
- 기준: 법안명 + 제안이유·주요내용 전문에 **3회 이상** 언급
- 중복 제거: `(법안명, 대표발의자)` 기준 — 최신 발의일만 유지

#### Stage 2 — GPT core/adjacent/unrelated 판별
- 모델: gpt-4.1-mini
- 프롬프트 ([classify_bills.py](classify_bills.py) 내 `AI_FILTER_PROMPT`):
  - **core**: AI가 법안의 주된 목적 (AI 기본법, AI 산업육성법, AI 책임법 등)
  - **adjacent**: AI가 핵심 trigger이거나 AI 관련 실질 조항 포함
  - **unrelated**: 배경 언급만. AI 없이도 법안 성립
- 핵심 판단: *"이 법안에서 AI 관련 내용을 삭제하면 법안의 존재 이유가 사라지는가?"*
- 최종 AI 법안 = **core + adjacent만** (unrelated 제거)

**캐시**: `data/kr_{age}_ai_filtered.json` — Stage 2 결과 전체 (classification·is_ai_bill·gpt_reason·ai_provisions 포함). 재실행 시 재사용.

**왜 2단계인가**: 단순 키워드 카운트만 쓰면 "조류인플루엔자(AI) 예방법", "AI 시대에 대응하여..." 같은 주변적 언급 법안이 대거 포함되어 AI 법안 수가 부풀어짐. GPT 판별로 본질적 AI 법안만 남김.

### 3.2 미국 의회 법안
- **별도 필터 불필요**: Brennan Center for Justice가 선별한 AI 법안 리스트 (118대 154건)를 원본 기준으로 사용. 119대는 동일 기준 53건.
- `classify_bills.py`의 `load_us_bills()`는 10속성 분류만 수행.

### 3.3 EU AI Act
- **별도 필터 불필요**: 전체가 AI 규제 법안. 조문 116개 + 수정안 771건 모두 분류 대상.

### 3.4 뉴스 (전 소스 공통)
- **섹션/desk 필터**: Guardian 14개 섹션, NYT 12개 desk 화이트리스트 (수집 단계).
- **Naver 도메인 필터**: 쿼리 자체가 `domain.com keyword` 형식이라 자동 필터.
- **제목 키워드 필터** ([classify.py](classify.py) 내 `title_has_kw`): 제목에 `\bAI\b | artificial intelligence | A.I. | 인공지능 | 인공 지능` 중 하나 포함. 세 소스 모두 동일 적용.

---

## 4. 10속성 분류 — [classify.py](classify.py) / [classify_bills.py](classify_bills.py)

### 4.1 공통 사양
- **프롬프트**: [prompts.py](prompts.py)의 `SYSTEM_PROMPT` (영문 v2, 뉴스·법안 공용)
- **모델**: `gpt-4.1-mini`, `temperature=0`, `response_format=json_object`
- **출력 형식**: `{"primary": "<Label>", "secondary": "<Label or none>", "tertiary": "<Label or none>"}`
- **라벨 공간**: 10속성 영문 문자열 + `"none"`
- **병렬**: ThreadPoolExecutor (워커 4) + 429 rate-limit exponential backoff
- **캐시**: 각 출력 JSON에서 error 항목만 재시도, success는 재사용
- **출력 필드**: `primary`, `secondary`, `tertiary`, `article_id` 또는 `id`, `title`

### 4.2 뉴스 분류
- 입력: 제목 + description(150자 스니펫)
- 타겟 키워드 필터 거친 후 분류
- 출력:
  - `data/news_guardian_classified.json` (2,310건)
  - `data/news_nyt_classified.json` (1,326건)
  - `data/news_naver_classified.json` (449건)

### 4.3 법안 분류
- 입력: 법안명 + 제안이유 앞 2,000자 (KR) / 법안 원문 앞 3,000자 (US) / 조문 앞 2,500자 (EU)
- 출력:
  - `data/bills_classified_kr_{19,20,21,22}.json`
  - `data/bills_classified_us_{118,119}.json`
  - `data/bills_classified_eu_{act,amendments}.json`

---

## 5. 산출 스크립트

### 5.1 수집
| 스크립트 | 역할 |
|----------|------|
| [download_bills.py](download_bills.py) | 한국 법안 API 수집 orchestrator |
| [collector.py](collector.py) | 법안 크롤링 구현 |
| [collect_guardian.py](collect_guardian.py) | Guardian API 수집 |
| [collect_nyt.py](collect_nyt.py) | NYT Archive API 수집 |
| [collect_naver_v3.py](collect_naver_v3.py) | Naver v3 (16 매체 × 2 키워드) |
| [collect_subtopic_expand.py](collect_subtopic_expand.py) | Naver 세부주제 80 쿼리 확장 |
| [fetch_bodies.py](fetch_bodies.py) | n.news.naver.com 본문 fetch 및 AI 빈도 카운트 |
| [eu_01_collect_ai_act.py](eu_01_collect_ai_act.py) | EU AI Act 조문 수집 |
| [eu_02_collect_amendments.py](eu_02_collect_amendments.py) | EU 수정안 수집 |
| [replicate_carvao/02_collect_bill_details.py](replicate_carvao/02_collect_bill_details.py) | US 118대 법안 상세 |
| [replicate_carvao/us119_run_all.py](replicate_carvao/us119_run_all.py) | US 119대 전체 파이프라인 |

### 5.2 분류
| 스크립트 | 역할 |
|----------|------|
| [prompts.py](prompts.py) | 통일 10속성 분류 프롬프트 (v2 영문) |
| [classify.py](classify.py) | 뉴스 3소스 분류 (Guardian/NYT/Naver) |
| [classify_bills.py](classify_bills.py) | 법안 분류 (KR 2단계 필터 포함 + US + EU) |

### 5.3 내보내기
| 스크립트 | 역할 |
|----------|------|
| [export_titles.py](export_titles.py) | 뉴스 제목 리스트 속성별 (매체/desk 소분류) |
| [export_titles_title_filter.py](export_titles_title_filter.py) | 제목 키워드 필터 적용 리스트 |
| [export_bills_kr.py](export_bills_kr.py) | 한국 법안 속성별 리스트 + 대수별 교차표 |

### 5.4 분석·시각화
| 스크립트 | 역할 |
|----------|------|
| [generate_timeline.py](generate_timeline.py) | 연도별 법안·뉴스 시계열 그림 |
| [generate_timeline_pct.py](generate_timeline_pct.py) | 비율 기반 시계열 |
| [generate_figures.py](generate_figures.py) | 보고서용 그림 생성 |
| [build_treemap_data.py](build_treemap_data.py) | 트리맵 데이터 구축 |
| [build_treemap_kr.py](build_treemap_kr.py) | 한국 법안 트리맵 |
| [subtopic_bertopic_v4.py](subtopic_bertopic_v4.py) | BERTopic 소주제 추출 (최신 v4) |

---

## 6. 데이터 흐름 개요

```
┌──────────────────────┐    ┌────────────────────────┐    ┌──────────────────────┐
│  수집 (Collection)   │ →  │  필터 (Filtering)      │ →  │ 분류 (Classification)│
├──────────────────────┤    ├────────────────────────┤    ├──────────────────────┤
│ Guardian API         │    │ 섹션 14개 화이트리스트  │    │                      │
│   → guardian_raw     │    │ 제목 AI 키워드 필터    │    │  prompts.SYSTEM_PROMPT│
│                      │    │                        │    │  ↓                   │
│ NYT Archive API      │    │ desk 12개 + 키워드 2중 │    │  gpt-4.1-mini        │
│   → nyt_raw          │    │ 제목 AI 키워드 필터    │    │  temperature=0       │
│                      │    │                        │    │  JSON output         │
│ Naver Search API     │    │ 도메인 × 키워드 매칭   │    │  {primary, sec, ter} │
│ (여러 단계)          │    │ 2년 / 뉴시스 포토 제외 │    │                      │
│   → naver_v3_clean   │    │ 본문 AI 3회 이상 검증  │    │                      │
│                      │    │ 제목 AI 키워드 필터    │    │                      │
│                      │    │   → 449건               │    │                      │
│                      │    │                        │    │                      │
│ KR Open API          │    │ Stage 1: 키워드 3회+   │    │                      │
│   → bill_txt_{age}/  │    │ Stage 2: GPT core/adj/ │    │                      │
│                      │    │ unrelated (unrelated 제외)│    │                   │
│                      │    │                        │    │                      │
│ US Brennan + Congress│    │ (Brennan 선별 사용)    │    │                      │
│ gov API              │    │                        │    │                      │
│                      │    │                        │    │                      │
│ EU eur-lex           │    │ (AI Act 전체)          │    │                      │
│   → eu_ai_act_*      │    │                        │    │                      │
└──────────────────────┘    └────────────────────────┘    └──────────────────────┘
```

---

## 7. 전체 스크립트 실행 순서 (재현용)

### 신규 실행
```bash
# 1. 한국 법안 수집 (데이터베이스·텍스트 파일)
python download_bills.py

# 2. EU 수집
python eu_01_collect_ai_act.py
python eu_02_collect_amendments.py

# 3. 미국 수집
python replicate_carvao/02_collect_bill_details.py          # 118th
python replicate_carvao/us119_run_all.py                    # 119th

# 4. 뉴스 수집
python collect_guardian.py
python collect_nyt.py
python collect_naver_v3.py                                  # Naver broad
python collect_subtopic_expand.py                           # Naver subtopic
python fetch_bodies.py                                      # Naver body verify
python export_titles_title_filter.py                        # title filter 적용

# 5. 분류
python classify.py all                                      # 뉴스 3소스
python classify_bills.py all                                # 법안 6소스 (KR 2단계 포함)

# 6. 내보내기
python export_titles.py all                                 # 뉴스 속성별
python export_bills_kr.py                                   # 법안 속성별

# 7. 시각화
python generate_timeline.py
python generate_figures.py
```

### 재실행 (캐시 활용)
- 모든 `classify*.py`는 출력 JSON 존재 시 **error 항목만 재시도**, 성공 항목은 재사용
- `kr_{age}_ai_filtered.json` (Stage 2 결과) 재사용 — Stage 2 GPT 필터 비용 절감
- `naver_bodies_cache.json` (본문 fetch 캐시) 재사용
- 네이버 raw JSON은 `collect_naver_v3.py` 내부에서 캐시 확인 후 스킵

---

## 8. 주요 출력 파일 체크리스트

### 원본 수집
- [ ] `data/guardian_articles_raw.json` (11,120)
- [ ] `data/nyt_articles_raw.json` (3,108)
- [ ] `data/naver_articles_v3_raw.json` (27,720)
- [ ] `data/bill_txt_{19,20,21,22}/*.json` (수만 건)
- [ ] `data/eu_ai_act_articles.json` (116)
- [ ] `data/eu_amendments.json` (771)
- [ ] `replicate_carvao/data/bills_processed.json` (US 118, 154)
- [ ] `replicate_carvao/data/us119_bills_processed.json` (US 119, 53)

### 필터·전처리
- [ ] `data/naver_articles_v3_clean.json` (3,379)
- [ ] `data/naver_articles_filtered.json` (1,207, 본문 AI 3회)
- [ ] `data/naver_articles_final.json` (1,307, 세부주제 통합)
- [ ] `data/naver_articles_title_filtered.json` (449, **정본**)
- [ ] `data/naver_bodies_cache.json` (fetch 캐시)
- [ ] `data/kr_{19,20,21,22}_ai_filtered.json` (2단계 필터 결과)

### 10속성 분류 결과 (정본)
- [ ] `data/news_guardian_classified.json` (2,310)
- [ ] `data/news_nyt_classified.json` (1,326)
- [ ] `data/news_naver_classified.json` (449)
- [ ] `data/bills_classified_kr_{19,20,21,22}.json`
- [ ] `data/bills_classified_us_{118,119}.json`
- [ ] `data/bills_classified_eu_{act,amendments}.json`

### 사람 열람용 마크다운
- [ ] `data/titles_{guardian,nyt,naver}_by_category.md`
- [ ] `data/bills_kr_{19,20,21,22}_by_category.md`
- [ ] `data/bills_kr_all_by_category.md`

### 보고서
- [ ] `report_expanded_draft.md` — 최신 정본 보고서
- [ ] `report.pdf` / `report.tex` — LaTeX 빌드

---

## 9. 주요 설계 결정 (What · Why)

### 9.1 통일 영문 v2 프롬프트
- **What**: 한국어 뉴스·법안 포함 모든 소스에 동일 영문 프롬프트 적용, 출력 라벨도 영문 통일
- **Why**: GPT 내부 추론이 영어 기반이라 분류 일관성 높음. 라벨이 영문으로 고정되면 세 국가(한/미/영) 간 직접 비교 가능. Carvão 원문 용어 그대로 보존.

### 9.2 한국 법안 2단계 필터
- **What**: 키워드 3회+ 1차 후보 → GPT core/adjacent/unrelated 2차 → unrelated 제거
- **Why**: 단순 키워드만 쓰면 "조류인플루엔자(AI)" 처럼 주변적 언급 법안이 섞여 수가 부풀어짐. GPT 판별로 "AI 내용을 빼도 법안이 성립하는지" 판단.

### 9.3 Naver 수집 방식: 언론사 도메인 × 키워드
- **What**: `"chosun.com 인공지능"` 같은 쿼리로 언론사별 1,000건 상한을 분리해서 확보
- **Why**: Naver API는 쿼리당 1,000건 상한에 기간 필터 없음. broad 쿼리 하나만 쓰면 최근 하루치만 잡힘. 도메인 쿼리로 언론사당 최대 1,000건, 수년간 span 확보.

### 9.4 Naver 본문 검증 (n.news.naver.com)
- **What**: API가 반환한 description(150자)에는 사이드바·추천 기사 등 노이즈 섞여 있어, 네이버 뉴스 재게시 페이지를 개별 fetch해 본문 AI 빈도 확인
- **Why**: 중앙일보 "팩플" 같은 AI 시리즈가 모든 기사 사이드바에 링크 노출 → AI 무관 기사도 description에 AI 다수 등장. 실제 본문 기준 필터 필요.

### 9.5 뉴스 제목 키워드 필터 최종 적용
- **What**: 수집·분류가 끝난 뒤에도 "제목에 AI 키워드" 조건으로 1차 scope 좁혀 비교 정본 수집물 확정
- **Why**: 동일 조건을 세 소스에 적용해야 비교 공정성 유지. 본문만 AI 언급하고 제목은 다른 주제인 기사는 국가 간 담론 비교에서 편향 유발.

---

## 10. 작업 이력 주요 분기

### 2026-04-18 (current) — 프롬프트 통일·법안 필터 재구축 + 어댑터 제거
- 모든 구 버전 v1 프롬프트 폐기, 통일 영문 v2 프롬프트 단일화
- `prompts.py` 공통 모듈로 분리
- `classify.py` (뉴스) / `classify_bills.py` (법안) 2개 파일로 통합
- 한국 법안 2단계 GPT 필터를 `classify_bills.py`에 내장 — 이전 `kr_analysis/kr_01_prepare_data.py` 파이프라인과 기능적 동등
- `replicate_carvao/` 폴더를 미국 논문 replicate 전용으로 정리 — 한국 관련 스크립트·데이터는 `kr_analysis/` 신설 폴더로 이동
- 어댑터 제거: `bill_loaders.py` 신설로 `bills_classified_*.json`을 직접 로드. 중간 변환 파일(`*_policy_attr_all.json`)과 `build_legacy_bills.py` 모두 삭제. 소비자(`figures/regenerate_all.py`, `replicate_carvao/gen_{us,eu}_report.py`, `kr_analysis/validate_tfidf_lda.py`)는 모두 `bill_loaders`를 경유

### 2026-04-17 — Naver 수집 방법론 재정립
- 20개 정책 쿼리 방식 폐기 (쿼리별 기간 편차 심각)
- 2개 broad 키워드 × 16개 언론사 도메인 쿼리로 전환
- 네이버 뉴스 페이지 본문 fetch 도입
- 세부 주제 80 쿼리로 통신사 비중 보완

### 2026-04-15 — 제목 키워드 필터 확정
- 세 소스 모두 "제목에 AI 키워드 포함" 조건으로 비교 scope 좁힘

---

## 11. 알려진 한계와 후속 과제

- **한·미·EU 기간 비대칭**: 한국 뉴스는 Naver API 한계로 2년 단면만. 미·영은 10년 시계열. 해석 시 주의.
- **BigKinds 유료 전환**: 한국 뉴스 장기 수집은 BigKinds 구독 전제.
- **EU 단일 법안 vs 미·한 다법안 비대칭**: EU는 AI Act 단일 체계, 미·한은 개별 법안 다수. 수정안 771건으로 분량 간접 측정 중.
- **GPT 분류 경계 사례**: 특히 "공익 vs 책임/윤리 AI" 구분에서 5~10% 재라벨링 필요 (보고서 3.3 한계 참조).
- **Naver 매체 8개로 축소**: 전문지 4개, 경제지 3개는 필터 후 10건 미만으로 제외. 엘리트 일간지·통신사 중심 샘플임을 보고서에 명시.

---

*이 문서는 정본 워크플로우를 한 곳에 모으는 용도. 파이프라인 변경 시 반드시 이 문서도 갱신.*
