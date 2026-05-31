# 작업 파이프라인: 한·미·EU AI 입법·뉴스 비교 분석

> 이 문서는 본 프로젝트의 모든 데이터 흐름, 필터 기준, 분류 방법, 산출물을 단일 참고자료로 기록.
> 보고서 [report_expanded_draft.md](report_expanded_draft.md)와 함께 본 파이프라인이 정본.

> **2026-05-25 (current) — WSL 이주 + BERTopic 파이프라인 GPU 가속·매핑 영속화**
>
> 1. **환경**: Windows → WSL2 Linux 이주, `.venv` 전체 재설치 (cuml-cu13, pytest 등 추가) — [requirements.txt](requirements.txt)
> 2. **subtopic_bertopic.py 리팩터** ([analyze/subtopic_bertopic.py](analyze/subtopic_bertopic.py)):
>    - `--backend {auto,cuml,cpu}` CLI 옵션 — cuML 사용 시 UMAP+HDBSCAN GPU 가속 (KO 36k 클러스터링 5분 → 10초)
>    - Kiwi 병렬 토큰화 (`pretokenize_ko_texts`, `num_workers=8`) + 사용자 사전 19개 (KIWI_USER_WORDS) — 조사 결합형 제거
>    - KO 분기 `ngram_range=(1,2)`로 명사 bigram 추출 (`딥페이크 성범죄`, `자율주행 차량` 등)
>    - `'AI'`/`'A.I'` stop_words 추가 — 단순 prefix bigram noise 차단
>    - 임베딩 캐시 `data/analysis/bertopic_embeddings/*.npy` — 재실행 시 hit
>    - article→topic 매핑 → `assembly_analysis.duckdb::subtopic_assignments` 테이블 INSERT (run_timestamp로 버전 구분)
>    - lazy OpenAI client (`OPENAI_API_KEY` 없어도 `--no-label`로 실행 가능)
> 3. **신규 시각화**: [figures/temporal_top10.py](figures/temporal_top10.py) — 분기별 소주제 Top-10 랭킹 변동 (Plotly bump chart) + Lifespan Gantt (matplotlib)
>
> **2026-05-28 — 뉴스 DB 분리 + 2단계 cleaning 파이프라인 + cleaning_version 메타**
>
> 1. **DB 분리**: `news.duckdb` (raw 157k) + `news_analysis.duckdb` (Stage 1+2 적용 + classifications + cleaning_runs). 소비 스크립트는 `news_analysis.duckdb`의 `content` 컬럼(이미 Stage 1 적용)을 SELECT — SQL 인터폴레이션 사라짐.
> 2. **2단계 파이프라인** ([analyze/news_cleaning.py](analyze/news_cleaning.py) 통합 모듈):
>    - Stage 1 (B1·B2): YTN footer 라인 제거 + MBC `(AI학습 포함)` substring 제거 (본문 정화, 행 보존)
>    - Stage 2 (R1~R5): AI 키워드 매칭 + 영문 제외 + 조류 약자 제외 + 사이버대학·일반대 광고 제외 (행 단위)
>    - 룰 정의 + 빌드 IO + CLI가 한 모듈. `python analyze/news_cleaning.py [--force|--dry-run|--stage1-only]`
> 3. **버그 자연 해소**: 기존 inline `REPLACE`가 Rule B1(YTN footer)를 GPT 입력에 미적용하던 버그가 cleaned DB의 `content`가 미리 정화되어 자연 해소. 기존 81k 분류는 `cleaning_version='pre_b1_legacy'`로 추적.
>
> **2026-05-22 — 한국 도메스틱 뉴스 Strict 필터 + 10속성 분류 완료**
>
> 1. **Strict AI 필터 신설** ([analyze/news_cleaning.py](analyze/news_cleaning.py)):
>    - 공급사 substring 매칭 누수 + 매체별 boilerplate footer 정제 (4규칙). 2026-05-28에 Stage 1·2로 재구조화.
>    - 정의·근거 상세: [analyze/news_cleaning.md](analyze/news_cleaning.md)
>
> 2. **한국 도메스틱 10속성 분류** → 처음에는 `news.duckdb::news_classifications`. 2026-05-28에 `news_analysis.duckdb::news_classifications`로 이주.
>    - [analyze/classify_news_kr.py](analyze/classify_news_kr.py) (sync) / [analyze/classify_news_kr_batch.py](analyze/classify_news_kr_batch.py) (Batch API)
>
> **이전 변경 이력**
>
> - 2026-05-21 — data/ 폴더 도메인 분리 + KR 도메스틱 뉴스 정식 도입 (`news.duckdb`), Naver 파이프라인 폐기
> - 2026-05-10 — 스크립트 폴더 분리 (`collect/`, `analyze/`, `figures/`), DB raw/analysis 분리
> - 2026-04-18 — `bill_txt_*/*.json` → `bill_text`, `bills_classified_*.json` → `bill_classifications`, `kr_*_ai_filtered.json` → `bill_ai_filter`. 모든 per-age 테이블에 `age INTEGER` 주입.

본 문서의 파일경로 표현은 논리적 의미만 가지며 실제 위치는 DB. 자세한 출처는 [CODEBOOK.md](CODEBOOK.md)와 [`bill_loaders.py`](bill_loaders.py) 참고.

---

## 0. 프로젝트 개요

**연구 질문**: 한·미·EU의 AI 거버넌스를 입법 활동과 언론 담론 측면에서 정책 속성별로 어떻게 비교할 것인가?

**데이터 6종**:
1. 한국 국회 19~22대 법안
2. 미국 의회 118·119대 법안
3. EU AI Act 본문 + 수정안
4. Guardian (영국 뉴스)
5. NYT (미국 뉴스)
6. 한국 6개 매체 (KBS/MBC/SBS/YTN/중앙일보/한겨레, raw `news.duckdb` + 정화 `news_analysis.duckdb`)

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

### 2.1 한국 국회 법안 — [collect/download_bills.py](collect/download_bills.py) + [collect/collector.py](collect/collector.py)

1. 열린국회정보 Open API (`Assembly API`)로 19~22대 법안 메타데이터 수집
2. 국민참여입법센터·국회의안정보시스템 크롤링으로 제안이유·주요내용 텍스트 확보
3. 법안 PDF 파일시스템 보관: `data/bills_kr/pdf_archives/{19,20,21,22}/PRC_*.pdf` + 텍스트는 DB `bill_text` 테이블
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

- [collect/eu_01_collect_ai_act.py](collect/eu_01_collect_ai_act.py): EUR-Lex에서 AI Act 조문 116개 수집 → `data/bills_eu/eu_ai_act_articles.json`
- [collect/eu_02_collect_amendments.py](collect/eu_02_collect_amendments.py): European Parliament 제출 수정안 771건 수집 → `data/bills_eu/eu_amendments.json`

### 2.4 뉴스 소스

#### Guardian — [collect/collect_guardian.py](collect/collect_guardian.py)
- **API**: Guardian Content API
- **쿼리**: `"artificial intelligence"`, `"A.I."`, `AI`
- **기간**: 2016-03 ~ 2026-04 (11개 시간 슬롯)
- **섹션 필터** (14개): technology, business, politics, world, us-news, uk-news, australia-news, science, law, global-development, environment, society, commentisfree, education, media
- **출력**: `data/news/guardian_articles_raw.json` (11,120건)
- 필드: `id`, `title`, `trail_text`, `url`, `section`, `pub_date`, `word_count`, `query`

#### NYT — [collect/collect_nyt.py](collect/collect_nyt.py)
- **API**: NYT Archive API (월별 전체 기사 다운로드)
- **1차 필터**: 정규식 매칭 `artificial intelligence`, `A.I.`, `AI` (headline + abstract + snippet + lead_paragraph + keywords)
- **2차 필터 (desk 화이트리스트 12개)**: Business, Washington, Foreign, Science, Politics, National, SundayBusiness, OpEd, Climate, Investigative, Express, NYTNow. 빈 desk도 포함.
- **기간**: 2016-03 ~ 2026-04
- **캐시**: `data/news/nyt_archive/{year}_{month}.json`
- **출력**: `data/news/nyt_articles_raw.json` (3,108건)

#### 한국 도메스틱 6개 매체 — `data/news/news.duckdb` (raw) + `news_analysis.duckdb` (정화)
- **소스**: 외부 라이선스로 입수한 정식 아카이브 — KBS, MBC, SBS, YTN, 중앙일보, 한겨레
- **기간**: 2018-01 ~ 2026-05
- **규모**: raw 157,886건 → 2단계 정화 후 81,121건
- **원본 JSON**: `data/news/raw_news_archive/{매체}/{년}/{월}/{일}/*.json` (gitignored, 약 665 MB)
- **DB 적재**:
  - 1단계: [collect/build_news_db.py](collect/build_news_db.py) → `news.duckdb::news_articles` (raw, PK = `news_id`)
  - 2단계: [analyze/news_cleaning.py](analyze/news_cleaning.py) (build mode) → `news_analysis.duckdb::news_articles` (Stage 1+2 적용, content는 정화본)
- **스키마 필드**: `news_id`, `title`, `content`(전문), `dateline`, `published_at`, `enveloped_at`, `provider`, `byline`, `provider_link_page`, `category`(JSON 배열), `hilight`. analysis DB의 `content`는 Stage 1 sanitized.
- **2단계 정화 + 10속성 분류** → `news_analysis.duckdb::news_classifications` (PK `news_id × prompt_version`, 추가 컬럼 `cleaning_version`) — §3.5, §4.2 참조

---

## 3. AI 관련성 필터

### 3.1 한국 국회 법안 — 2단계 필터 (정본)

**스크립트**: [analyze/classify_bills.py](analyze/classify_bills.py) 내 `stage1_keyword_filter_kr()` + `stage2_gpt_filter_kr()`

#### Stage 1 — 키워드 후보 선별
- 패턴: `인공지능|AI|A\.I`
- 기준: 법안명 + 제안이유·주요내용 전문에 **3회 이상** 언급
- 중복 제거: `(법안명, 대표발의자)` 기준 — 최신 발의일만 유지

#### Stage 2 — GPT core/adjacent/unrelated 판별
- 모델: gpt-4.1-mini
- 프롬프트 ([analyze/classify_bills.py](analyze/classify_bills.py) 내 `AI_FILTER_PROMPT`):
  - **core**: AI가 법안의 주된 목적 (AI 기본법, AI 산업육성법, AI 책임법 등)
  - **adjacent**: AI가 핵심 trigger이거나 AI 관련 실질 조항 포함
  - **unrelated**: 배경 언급만. AI 없이도 법안 성립
- 핵심 판단: *"이 법안에서 AI 관련 내용을 삭제하면 법안의 존재 이유가 사라지는가?"*
- 최종 AI 법안 = **core + adjacent만** (unrelated 제거)

**캐시**: `bill_ai_filter` 테이블 (analysis DB). Stage 2 결과 전체 (classification·is_ai_bill·gpt_reason·ai_provisions 포함). 재실행 시 PROMPT_VERSION 매칭 행 자동 skip. 전면 재판정은 `analyze/classify_bills.py kr_22 --force` (해당 source의 현재 prompt_version 행 DELETE 후 재분류).

**왜 2단계인가**: 단순 키워드 카운트만 쓰면 "조류인플루엔자(AI) 예방법", "AI 시대에 대응하여..." 같은 주변적 언급 법안이 대거 포함되어 AI 법안 수가 부풀어짐. GPT 판별로 본질적 AI 법안만 남김.

### 3.2 미국 의회 법안
- **별도 필터 불필요**: Brennan Center for Justice가 선별한 AI 법안 리스트 (118대 154건)를 원본 기준으로 사용. 119대는 동일 기준 53건.
- `analyze/classify_bills.py`의 `load_us_bills()`는 10속성 분류만 수행.

### 3.3 EU AI Act
- **별도 필터 불필요**: 전체가 AI 규제 법안. 조문 116개 + 수정안 771건 모두 분류 대상.

### 3.4 영문 뉴스 (Guardian / NYT)
- **섹션/desk 필터**: Guardian 14개 섹션, NYT 12개 desk 화이트리스트 (수집 단계).
- **제목 키워드 필터** ([analyze/classify_articles.py](analyze/classify_articles.py) 내 `title_has_kw`): 제목에 `\bAI\b | artificial intelligence | A.I.` 중 하나 포함. Guardian/NYT 양쪽 동일 적용.

### 3.5 한국 도메스틱 뉴스 — 2단계 cleaning 파이프라인 (since 2026-05-28)

- **정의 + 빌드 + CLI**: [analyze/news_cleaning.py](analyze/news_cleaning.py) — 통합 모듈
  - **Public API**: `SANITIZE_CONTENT_SQL(content_expr)` (Stage 1 SQL 식 반환) + `RELEVANCE_WHERE(*, title_expr, sanitized_content_expr, raw_content_expr)` (Stage 2 WHERE 절 반환) + `RULES_APPLIED` 상수
  - **빌드 CLI**: `python analyze/news_cleaning.py [--force|--dry-run|--stage1-only]` — raw → `news_analysis.duckdb`
- **상세 규칙·근거**: [analyze/news_cleaning.md](analyze/news_cleaning.md)
- **두 단계**:
  - Stage 1 (B1·B2): 본문 정화. YTN footer 라인 제거, MBC `(AI학습 포함)` substring 제거. 행 보존.
  - Stage 2 (R1~R5): 행 단위 통과/탈락. AI 키워드, 영문 제외, 조류 약자, 사이버대학·일반대 광고 제외.
- **소비 스크립트는 import 불필요** — analysis DB의 `content` 컬럼(이미 Stage 1 적용)을 직접 SELECT: [analyze/classify_news_kr.py](analyze/classify_news_kr.py), [analyze/classify_news_kr_batch.py](analyze/classify_news_kr_batch.py), [analyze/subtopic_bertopic.py](analyze/subtopic_bertopic.py), [analyze/news_descriptive.py](analyze/news_descriptive.py)
- **메타 추적**: `news_analysis.duckdb::news_cleaning_runs`에 빌드별 (cleaning_version, rules_applied, sanitize/relevance hash, git SHA, raw/cleaned row count) 누적. 분류 결과의 `cleaning_version` FK로 "어느 룰 위에서 만들어졌는지" 추적.

---

## 4. 10속성 분류 — [analyze/classify_articles.py](analyze/classify_articles.py) / [analyze/classify_bills.py](analyze/classify_bills.py) / [analyze/classify_news_kr.py](analyze/classify_news_kr.py)

### 4.1 공통 사양
- **프롬프트**: [prompts.py](prompts.py)의 `SYSTEM_PROMPT` (영문 v2, 뉴스·법안 공용)
- **모델**: `gpt-4.1-mini`, `temperature=0`, `response_format=json_object`
- **출력 형식**: `{"primary": "<Label>", "secondary": "<Label or none>", "tertiary": "<Label or none>"}`
- **라벨 공간**: 10속성 영문 문자열 + `"none"`
- **병렬**: ThreadPoolExecutor + 429 rate-limit exponential backoff
- **캐시**: 각 출력 JSON 또는 DB 테이블에서 error 항목만 재시도, success는 재사용
- **출력 필드**: `primary`, `secondary`, `tertiary`, `news_id`/`article_id`/`bill_id`, `title`

### 4.2 뉴스 분류

#### Guardian / NYT
- 입력: 제목 + description(150자 스니펫)
- 출력: `data/analysis/articles_classified_{guardian,nyt}.json`

#### 한국 도메스틱
- 입력: `news_analysis.duckdb::news_articles` (Stage 1+2 적용본, 81,121건). 제목 + 본문(이미 Stage 1 정화본, cap 30,000자)
- 출력: `news_analysis.duckdb::news_classifications` (PK `news_id × prompt_version`, 추가 컬럼 `cleaning_version`)
- 스크립트:
  - [analyze/classify_news_kr.py](analyze/classify_news_kr.py) — sync (smoke test·소량용)
  - [analyze/classify_news_kr_batch.py](analyze/classify_news_kr_batch.py) — Batch API (대량 production, 50% 할인)
- 메타: `news_analysis.duckdb::news_prompt_versions` (prompt 버전) + `news_cleaning_runs` (cleaning 버전·hash·git SHA 누적)

### 4.3 법안 분류
- 입력: 법안명 + 제안이유 앞 2,000자 (KR) / 법안 원문 앞 3,000자 (US) / 조문 앞 2,500자 (EU)
- 출력: **DB 테이블** (`data/bills_kr/assembly_analysis.duckdb`의 `bill_classifications`, `prompt_versions`)
  - source 컬럼으로 구분: `kr_19`/`kr_20`/`kr_21`/`kr_22`/`us_118`/`us_119`/`eu_act`/`eu_amendments`
  - 옛 JSON (`bills_classified_*.json`)은 `data/_archive/`에 보존, 사용 안 함
  - 다운스트림은 [bill_loaders.py](bill_loaders.py) 경유 (`load_kr_bills`/`load_us_bills`/`load_eu_bills`)

---

## 5. 산출 스크립트

### 5.1 수집
| 스크립트 | 역할 |
|----------|------|
| [collect/download_bills.py](collect/download_bills.py) | 한국 법안 API 수집 orchestrator |
| [collect/collector.py](collect/collector.py) | 법안 크롤링 구현 |
| [collect/collect_guardian.py](collect/collect_guardian.py) | Guardian API 수집 |
| [collect/collect_nyt.py](collect/collect_nyt.py) | NYT Archive API 수집 |
| [collect/build_news_db.py](collect/build_news_db.py) | 한국 도메스틱 뉴스 JSON 아카이브 → raw `data/news/news.duckdb` 적재 |
| [collect/eu_01_collect_ai_act.py](collect/eu_01_collect_ai_act.py) | EU AI Act 조문 수집 |
| [collect/eu_02_collect_amendments.py](collect/eu_02_collect_amendments.py) | EU 수정안 수집 |
| [replicate_carvao/02_collect_bill_details.py](replicate_carvao/02_collect_bill_details.py) | US 118대 법안 상세 |
| [replicate_carvao/us119_run_all.py](replicate_carvao/us119_run_all.py) | US 119대 전체 파이프라인 |

### 5.2 필터·분류
| 스크립트 | 역할 |
|----------|------|
| [prompts.py](prompts.py) | 통일 10속성 분류 프롬프트 (v2 영문) |
| [analyze/news_cleaning.py](analyze/news_cleaning.py) | 한국 도메스틱 뉴스 2단계 cleaning 파이프라인 — 룰 정의 (`SANITIZE_CONTENT_SQL`, `RELEVANCE_WHERE`) + 빌드 IO + CLI. raw → `news_analysis.duckdb` 빌드 entry point. |
| [analyze/classify_articles.py](analyze/classify_articles.py) | 영문 뉴스 분류 (Guardian/NYT) |
| [analyze/classify_news_kr.py](analyze/classify_news_kr.py) | 한국 도메스틱 뉴스 분류 — sync |
| [analyze/classify_news_kr_batch.py](analyze/classify_news_kr_batch.py) | 한국 도메스틱 뉴스 분류 — Batch API (50% 할인) |
| [analyze/classify_bills.py](analyze/classify_bills.py) | 법안 분류 (KR 2단계 필터 포함 + US + EU) |

### 5.3 내보내기
| 스크립트 | 역할 |
|----------|------|
| [analyze/export_titles.py](analyze/export_titles.py) | 뉴스(Guardian/NYT) 제목 리스트 속성별 (매체/desk 소분류) |
| [analyze/export_bills.py](analyze/export_bills.py) | 한·미·EU 법안 속성별 리스트 + 소그룹 교차표 (kr/us/eu/all) |

### 5.4 분석·시각화
| 스크립트 | 역할 |
|----------|------|
| [analyze/make_figures.py](analyze/make_figures.py) | **현행 정본** 그림 일괄 생성 (fig01~fig06 + report41a/b/c + figures_data.xlsx). 구 `figures/regenerate_all.py`. 법안 그림은 `bill_loaders`, 뉴스 그림(report41*·fig04·fig05)은 `news_descriptive` 데이터 로더 경유. `news_descriptive.py`로 데이터를 갱신한 뒤 그림이 필요하면 이 스크립트를 재실행 |
| `figures/_legacy/generate_timeline*.py` | 옛 시계열 그림 (make_figures로 통합됨) |
| `figures/_legacy/generate_figures.py` | 옛 보고서 그림 (make_figures로 통합됨) |
| `figures/_legacy/build_treemap_*.py` | 옛 트리맵 데이터 |
| [analyze/subtopic_bertopic.py](analyze/subtopic_bertopic.py) | BERTopic 소주제 추출 — **언어별 독립 분류**(`--lang ko`, EN/KO cross-lingual 병합 제거), Kiwi 명사 토큰화, cuML GPU. 3단계 분리: ① 클러스터링(`--cluster-method eom --deterministic --no-label`) ② GPT 라벨링(`--label-only`, centroid 50제목+점수) ③ 토픽 그룹화(`--group-topics`, centroid average-linkage sim≥0.80, AI안전 제외). 매핑 → `subtopic_assignments` |
| [figures/temporal_top10.py](figures/temporal_top10.py) | 분기별 소주제 Top-10 랭킹 변동 — Bump chart(Plotly) + Lifespan Gantt(matplotlib) |

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
│ 한국 6매체 archive   │    │ 2단계 cleaning          │    │  classify_news_kr*.py│
│   → news.duckdb      │ →  │ (news_cleaning.py)     │ →  │  → news_classifications│
│   news_articles      │    │ Stage 1: 본문 정화     │    │  (news_analysis.duckdb)│
│   (raw 157k)         │    │ Stage 2: 행 필터링     │    │                      │
│                      │    │ → news_analysis.duckdb │    │                      │
│                      │    │   (81k articles)       │    │                      │
│                      │    │                        │    │                      │
│ KR Open API          │    │ Stage 1: 키워드 3회+   │    │                      │
│   → bill_text (DB)   │    │ Stage 2: GPT core/adj/ │    │                      │
│                      │    │ unrelated (unrelated 제외)│ │                      │
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
# 0. Open API 메타데이터 일괄 수집 (37 API → assembly_raw.duckdb)
python collect/download_all.py                              # 자동으로 validate_collection 호출

# 1. 한국 법안 본문 PDF 다운로드·추출 (raw.bill_text)
python collect/download_bills.py --age 22                   # 대수별

# 2. 회의록·연구단체보고서·여론조사 첨부 다운로드·추출 (raw.document_text)
python collect/download_documents.py --source minutes_committee
python collect/download_documents.py --source minutes_plenary
python collect/download_documents.py --source report
# (다른 source: minutes_subcommittee / minutes_committee_of_whole / research)

# 3. EU 수집
python collect/eu_01_collect_ai_act.py
python collect/eu_02_collect_amendments.py

# 4. 미국 수집
python replicate_carvao/02_collect_bill_details.py          # 118th
python replicate_carvao/us119_run_all.py                    # 119th

# 5. 뉴스 수집
python collect/collect_guardian.py                          # Guardian Content API
python collect/collect_nyt.py                               # NYT Archive API
python collect/build_news_db.py                             # 한국 6매체 archive → raw data/news/news.duckdb

# 5.5. 한국 도메스틱 뉴스 cleaning (raw → news_analysis.duckdb, Stage 1+2 적용)
python analyze/news_cleaning.py                             # 첫 빌드: 81,121건 + 기존 81k 분류 마이그레이션

# 6. 분류
python analyze/classify_articles.py all                     # Guardian + NYT
python analyze/classify_news_kr_batch.py full               # 한국 도메스틱 → news_analysis.duckdb (Batch API 권장)
python analyze/classify_bills.py all                        # 법안 6소스 (KR 2단계 포함)

# 7. 내보내기
python analyze/export_titles.py all                         # 뉴스 속성별
python analyze/export_bills.py all                          # 법안 속성별 (KR + US + EU)

# 8. 시각화 (정본) — news_descriptive.py로 데이터 갱신 후 실행
python analyze/news_descriptive.py --report                 # 뉴스 데이터/리포트/CSV (그림 생성 안 함)
python analyze/make_figures.py                              # fig01~fig06 + report41a/b/c + figures_data.xlsx

# 9. BERTopic 소주제 추출 — KO 단독, 3단계 분리 (클러스터링/라벨링/그룹화 독립)
#    ① 클러스터링: deterministic(재현 가능) + 키워드 임시 라벨 + 대표문서 보존 + DB write
python analyze/subtopic_bertopic.py --lang ko --cluster-method eom --deterministic --no-label
#    ② GPT 라벨링: centroid 최근접 50제목+점수 → 토픽 라벨, 묶음그룹은 하위 라벨 기반 상위 라벨
python analyze/subtopic_bertopic.py --label-only
#    ③ 토픽 그룹화: centroid average-linkage sim≥0.80 으로 group_id 부여 (AI안전 제외)
python analyze/subtopic_bertopic.py --group-topics --group-threshold 0.80
# → output/analysis/subtopics_bertopic.json (토픽 라벨 + group_id + group_label)
# → news_analysis.duckdb::subtopic_assignments (article→topic, lang='ko')
# 비결정성 주의: ②③ 는 ① 의 클러스터/대표문서를 재사용 — ① 재실행 없이 재라벨/재그룹 가능.
#   단 ① 를 다시 돌리면 토픽 구성이 바뀌므로 ②③ 도 다시 실행해야 함 (--deterministic 이라 동일 입력→동일 결과).
# 영문(US/UK) subtopic 은 기사량이 적어 별도 진행 (--lang en) — 현재는 KO 만 정본.

# 기사 제목 리스트(속성>그룹>토픽) 산출물:
python working/export_ko_lists.py    # → output/article_lists_ko.md

python figures/temporal_top10.py
# → figures/out/temporal_top10_bump.html (인터랙티브)
# → figures/out/temporal_top10_lifespan.png
```

### 재실행 (캐시 활용)
- 모든 `classify*.py`는 출력 JSON·DB 행 존재 시 **error 항목만 재시도**, 성공 항목은 재사용
- Stage 2 결과는 `bill_ai_filter` 테이블에 캐시 — `PROMPT_VERSION` 매칭 시 재사용으로 GPT 필터 비용 절감
- `build_news_db.py`는 PK(`news_id`) `INSERT OR IGNORE`로 idempotent — 재실행 시 신규 행만 적재
- `classify_news_kr*.py`는 `news_analysis.duckdb::news_classifications` PK(`news_id × prompt_version`) 매칭 시 skip
- `news_cleaning.py` 빌드는 전체 재빌드(DROP+CREATE) — 룰 변경 시 새 cleaning_version으로 메타 누적. classifications는 보존(`--force`면 raw에서 다시 복사)

---

## 8. 주요 출력 파일 체크리스트

### 폴더 정책 (2026-05-27 정립, 2026-05-28 강화)

| 폴더 | 용도 | git |
|---|---|---|
| `data/` | **정본 원본** (수집 JSON, DuckDB, PDF 아카이브) | gitignored |
| `output/` | **정본 파이프라인(`analyze/`·`collect/`·`figures/`) 산출물 전용** — 다운스트림 보고서·논문에 인용되는 결과만 | gitignored |
| `working/` | **시험 스크립트(`working/*.py`) + 그 산출물** — calibration·진단·누수 분석·임시 평가 markdown/JSON/npy 모두 | gitignored |
| `.cache/` | **재생성 가능한 캐시** (BERTopic SBERT 임베딩 등) — 숨김 폴더 | gitignored |

config 상수 (`config.py`):
- 정본 output: `ANALYSIS_DIR`, `EXPORTS_DIR`, `FIGURES_OUT_DIR` (모두 `output/` 하위)
- 시험 working: `WORKING_DIR`, `STABILITY_DIR` (모두 `working/` 하위)
- 캐시: `BERTOPIC_EMBED_CACHE`, `CACHE_DIR` (`.cache/` 하위)

**판정 기준**: 결과를 보고서/논문에 인용·재사용할 가능성 있나? Y → output, N → working. **하드코드 path 금지** — 항상 config 상수 사용.

### 데이터베이스 (정본, data/)
- [ ] `data/bills_kr/assembly_raw.duckdb` — 37 API + bill_text + document_text + speeches
- [ ] `data/bills_kr/assembly_analysis.duckdb` — bill_classifications + bill_ai_filter + speech_issues + **`subtopic_assignments`** (BERTopic article→topic 매핑, run_timestamp별) + 분석 뷰
- [ ] `data/bills_us/congress.duckdb` — US 118·119 Congress API 수집물
- [ ] `data/news/news.duckdb` — raw `news_articles` (157,886)
- [ ] `data/news/news_analysis.duckdb` — `news_articles` (Stage 1+2 적용본, 81,121) + **`news_classifications`** (+ `cleaning_version` 컬럼) + `news_prompt_versions` + **`news_cleaning_runs`** (빌드 메타 누적)

### 원본 수집 (JSON, 일부는 DB로 흡수됨)
- [ ] `data/news/guardian_articles_raw.json`
- [ ] `data/news/nyt_articles_raw.json`
- [ ] `data/news/raw_news_archive/{매체}/{년}/{월}/{일}/*.json` (gitignored)
- [ ] `data/bills_eu/eu_ai_act_articles.json`
- [ ] `data/bills_eu/eu_amendments.json`
- [ ] `replicate_carvao/data/bills_processed.json` (US 118)
- [ ] `replicate_carvao/data/us119_bills_processed.json` (US 119)

### 10속성 분류 결과 (output/analysis/)
- [ ] `output/analysis/articles_classified_guardian.json` — 영문 뉴스
- [ ] `output/analysis/articles_classified_nyt.json`
- [ ] **DB**: `news_analysis.duckdb::news_classifications` — 한국 도메스틱 뉴스 (PK `news_id × prompt_version`, + `cleaning_version` 컬럼)
- [ ] **DB**: `assembly_analysis.bill_classifications` — 법안 6소스

### 사람 열람용 마크다운 (output/exports/)
- [ ] `output/exports/titles_{guardian,nyt}_by_category.md`
- [ ] `output/exports/bills_kr_{19,20,21,22}_by_category.md`
- [ ] `output/exports/bills_kr_all_by_category.md`

### BERTopic 정본 산출물
- [ ] `output/analysis/subtopics_bertopic.json` — 토픽 라벨·키워드·통계 (10개 attr × ~30 토픽)
- [ ] `assembly_analysis.duckdb::subtopic_assignments` — article→topic 매핑 (run_timestamp별 누적)
- [ ] `output/figures/temporal_top10_bump.html` — 분기별 Top-10 랭킹 변동 인터랙티브 차트
- [ ] `output/figures/temporal_top10_lifespan.png` — 토픽별 Top-10 머문 기간 Gantt
- [ ] `.cache/bertopic_embeddings/{attr}_{lang}_{hash}.npy` — BGE-M3 임베딩 캐시 (재생성 가능)

### BERTopic 시험·진단 산출물 (working/, gitignored)
- [ ] `working/stability/{attr}_{runs,cocluster,consistency_*}.npy/.json` — 100 runs 안정성 매트릭스 (10개 attr)
- [ ] `working/mcs_calibration.json`, `mcs_stability.json`, `calibration_k3_tau_sweep.json` — calibration 결과
- [ ] `working/finding_cluster_stability.md`, `calibration_history.md`, `calibration_k3_results.md` — 분석 발견 문서
- [ ] `working/leak_*.md`, `topic_*.md`, `none_classified_titles.md`, `subtopics_all.md` — 누수·진단 평가 자료

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

### 9.3 한국 도메스틱 뉴스 DB 적재 + raw/analysis 분리
- **What**: 6개 매체 9년치 157K JSON을 매체별 폴더 × 일별 트리에 두지 않고 `news.duckdb`의 단일 `news_articles` 테이블로 묶음 (2026-05-21). 그 후 2026-05-28에 raw `news.duckdb` + 정화 `news_analysis.duckdb`로 분리.
- **Why**: 16만 파일을 그대로 두면 파이프라인 입력 단계마다 디렉토리 walk · JSON 파싱 비용이 폭발. DB 한 곳에서 인덱싱(`provider`, `published_at`) · SQL 필터 가능. PK가 `news_id`라 `INSERT OR IGNORE`로 idempotent. raw/analysis 분리는 Assembly와 동일 어휘(`assembly_raw`/`assembly_analysis`)에 평행. 소비 스크립트는 `news_analysis.duckdb`의 `content`(Stage 1 적용본)를 직접 SELECT — SQL 인터폴레이션 사라짐. classify_news_kr.py의 inline `REPLACE` Rule B1 누락 버그도 자연 해소.

### 9.4 뉴스 제목 키워드 필터 최종 적용
- **What**: 수집·분류가 끝난 뒤에도 "제목에 AI 키워드" 조건으로 1차 scope 좁혀 비교 정본 수집물 확정 (Guardian/NYT)
- **Why**: 동일 조건을 세 소스에 적용해야 비교 공정성 유지. 본문만 AI 언급하고 제목은 다른 주제인 기사는 국가 간 담론 비교에서 편향 유발.

---

## 10. 작업 이력 주요 분기

### 2026-05-31 — 그림 생성기 재정비 + 보고서 §4.1/§4.4 갱신
- `news_descriptive.py` 재작성(76,645건 전량 분류본 기준)에서 누락됐던 그림 생성기를 복구·이동: `figures/regenerate_all.py` → `analyze/make_figures.py` (`import _bootstrap`, WSL 한글 폰트 자동탐색).
- `news_descriptive.py`는 그림 생성 책임 제거 — 데이터 로더 + 마크다운 리포트 + 출판사 CSV 전담. 그림은 make_figures가 정본.
- 폐기된 raw/strict 로더 의존 그림 블록 삭제. §4.1 그림(report41a/b/c)을 현 cleaned subset(76,645)으로 재구축, fig04를 3소스(국내·Guardian·NYT)로 확장.
- 국내 뉴스 재분류 완료로 `fig05_discourse_legislation_gap`(KR22 입법 vs 국내 뉴스) 활성화, fig06에 한국 페어 추가(3-region).
- 보고서 §4.1/§4.4/§3.x 뉴스 수치를 현 데이터로 전면 갱신(예: 산업정책 51.1→56.9%, ±24개월 +114.1→+122.4%).

### 2026-05-27 — 폴더 정책 정리: data / output / cache 분리
- `data/` 는 정본 원본·DB 만 (bills_kr/us/eu, news, _archive, _audit)
- 새 `output/` 폴더 — 파이프라인 산출물 (analysis JSON, exports markdown, figures, stability 매트릭스)
- 새 `.cache/` 폴더 (숨김) — 재생성 가능한 캐시 (bertopic_embeddings/), `.gitignore` 등재
- config.py 경로 상수 갱신: `ANALYSIS_DIR/EXPORTS_DIR/STABILITY_DIR/FIGURES_OUT_DIR` → `output/`, `BERTOPIC_EMBED_CACHE` → `.cache/`
- 모든 스크립트(analyze/, figures/, working/)에서 hardcode path 제거, config 상수 사용
- 시험적·일회성 분석 스크립트는 `working/` 폴더 안에 작성·실행 ([feedback_test_scripts_location](.claude/projects/-home-jays0967-assembly-data/memory/feedback_test_scripts_location.md) 메모리 참조)

### 2026-05-25 — WSL 이주 + BERTopic 파이프라인 GPU·매핑 영속화
- Windows → WSL2 Linux 환경 이주, `.venv` 전체 재구축 ([requirements.txt](requirements.txt))
- cuml-cu13(RAPIDS) GPU UMAP+HDBSCAN 도입 — KO 36k 클러스터링 5분 → 10초 (~30배)
- `subtopic_bertopic.py` 리팩터: `--backend {auto,cuml,cpu}` CLI, Kiwi 병렬(`num_workers=8`) + `pretokenize_ko_texts` 정상화 (이전 결과 1,615건 조사 결합형 → 0건), KO `ngram_range=(1,2)`, `'AI'` stopword, 임베딩 캐시(`.cache/bertopic_embeddings/`), lazy OpenAI 클라이언트
- 신규 테이블 `assembly_analysis.duckdb::subtopic_assignments` — article→topic 매핑 (run_timestamp 버전 구분)
- 신규 시각화 [figures/temporal_top10.py](figures/temporal_top10.py) — 분기별 Top-10 Bump chart + Lifespan Gantt
- 진단 자료: [BERTOPIC_KIWI_HANDOFF.md](BERTOPIC_KIWI_HANDOFF.md) (Windows hang 진단 기록 — WSL에서 해결됨)

### 2026-05-28 — 뉴스 DB 분리 + 2단계 cleaning 파이프라인
- raw `news.duckdb` + 정화 `news_analysis.duckdb` 분리 (Assembly 패턴과 평행)
- 룰 정의 + 빌드 IO + CLI를 [analyze/news_cleaning.py](analyze/news_cleaning.py) 한 모듈에 통합
- Stage 1 (B1·B2 본문 정화, 행 보존) + Stage 2 (R1~R5 행 필터) 명시 분리
- 소비 스크립트 4개 단순화 — SQL 인터폴레이션(`STRICT_WHERE`/`CLEANED_CONTENT_SQL`) 제거, `news_analysis.duckdb`의 `content` 컬럼 직접 SELECT
- 빌드 메타 누적: `news_cleaning_runs` 테이블 (cleaning_version, sanitize/relevance hash, git SHA)
- 분류 결과에 `cleaning_version` FK 추가 — 어느 룰 위에서 만들어진 분류인지 추적
- classify_news_kr.py의 inline `REPLACE`가 Rule B1(YTN footer)을 누락하던 버그 자연 해소
- 기존 81k 분류는 `cleaning_version='pre_b1_legacy'`로 마킹

### 2026-05-22 — 한국 도메스틱 뉴스 Strict 필터 + 10속성 분류
- 공급사 substring 매칭 누수·매체별 boilerplate footer 정제 위한 Strict 필터 4규칙 설계 (2026-05-28에 Stage 1·2로 재구조화)
- `news_cleaning.py` 모듈 분리, 문서 `news_cleaning.md` 신설
- 81,888건 GPT 10속성 분류 → `news.duckdb::news_classifications` 테이블 신설 (2026-05-28에 `news_analysis.duckdb`로 이주)
- 분류 비용 추정 휴리스틱 정립 (Batch API 권장 패턴 확립)

### 2026-05-21 — data/ 재구성 + 한국 신문기사 데이터셋 교체
- data/ 폴더 도메인별 분리 (bills_kr/us/eu, news, analysis, exports)
- Naver Search API 파이프라인 폐기
- 6개 매체 KBS/MBC/SBS/YTN/중앙/한겨레, 2018~2026 약 157K 기사 → `news.duckdb`

### 2026-05-10 — RAG 시스템 구축 (rag_assembly/)
- LanceDB float16 기반 의미 검색 인프라 (회의록·법안·발언·의원 임베딩)
- Vertex AI gemini-embedding-001 (8 region multi-region rotation, 1M TPM × 8)
- duckdb_mcp_server.py에 `rag_search`, `rag_search_bills/speeches/documents`, `rag_stats` 툴 추가

### 2026-05-10 — 분석 스크립트 폴더 분리
- root 분산 → `analyze/` (분류·내보내기·subtopic·compare_models)
- 옛 viz 7개 → `figures/_legacy/` (regenerate_all.py가 정본 — 이후 2026-05-31 `analyze/make_figures.py`로 이동·개명)
- root 4개 인프라만 유지: config, prompts, bill_loaders, duckdb_mcp_server

### 2026-05-09 — DB 분리 (raw / analysis)
- 단일 `data/assembly.duckdb` → `assembly_raw.duckdb` + `assembly_analysis.duckdb`
- raw: 37 API + bill_text + document_text + speeches + 9 wrapper view
- analysis: bill_classifications + bill_ai_filter + prompt_versions + speech_issues + 분석 통합 뷰
- 양쪽 동시 access는 ATTACH read-only 패턴

### 2026-05-08 — 수집 스크립트 collect/ 폴더 분리

### 2026-04-18 — 프롬프트 통일·법안 필터 재구축 + 어댑터 제거
- 모든 구 버전 v1 프롬프트 폐기, 통일 영문 v2 프롬프트 단일화
- `prompts.py` 공통 모듈로 분리
- `analyze/classify_articles.py` (뉴스) / `analyze/classify_bills.py` (법안) 2개 파일로 통합
- 한국 법안 2단계 GPT 필터를 `analyze/classify_bills.py`에 내장
- 어댑터 제거: `bill_loaders.py` 신설로 `bills_classified_*.json`을 직접 로드

### 2026-04-15 — 제목 키워드 필터 확정 (영문 뉴스)

---

*이 문서는 정본 워크플로우를 한 곳에 모으는 용도. 파이프라인 변경 시 반드시 이 문서도 갱신.*
