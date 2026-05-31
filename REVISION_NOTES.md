# REVISION_NOTES — Version History

이 문서는 한·미·EU AI 정책 비교 분석 파이프라인의 **버전별 변경 이력**이다. 각 버전은 정본
데이터·파이프라인·산출물의 한 스냅샷에 대응한다. 상세 실험·중간 산출물은 `working/`(gitignored)·
`output/`·`data/` DB에 분산하며, 본 문서는 그 인덱스 + 발견 요약이다.

**버저닝**: 정본 파이프라인/데이터셋 기준 semantic-versioning 유사. **MAJOR** = 데이터 정제·분류
스키마·파이프라인 구조의 큰 변경 (코드 리팩터·문서·산출물 재생성은 해당 MAJOR에 흡수). 최신 버전이 맨 위.

## 버전 인덱스

| 버전 | 일자 | 유형 | 한 줄 요약 |
|---|---|---|---|
| **v2.00** | 2026-05-28 ~ 05-31 | MAJOR | news raw/analysis DB 분리 + 3단계 정화 재작성 + KR 도메스틱 뉴스 76,645건 전량 GPT 분류 |
| **v1.00** | 2026-05-25 ~ 05-27 | MAJOR (baseline) | WSL 이주 + BERTopic GPU 가속 + 다중 라벨 가설 검증 + 데이터 누수 발견 |

> 버전 간 상호참조는 v1.00의 절 번호(§1~§10)를 그대로 사용한다 (v2.00 본문에서 `§6.2`, `§9.B` 등으로 인용).

---

# v2.00 — news 정화 재작성 + KR 뉴스 전량 GPT 분류 (MAJOR)

**일자**: 2026-05-28 ~ 05-31 · **baseline**: commit `8e08335`(v1.00) · **상태**: working tree 미커밋(+2,212 / −1,496, 18파일)

v1.00에서 발견한 누수(§6)와 정한 다음 단계(§9)를 실제 파이프라인에 반영한 메이저 개정. 두 축:
**(가) 데이터 인프라** — news raw/analysis DB 분리 + 2→3단계 정화 재작성, **(나) 분류** — KR 도메스틱
뉴스 전량을 Carvão 10속성으로 GPT 분류.

## v2.00 개요

| 영역 | 핵심 |
|---|---|
| 데이터 인프라 | news raw/analysis **DB 분리**, 2→**3단계 정화** 재작성 (B1·B2 / R1–R6 / D1), `news_cleaning_runs` 추적 |
| 분류 | KR 도메스틱 뉴스 **76,645건 전량** 10속성 GPT 분류 (Batch −50%, 100%·에러 0·중복 0) |
| 정합성 | 이질(off-taxonomy) 라벨 **제거 규칙 확정**, 4건 보정 |
| 산출물 | `news_descriptive.py` 재작성 → `output/news_analysis.md` + figure 4종 |
| 폐기 | 레거시 `subtopic_*` 3종 + `compare_models.py` + `BERTOPIC_KIWI_HANDOFF.md` |
| 보류 | AI 작성고지 boilerplate 583건 + 프롬프트 회귀 누수 — 미실행(§v2-H) |

---

## v2-A. news 정화 파이프라인 재작성 — analyze/news_cleaning.py (+821)

- monolithic `STRICT_WHERE`/`CLEANED_CONTENT_SQL` export → **파라미터화 함수**로 전환:
  `SANITIZE_CONTENT_SQL(content_expr)`(Stage 1), `RELEVANCE_WHERE(...)`(Stage 2),
  `DEDUP_DELETE_SQL(table)`(Stage 3), `RULES_APPLIED` 상수.
- 룰을 **3단계로 재명명**: Stage 1 Boilerplate(B1 YTN footer, B2 MBC `(AI학습 포함)`),
  Stage 2 Relevance(R1 키워드, R2 영문본문, R3 조류독감 충돌, R4 사이버대 광고, R5 일반대 모집 footer)
  + **신규 R6**(KBS `[사진기사]` 플레이스홀더 33건 제외) + **신규 Stage 3 D1**(중복 제거:
  `(provider, MD5(content_no_ws))` 그룹을 published_at→byline→link→news_id 우선순위로 1건만 보존).
- **완전한 build IO/CLI 추가**: `build()`가 raw `news.duckdb` → `news_analysis.duckdb::news_articles`
  (Stage1 정화본 + Stage2 필터 + Stage3 dedup)를 트랜잭션으로 기록. auto-backup/restore, raw-lock
  precheck, `EXPECTED_TOTAL=76,645`/`EXPECTED_BY_PROVIDER` 검증, orphan classification 정리.
  신규 DDL `news_cleaning_runs`(cleaning_version PK, rules_applied, row counts, sanitize/relevance
  hash, git SHA). CLI `--dry-run`, `--stage1-only`. **순수 결과: raw 157,886 → 76,645** (D1 dedup으로
  v1.00 시점 81,121에서 추가 감소).

## v2-B. news_descriptive.py 전면 재작성 (+1152)

- 소스 DB `NEWS_DB_PATH` → `NEWS_ANALYSIS_DB_PATH`. numpy 제거(행렬을 plain list로), `news_cleaning`
  re-export 제거.
- 분류 인지 로더 신설(현재 버전 = prompt_version×cleaning_version 최신 `classified_at` 기준):
  `load_classification_coverage`, `load_monthly_total_ma`, `load_event_window`(AI 기본법 ±24개월,
  부분 윈도우 처리), `load_attr_distribution`, `load_attr_by_provider`, `load_attr_by_year`(none 제외 share).
  `load_subtopic_stats`는 BERTopic placeholder.
- matplotlib figure 4종(`fig_decade_trend`, `fig_event_window`, `fig_provider_attr`,
  `fig_attr_year_trend`) + 한글폰트(`config.KO_FONT_PATH`) + 출판사 인계용 CSV(utf-8-sig + README) +
  마크다운 리포트(`build_report` → `output/news_analysis.md`). CLI `--figures/--sources/--report/--all`.

## v2-C. 분류기 DB 분리 — classify_news_kr.py(+90) / classify_news_kr_batch.py(+85)

- 둘 다 `NEWS_DB_PATH` → `NEWS_ANALYSIS_DB_PATH` 읽기·쓰기. `news_cleaning.STRICT_WHERE` import와
  인라인 `strict_pass` CTE 제거(content가 사전 정화돼 **B1-omission 버그 소멸**).
- 둘 다 `news_classifications`/`news_prompt_versions` DDL 소유, **`cleaning_version` 컬럼 추가**,
  `news_cleaning_runs`에서 활성 cleaning_version 해석(없으면 hard-fail) 후 매 행에 stamp.
- CLI 예시 de-Windows화(`venv/Scripts/python.exe` → `python`).

## v2-D. MCP·config·기타 코드

- **duckdb_mcp_server.py**(+71): read-only DB **3개 ATTACH**(`raw`, `news_analysis`, `news_raw`),
  idempotent(BinderException swallow), `_CATALOGS` 화이트리스트 기반 list/describe. `NEWS_RAW_DB_PATH`/
  `NEWS_ANALYSIS_DB_PATH` 상수.
- **config.py**(+25): `WORKING_DIR` 신설, `STABILITY_DIR`을 output/ → working/ 이동. 신규
  `OUTPUT_DIR`, `FIGURES_SOURCE_DIR`, `KO_FONT_PATH`, `NEWS_ANALYSIS_DB_PATH`, `NEWS_RAW_DB_PATH`.
- **subtopic_bertopic.py**(+18): KR 뉴스를 `NEWS_ANALYSIS_DB_PATH`의 사전 정화 `n.content`에서 로드
  (STRICT_WHERE 제거), `subtopic_assignments`를 `NEWS_ANALYSIS_DB_PATH`에 기록, 임베딩 1024-dim(BGE-M3) 주석.
- **figures/temporal_top10.py**(+6): `subtopic_assignments`를 `NEWS_ANALYSIS_DB_PATH`에서 읽기.

## v2-E. 폐기된 파일 (삭제)

| 파일 | 사유 |
|---|---|
| `BERTOPIC_KIWI_HANDOFF.md` (−251) | Windows hang 진단 핸드오프 — WSL 이주로 해소돼 obsolete |
| `analyze/compare_models.py` (−147) | gpt-4.1-mini vs 4.1 비교 유틸 — ad-hoc은 working/ 정책으로 폐기 |
| `analyze/subtopic_discover.py` (−204) | 구 LLM-direct 서브토픽 발견 — subtopic_bertopic.py로 대체 |
| `analyze/subtopic_finalize.py` (−110) | 구 LLM merge 단계 — 동상 |
| `analyze/subtopic_overlap.py` (−124) | 구 seed overlap/stability — 동상 |

## v2-F. 문서 갱신

- **CLAUDE.md**(+35) / **WORKFLOW.md**(+125) / **analyze/news_cleaning.md**(+289): DB 분리,
  3단계 정화(B1/B2·R1-R6·D1), `news_cleaning_runs`/`cleaning_version` 추적, working/ 폴더 정책,
  매체별 drop 통계(YTN −1,129, KBS −3,181 등), 누적 영향(94K → 76.6K, 51.5%) 반영. 폐기 스크립트
  3종 + compare_models 행 테이블에서 제거.

---

## v2-G. KR 도메스틱 뉴스 전량 GPT 분류 실행 (2026-05-30 ~ 05-31)

§9.B에서 후보로 적었던 "GPT prompt 누수 차단"을 **부분 실행**한 상태에서 KR 도메스틱 뉴스
전량(76,645건)을 10속성으로 분류했다.

### G.1 프롬프트 변경 (prompts.py) — ⚠️ 회귀 주의

[prompts.py::SYSTEM_PROMPT](prompts.py)의 "none" 규칙 한 줄이 **교체**됨 ([prompts.py:117](prompts.py#L117)):

```diff
- AI mentioned only as a tool; article's real subject is elsewhere (e.g., medical study, entertainment review)
+ Avian influenza or other wildlife related topics without any connection to artificial intelligence
```

- **의도**: §6.2 패턴 A(조류인플루엔자 약어 false positive) 명시 차단.
- **부작용**: §6.6/§9.B에서 "약하다"고 지목했던 *"AI mentioned only as a tool → none"* 규칙을
  **추가가 아니라 교체로 잃어버림**. 조류독감은 잡지만, AI가 곁가지로만 언급된 기사(패턴 B·C)를
  none으로 보내던 일반 규칙이 사라짐 → G.4의 회귀 누수 발생.
- prompt_version은 `v2_en_20260418` 그대로 유지(기존 분류 0건이라 충돌 없음). 단 이 버전 문자열이
  가리키는 프롬프트 내용이 바뀐 셈이므로, 추후 줄 복원 시 **v3로 bump 필요**.

### G.2 Batch 순차 제출 인프라 (working/run_batch_sequential.py, 신규)

- **문제**: OpenAI 조직의 `gpt-4.1-mini` **enqueued-token 한도 40M**. 정본 스크립트
  ([analyze/classify_news_kr_batch.py](analyze/classify_news_kr_batch.py))는 todo 전량을 6청크로
  한꺼번에 제출 → 1청크(≈39M 토큰)만 들어가고 나머지 5청크 `token_limit_exceeded` 즉시 실패.
- **해결**: working/run_batch_sequential.py — 기존 모듈 함수(`fetch_todo`/`chunk_lines`/
  `insert_rows`/`cmd_collect`/state I/O) 재사용, **한 청크 제출 → 완료 대기 → collect → 다음 청크**
  순차 드라이버. state 파일은 동일(`data/news/batch_state.json`)이라 정본 `status`/`collect`도 호환.
- **겹침 방지 2중 보장**: (a) `fetch_todo`의 `LEFT JOIN ... WHERE c.news_id IS NULL`이 collect된
  행을 자동 배제 + 순차 구조(이전 청크 collect 후 다음 fetch), (b) PK `(news_id, prompt_version)` +
  `INSERT OR REPLACE`로 중복 행 원천 불가. 실측: 76,645 rows = distinct news_id, 중복 0.

### G.3 분류 실행 결과

| 지표 | 값 |
|---|---|
| 분류 완료 | **76,645 / 76,645 (100%)**, 에러 0, 중복 0 |
| prompt_version / cleaning_version | `v2_en_20260418` / `2026-05-30_076a7377_2611088d_5514be91` |
| 청크 | 6개 순차 (13,176 / 13,618 / 13,215 / 15,379 / 12,760 / 8,497) |
| 비용 | Batch −50% 적용, 실측 **$0.000556/req**, 총 **~$42.6** (동기 대비 절반) |

**최종 primary_attr 분포** (미분류 제외 share):

| 속성 | 건수 | share |
|---|---:|---:|
| 산업정책 (Industrial policy) | 34,893 | 56.9% |
| 공익 (Public interest) | 8,898 | 14.5% |
| 안보 (National security) | 4,726 | 7.7% |
| 노동 (Labor) | 3,358 | 5.5% |
| 책임과 윤리 (Responsible and ethical AI) | 2,807 | 4.6% |
| 시장경쟁 (Market efficiency/antitrust) | 2,112 | 3.4% |
| 국제협력 (International collaboration) | 1,765 | 2.9% |
| 선거 (Elections) | 1,132 | 1.8% |
| 안전성 (Safety) | 985 | 1.6% |
| 저작권 (Copyright) | 608 | 1.0% |
| 미분류 (none) | 15,361 | — |

### G.4 QA 발견 — 누수 3종 + 이질 라벨

| 발견 | 정량 | 처리 |
|---|---|---|
| **이질(off-taxonomy) 라벨** | 4건: primary 2 (`Human rights and ethical AI`, `Regulation and legal compliance`), secondary `Health` 2 | ✅ **보정 실행** (G.5) |
| **프롬프트 회귀 누수** (패턴 B·C 재발) | AI 곁가지 기사가 substantive 속성으로. 예: "2030 대선 표심 분석"(→Elections), "국힘 신천지 전당대회"(→Elections). 원인 = G.1의 규칙 상실 | ❌ 미실행 (§v2-H) |
| **AI 작성고지 boilerplate 누수** | 중앙일보 *"이 기사는 …생성형 AI…AI 시스템의 도움을 받아 작성했습니다"* 고지가 정화 R1 통과시킴. 정화본 **604건**(전체 0.79%, 중앙일보의 1.9%), AI키워드 ≤3회 진짜 누수 **583건** | ❌ 미실행 (§v2-H) |

- AI 작성고지는 v2-A 정화 Rule B2(MBC `(AI학습 포함)`)와 동종의 boilerplate 문제. MBC `[인공지능 번역]`
  3건은 실제 AI 기사라 누수 아님.
- 레퍼런스: [working/ai_authoring_disclaimer_types.md](working/ai_authoring_disclaimer_types.md),
  [working/boilerplate_articles_titles.md](working/boilerplate_articles_titles.md)(604건 제목+플래그).

### G.5 적용한 보정 (이질 라벨만) — 규칙 확정

**규칙**: 표준 10속성 밖 라벨은 **자의적 재배정 없이 제거**. (대륙아주 "AI 법률서비스 징계" 기사처럼
내용이 진짜 AI 정책이어도, off-taxonomy 라벨을 임의 매핑하지 않고 일관 제거.)

`news_analysis.duckdb::news_classifications`에 직접 UPDATE:
- primary 이질 2건 → `primary='none'`, secondary/tertiary `NULL` (none = 비-AI정책이므로 하위 라벨도 정리)
- secondary `Health` 2건 → `NULL` (primary 유효 → 기사 보존)
- 결과: 이질 라벨 0건 잔존, none 15,359 → 15,361.

### G.6 산출물

- [analyze/news_descriptive.py](analyze/news_descriptive.py) `--summary --all` 재실행 →
  [output/news_analysis.md](output/news_analysis.md) + figure 4종(`news_decade_trend`,
  `news_event_window`, `news_provider_attr`, `news_attr_year_trend`) + source CSV/README.
- AI 기본법 ±24개월: 월평균 911 → 2,026 (+122.4%, 부분 윈도우).
- 속성별 전체 기사 리스트: [working/kr_news_by_attr_FINAL.md](working/kr_news_by_attr_FINAL.md) (76,645건).

---

## v2-H. 미실행 후처리 (의도적 보류 — 2026-05-30 결정)

사용자 결정으로 아래 두 항목은 **v2.00에서 실행하지 않음**. 데이터셋 영향이 각각 ~1% 미만이고,
나중에 후처리/재분류 묶음(차기 버전)으로 일괄 처리 가능하도록 술어·레퍼런스만 남긴다.

### ① AI 작성고지 boilerplate 제거 — *깔끔한 술어 있음, 미실행*

- 대상: 중앙일보 AI 작성 고지 기사. 매칭 술어 `content LIKE '%AI 시스템의 도움을 받아 작성%'` → 604건.
- 권장 범위: **보수적**(+ AI키워드 ≤3회 = 583건, 실제 AI 기사 ~21건 보존). 전량(604) 옵션도 가능.
- 미결정: 분류행만 삭제 vs 기사행까지 제거.
- 근본 수정 대안: [analyze/news_cleaning.py](analyze/news_cleaning.py)에 **Stage 1 Rule B3**(고지 문구
  제거)를 추가해 재빌드하면, R1에서 떨어져 데이터셋에서 자동 배제됨(B2와 동일 패턴). 재빌드 비용 때문에 보류.
- 레퍼런스: working/ai_authoring_disclaimer_types.md, working/boilerplate_articles_titles.md.

### ② 프롬프트 회귀 누수 (AI 곁가지 기사) — *깔끔한 술어 없음, 미실행*

- 원인: G.1에서 *"AI mentioned only as a tool → none"* 규칙이 삭제됨.
- 증상: 대선 표심·전당대회 등 AI 무관 기사가 substantive 속성으로(특히 Elections 노이즈). AI키워드 ≤2회 &
  non-none 그룹을 샘플링한 의심률 ~30%(상한 46.5%는 과대) — 자동 제거는 과함.
- 옵션: (a) prompts.py 줄 복원 → **v3로 전량 재분류**(~$42), (b) 한계로 문서화·수용.
- 이 항목은 단순 DELETE 불가 — 별도 의사결정 필요.

> 참고: 위 두 누수는 §9의 "데이터 정제 측면" 후보(A: Strict 필터 강화, B: GPT prompt v3)와 직결된다.
> v2.00 정본 데이터(`news_classifications`)는 현재 v2 프롬프트 + 미제거 boilerplate를 **포함한 채** 존재함을 명심.

---

## v2-I. 변경 파일 종합

**git 추적 (커밋 대상)**:
- 이번 분류 세션 직접 변경: `prompts.py`(G.1 한 줄), `REVISION_NOTES.md`(본 문서).
- v2.00 인프라(직전 커밋 `8e08335` 이후 누적): `analyze/news_cleaning.py`, `analyze/news_descriptive.py`,
  `analyze/classify_news_kr.py`, `analyze/classify_news_kr_batch.py`, `duckdb_mcp_server.py`, `config.py`,
  `analyze/subtopic_bertopic.py`, `figures/temporal_top10.py`, `CLAUDE.md`, `WORKFLOW.md`,
  `analyze/news_cleaning.md`.
- 삭제: `BERTOPIC_KIWI_HANDOFF.md`, `analyze/compare_models.py`, `analyze/subtopic_{discover,finalize,overlap}.py`.

**git 미추적 (gitignored)**: `working/run_batch_sequential.py`(신규 드라이버),
`working/{ai_authoring_disclaimer_types,boilerplate_articles_titles,kr_news_by_attr_FINAL}.md`,
`data/news/news_analysis.duckdb`(news_articles 76,645 + news_classifications 76,645 + news_cleaning_runs),
`output/news_analysis.md` + `output/figures/news_*`.

---
---

# v1.00 — WSL 이주 + BERTopic GPU + 다중 라벨 + 누수 발견 (MAJOR, baseline)

**일자**: 2026-05-25 ~ 05-27 · **commit**: `8e08335`("WSL 이주 + BERTopic GPU·다중 라벨 + 데이터 누수 발견")

이 문서의 최초 베이스라인. 한·미·EU AI 정책 비교 분석 파이프라인에 대한 일련의 진단·교정·발견.
아래 절 번호(§1~§10)는 v2.00 본문에서 그대로 인용된다.

## 1. 개요 — 무엇을 했나

| 작업 영역 | 핵심 활동 |
|---|---|
| **환경 이주** | Windows → WSL2 Linux. `.venv` 신규 구축, cuml-cu13 GPU 백엔드 도입 |
| **BERTopic 파이프라인 GPU 가속** | `subtopic_bertopic.py` 리팩터, KO clustering 5분 → 10초 (~30배) |
| **Korean tokenizer 정상화** | Kiwi 명사 추출 + `pretokenize_ko_texts()` 정확히 작동 검증 (이전 옛 코드 산물 조사 결합형 1,615건 → 0건) |
| **mcs 함수 calibration** | 8 함수 × 10 attr × K=3 sweep, τ 0.20-0.70 |
| **cuML 비결정성 정량화** | 100 runs × 4 attr stability_runs, co-clustering matrix M 분석 |
| **canonical cluster + 다중 라벨 가설 검증** | τ-K 분리 표기, primary/secondary/tertiary L1/L2/L3 분류 |
| **데이터 누수 발견** ⭐ | 공익/소비자보호 attr 31.9%가 AI 1회 부수 언급. 조류인플루엔자 약어 false positive. BERTopic이 누수 cluster 자동 분리 |
| **GPT 분류 prompt 점검** | `prompts.py::SYSTEM_PROMPT` 현재 상태 검토 + 누수 차단 안 되는 원인 파악 |
| **폴더 정책 정리** | `data/` = 정본 원본, `output/` = 산출물, `.cache/` = 캐시. config.py 상수화 |

## 2. 환경 이주 (WSL + venv + cuML)

- Windows의 BERTopic Kiwi 통합 시 hang 문제(구 `BERTOPIC_KIWI_HANDOFF.md`, v2.00에서 삭제) → WSL2에서 해소
- 새 `.venv` 구축: Python 3.12.3, torch 2.12+cu130, cuml-cu13 26.4.0, kiwipiepy 0.23
- 임베딩 캐시 `.cache/bertopic_embeddings/` 도입 — 재실행 시 hit (1.4 GB)

## 3. BERTopic 파이프라인 변경 (analyze/subtopic_bertopic.py)

| 변경 | 효과 |
|---|---|
| `--backend {auto,cuml,cpu}` CLI 옵션 | GPU 백엔드 선택 가능. cuML 시 산업정책 KO 36k clustering 5분 → 10초 |
| `pretokenize_ko_texts()` Kiwi 명사 추출 정상화 | 조사 결합형 키워드 (`인공지능을/이/의`) 제거 |
| KO 분기 `ngram_range=(1,2)` | 의미 있는 bigram (`딥페이크 성범죄`, `데이터센터 전력`) 키워드로 |
| `'AI'`/`'A.I'` stop_words 추가 | 단순 AI prefix bigram noise 차단 |
| `mcs = max(8, ceil(0.6·√n))` (sqrt × 0.6) | 산업정책 0번 거대 컨테이너 28% → 9.2% 분할 |
| article→topic 매핑 → `assembly_analysis.duckdb::subtopic_assignments` 테이블 | run_timestamp 버전 누적 |
| lazy `_get_client()` for OpenAI | `OPENAI_API_KEY` 없어도 `--no-label` 실행 가능 |

## 4. mcs Calibration — 전체 기록

상세: [working/calibration_history.md](working/calibration_history.md)

### Phase 1: 3 attr × 8 함수 (산업/공익/책임)
sqrt × 0.6 (mcs = max(8, ceil(0.6·√n))) 평균 max% 10.4%로 최고. 정본 파이프라인에 임시 적용.

### Phase 2: 전체 10 attr × 8 함수 (편향 보정)
**Phase 1의 결론 뒤집힘**. sqrt × 0.6이 AI안전(max 70%), 노동(max 52%)에서 망가뜨림. 단일 함수가 모든 attr에 best 아님 확인.

attr별 최적 mcs는 **mcs/√n 0.22 ~ 1.29 (6배 차이)** — 단일 함수로 fit 불가능.

### Phase 3: 비결정성 10회 반복
같은 mcs·random_state=42에서 4/10 attr 매우 불안정 (책임/산업/공익/선거 max% range 33-50%p). 6/10 attr 완전 deterministic.

### Phase 4-5: 100 runs + co-clustering matrix (불안정 attr)
**핵심 발견**: cuML의 의미적 cluster 구조는 안정적, **topic_id 매핑만 random**.

책임/윤리AI:
- topic_id 직접 비교: `cluster_swap` 60% (alignment 인공물)
- co-clustering matrix: `true_stable` 60%, `cluster_swap_real` 0.3% (8건)

### Phase 6: K=3 다중 라벨 가설 검증 (τ × N_canon × K=3)

상세: [working/calibration_k3_results.md](working/calibration_k3_results.md) (110 cells × 6 metric)

용어 분리:
- **τ** (co-clustering threshold) — 0.20~0.70 sweep
- **N_canon** — τ 적용 후 connected components 수 (책임/윤리AI: 16~23)
- **K** — multi-label 수 (top-3 고정)

4-tier 분류 (alignment-invariant):
- **L1**: visit canonical 1개 (label-only)
- **L2**: top-K 안 swap + centroid sim ≥ 0.85 (fragmentation, 의미 가까움)
- **L3**: top-K 안 swap + sim < 0.85 (real multi-topic)
- **noisy**: top-K 밖 swap (가설 실패)

결론: **τ = 0.25 ~ 0.30 sweet spot**. 4개 불안정 attr 모두에서 noisy 6%~17%. τ ≥ 0.4부터 noisy 30-60% 폭증 (cluster 단편화).

상세 비교: [working/finding_cluster_stability.md](working/finding_cluster_stability.md)

## 5. cuML 비결정성의 본질

- **outlier 판정은 deterministic** — 어떤 article이 outlier로 분류될지 매번 동일
- **cluster boundary만 random** — 의미적으로 가까운 cluster(centroid sim 0.78~0.91) 사이를 흔들림
- **swap이 의미 가까운 영역 안에서만 일어남** — 절대 random noise 아님
- **boundary 기사 특성**: 텍스트 길이↑, multi-policy 비교 기사, 신문 longform > 방송 단신

## 6. 데이터 누수 발견 ⭐ (v1.00의 핵심)

### 6.1 정량 — 공익/소비자보호 attr 사례

| 지표 | 값 |
|---|---|
| Strict 필터 통과 KR 뉴스 | 81,121건 |
| GPT가 `primary=none` 분류 (BERTopic 제외됨) | 15,603건 (19.2%) |
| BERTopic 입력으로 사용 | 65,517건 (80.8%) |
| **공익/소비자보호 attr** | **10,879건** |
| 그 중 **AI 본문 등장 1회만** (강력 누수 의심) | **3,472건 (31.9%)** |
| AI 1-2회 (확장 누수 의심) | 6,130건 (56.3%) |

### 6.2 누수 패턴 3종

| 패턴 | 예시 | 원인 |
|---|---|---|
| **A. AI 약어 ≠ 인공지능** (false positive) | "멧돼지도 비상… AI 부분만 체계적" (AI = 조류인플루엔자), "포천 산란계 AI" | Strict 필터 정규식 `(?i)\bAI\b`가 가축·조류 질병 약어 매치 |
| **B. AI 부수 활용 (true positive but 본질 아님)** | "불수능 영어 + AI 도입 한 줄", "윤 대통령 F학점 + 바이오·인공지능 산업 육성 한 줄" | GPT가 본문 메인 정책 주제로 분류 (Public interest, Industrial policy) |
| **C. 정책 리스트 부수 언급** | 정부 발표 중 "AI" 한 항목으로 1회 등장 | 위와 동일 |

### 6.3 BERTopic이 누수 cluster 자동 분리 — 매우 valuable 발견

공익/소비자보호 BERTopic 결과:

| topic | size | 키워드 | 평가 |
|---|---|---|---|
| -1 (outlier) | 3,339 (30.7%) | — | 진짜 multi-topic + 누수 혼합 |
| 0 | 1,651 | 상담/고객/전화 | 챗봇 AI ✓ |
| 1 | 1,017 | 안전/교통사고/감시 | AI 안전 ✓ |
| 2 | 671 | 디지털 교과서 | AI 교육 ✓ |
| 3 | 440 | 보이스피싱 | AI 사기 탐지 ✓ |
| **4** | **308** | **여야/발언/내용/정부** | ⚠️ **정치·정부 발언 누수** |
| **6** | **270** | **오리 농장, 병원 확인/확진/발생** | ⚠️ **명백한 조류인플루엔자 누수** |

### 6.4 Topic 6 (가축질병) 정밀 검증

270건 키워드 분석:
- "인공지능" 풀네임 0회: **89.6% (242건)**
- "AI" 단독 토큰: 802회 (95.6%)
- 가축질병 맥락 동반 (고병원성/조류/오리/닭/축산/방역): **77.8% (210건)**
- 진짜 인공지능 기사 (풀네임 3회+): **단 1.1% (3건)**

→ Topic 6 cluster의 78%가 명백 조류인플루엔자 누수.

### 6.5 GPT `none` 분류 효과

15,603건 (Strict 통과의 19.2%) — GPT가 자동 차단. 명백 조류인플루엔자 (예: "전남 나주 오리 농가 고병원성 AI 확진") 다수 차단 ✓. 그러나 회색 지대 (부수 정책 언급)는 통과.

### 6.6 GPT 분류 prompt 현재 상태

[prompts.py::SYSTEM_PROMPT](prompts.py) 의 "none" 규칙 5개 ([prompts.py:114-121](prompts.py#L114-L121)):
- Product launch / corporate announcement
- Stock analysis / financial reporting
- Technical research / benchmarks
- **AI mentioned only as a tool; real subject elsewhere** ← 이 규칙이 약함
- Pure how-to or tutorial

**누수가 통과하는 이유**:
1. "real subject is elsewhere" 표현이 모호 — 정부 정책 기사인데 AI도 포함된 경우 못 잡음
2. 조류인플루엔자 약어 명시 없음
3. AI 등장 빈도 hint 없음

> ※ v2.00 G.1에서 이 4번째 규칙이 조류독감 규칙으로 **교체**되며 제거됨 → 회귀 누수 발생(§v2-H②).

## 7. 산출물·자료 인덱스

### v1.00 핵심 문서
- [working/finding_cluster_stability.md](working/finding_cluster_stability.md) — 4 attr × 100 runs stability + canonical cluster 분석 (정본 상세)
- [working/calibration_history.md](working/calibration_history.md) — mcs calibration 시도 6 phase 종합

### 실험 스크립트 (working/, gitignored)
- `calibrate_mcs.py` — 8 함수 × 10 attr calibration
- `test_stability.py` — 10 runs per attr
- `stability_runs.py` — 100 runs per attr (4+6 attr)
- `stability_analyze.py` — tier 분류
- `cocluster_analyze.py` — alignment-invariant 분석
- `consistency_check.py` — primary/secondary multi-label 가설
- `calibrate_tau_k3.py` — K=3 + τ sweep × 10 attr × 6 metric

### 정본 분석 산출물 (output/)
- `output/analysis/subtopics_bertopic.json` — 최종 BERTopic 결과 (10 attr)
- `output/analysis/articles_classified_{guardian,nyt}.json` — 영문 뉴스 분류
- `output/figures/temporal_top10_bump.html`, `_lifespan.png` — 분기별 Top-10 시각화

### 시험·진단 산출물 (working/, gitignored — 2026-05-28 폴더 정책 강화로 output/에서 이동)
- `working/stability/{attr}_runs.npy` — 100 runs × n_articles (10개 attr)
- `working/stability/{attr}_cocluster.npy` — co-clustering matrix
- `working/stability/{attr}_consistency_*.json` — 책임/윤리AI 분류
- `working/calibration_k3_tau_sweep.json` — 110 cells × 6 metric
- `working/mcs_calibration.json`, `mcs_stability.json` — Phase 2-3 결과
- `working/leak_publicinterest_ai1_titles.md` — 공익 attr AI 1회 기사 3,472건 제목
- `working/publicinterest_articles_with_subtopic.md` — 공익 10,879건 + BERTopic subtopic 매핑
- `working/none_classified_titles.md` — `primary=none` 분류 15,603건 제목
- `working/topic_industrial_university.md` — 산업정책 대학·인재양성 토픽 1,537건
- `working/subtopics_all.md` — 10개 attr × 토픽 키워드 전체

### DB 산출물
- `assembly_analysis.duckdb::subtopic_assignments` — article→topic_id 매핑 (run_timestamp 누적)
- `news_analysis.duckdb::news_classifications` — KR 뉴스 10속성 분류 (2026-05-28 raw `news.duckdb`에서 이주, + `cleaning_version` 컬럼)

## 8. v1.00의 의의

이번 작업에서 드러난 핵심 문제:

1. **데이터 정제의 한계 노출** — Strict 필터 + GPT 1단계 분류로는 부수 언급 누수 차단 부족
2. **BERTopic 결과의 신뢰성 재평가 필요** — 30%+ 누수 기사가 cluster 내부에 섞임
3. **GPT prompt 개선 시급** — Carvão 10속성 framework는 적절하나 "none" 분류 기준이 약함
4. **canonical cluster·다중 라벨 가설은 검증됨** — τ=0.30 + K=3 조합으로 noisy 6-17%, cuML 비결정성을 본질적으로 흡수 가능

## 9. 다음 단계 (v1.00 시점의 후보 — 일부는 v2.00에서 실행)

### 데이터 정제 측면
A. **Strict 필터 강화** — 가축질병 약어 제외 규칙 (`AI 백신`, `AI 발생`, 고병원성·조류·오리·닭 동반 시 제외) → *v2.00 R3·R6에서 부분 반영*
B. **GPT prompt v3** — `none` 규칙 강화 (AI 본질성 검증, 부수 언급 명시 차단, 조류인플루엔자 약어 안내) → *v2.00 G.1에서 부분 실행(조류독감만), 회귀 발생*
C. **KR 뉴스에 Stage 2 GPT 필터** — KR 법안의 core/adjacent/unrelated 모델 적용
D. **BERTopic 기반 누수 cluster 자동 식별** — 각 attr cluster 키워드 스캔 → 의심 cluster GPT 재검증

### 분석 측면
E. **다른 attr의 누수 정량** — 산업정책 36k cluster 안 누수, 책임/윤리AI hub C0/C3 검증
F. **τ=0.30 + K=3 다중 라벨 정본 파이프라인 통합** — `subtopic_assignments` 테이블에 primary/secondary/tertiary 컬럼 추가
G. **L1/L2/L3 분류 자동화** — 다운스트림 분석 (시각화·해석)에 활용

### 시각화·보고서 측면
H. **`output/figures/temporal_top10` 누수 영향 재평가** — top-10 토픽 중 누수 cluster 비중 명시
I. **누수 정제 전후 BERTopic 결과 비교 figure** — major revision의 핵심 근거 자료
J. **WORKFLOW.md §3.5 Strict 필터 섹션 갱신** — 누수 한계 명시 + 개선 계획

## 10. 변경 파일 (v1.00 = commit 8e08335)

추적 가능 (git):
- `WORKFLOW.md` — 폴더 정책 + 2026-05-25/27 작업 이력
- `config.py` — output/.cache/ 경로 상수
- `.gitignore` — data/output/.cache/ 등재
- `requirements.txt` — cuml-cu13, pytest 등 추가
- `analyze/subtopic_bertopic.py` — cuML/Kiwi/매핑/캐시 리팩터
- `figures/temporal_top10.py` — 신규 시각화 (분기별 Top-10)
- `REVISION_NOTES.md` — 본 문서

추적 안 함 (gitignored): `working/*.py`, `working/*.md`, `output/`, `data/`, `.cache/`
