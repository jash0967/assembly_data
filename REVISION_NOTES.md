# Major Revision Notes (2026-05-25 ~ 2026-05-27)

이 문서는 한·미·EU AI 정책 비교 분석 파이프라인에 대해 진행한 일련의 진단·교정·발견을 정리한다. 사용자가 Major revision 준비 단계에서 다른 협업자·미래 자신·외부 검토자가 한 곳에서 작업 흐름과 핵심 발견을 파악할 수 있도록 작성됨.

상세 자료·실험 스크립트·중간 산출물은 `working/` 폴더(gitignored), `output/` 폴더, `data/` DB에 분산. 본 문서는 그 인덱스 + 발견 요약.

---

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

---

## 2. 환경 이주 (WSL + venv + cuML)

- Windows의 BERTopic Kiwi 통합 시 hang 문제([BERTOPIC_KIWI_HANDOFF.md](BERTOPIC_KIWI_HANDOFF.md)) → WSL2에서 해소
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

## 6. 데이터 누수 발견 ⭐ (Major Revision의 핵심)

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

## 7. 산출물·자료 인덱스

### Major revision 핵심 문서
- [REVISION_NOTES.md](REVISION_NOTES.md) — 본 문서
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

### 분석 데이터 (output/)
- `output/stability/{attr}_runs.npy` — 100 runs × n_articles (10개 attr)
- `output/stability/{attr}_cocluster.npy` — co-clustering matrix
- `output/stability/{attr}_consistency_*.json` — 책임/윤리AI 분류
- `output/analysis/calibration_k3_tau_sweep.json` — 110 cells × 6 metric
- `output/analysis/mcs_calibration.json`, `mcs_stability.json` — Phase 2-3 결과
- `output/analysis/subtopics_bertopic.json` — 최종 BERTopic 결과 (10 attr)
- `output/figures/temporal_top10_bump.html`, `_lifespan.png` — 분기별 Top-10 시각화

### 누수 검증 자료
- [output/exports/leak_publicinterest_ai1_titles.md](output/exports/leak_publicinterest_ai1_titles.md) — 공익 attr AI 1회 기사 3,472건 제목
- [output/exports/publicinterest_articles_with_subtopic.md](output/exports/publicinterest_articles_with_subtopic.md) — 공익 10,879건 + BERTopic subtopic 매핑
- [output/exports/none_classified_titles.md](output/exports/none_classified_titles.md) — `primary=none` 분류 15,603건 제목

### DB 산출물
- `assembly_analysis.duckdb::subtopic_assignments` — article→topic_id 매핑 (run_timestamp 누적)
- `news.duckdb::news_classifications` — KR 뉴스 10속성 분류

## 8. Major Revision의 의의

이번 작업에서 드러난 핵심 문제:

1. **데이터 정제의 한계 노출** — Strict 필터 + GPT 1단계 분류로는 부수 언급 누수 차단 부족
2. **BERTopic 결과의 신뢰성 재평가 필요** — 30%+ 누수 기사가 cluster 내부에 섞임
3. **GPT prompt 개선 시급** — Carvão 10속성 framework는 적절하나 "none" 분류 기준이 약함
4. **canonical cluster·다중 라벨 가설은 검증됨** — τ=0.30 + K=3 조합으로 noisy 6-17%, cuML 비결정성을 본질적으로 흡수 가능

## 9. 다음 단계 (Major Revision 작업 후보)

### 데이터 정제 측면
A. **Strict 필터 강화** — 가축질병 약어 제외 규칙 (`AI 백신`, `AI 발생`, 고병원성·조류·오리·닭 동반 시 제외)
B. **GPT prompt v3** — `none` 규칙 강화 (AI 본질성 검증, 부수 언급 명시 차단, 조류인플루엔자 약어 안내)
C. **KR 뉴스에 Stage 2 GPT 필터** — KR 법안의 core/adjacent/unrelated 모델 적용
D. **BERTopic 기반 누수 cluster 자동 식별** — 각 attr cluster 키워드 스캔 → 의심 cluster GPT 재검증

### 분석 측면
E. **다른 attr의 누수 정량** — 산업정책 36k cluster 안 누수, 책임/윤리AI hub C0/C3 검증
F. **τ=0.30 + K=3 다중 라벨 정본 파이프라인 통합** — `subtopic_assignments` 테이블에 primary/secondary/tertiary 컬럼 추가
G. **L1/L2/L3 분류 자동화** — 다운스트림 분석 (시각화·해석)에 활용

### 시각화·보고서 측면
H. **`output/figures/temporal_top10` 누수 영향 재평가** — top-10 토픽 중 누수 cluster 비중 명시
I. **누수 정제 전후 BERTopic 결과 비교 figure** — Major revision의 핵심 근거 자료
J. **WORKFLOW.md §3.5 Strict 필터 섹션 갱신** — 누수 한계 명시 + 개선 계획

## 10. 변경 파일 (이번 작업)

추적 가능 (git):
- `WORKFLOW.md` — 폴더 정책 + 2026-05-25/27 작업 이력
- `config.py` — output/.cache/ 경로 상수
- `.gitignore` — data/output/.cache/ 등재
- `requirements.txt` — cuml-cu13, pytest 등 추가
- `analyze/subtopic_bertopic.py` — cuML/Kiwi/매핑/캐시 리팩터
- `figures/temporal_top10.py` — 신규 시각화 (분기별 Top-10)
- `REVISION_NOTES.md` — 본 문서

추적 안 함 (gitignored): `working/*.py`, `working/*.md`, `output/`, `data/`, `.cache/`
