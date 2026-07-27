# 한국 도메스틱 뉴스 데이터 클리닝 — 2단계 파이프라인

본 문서는 `data/news/news.duckdb`의 `news_articles` 테이블(raw 157,886건)을 한국 AI 정책 담론 분석 대상으로 정제하는 2단계 파이프라인의 설계·근거·영향을 정리.

**파이프라인 구조**:
- **Stage 1 — Boilerplate Removal**: 본문 정화 (행 보존). 모든 기사가 대상이고 cleaning 결과는 `news_analysis.duckdb::news_articles.content` 컬럼에 저장.
- **Stage 2 — AI Relevance Filter**: 정화된 본문 위에서 행 단위 통과/탈락 판정. 결과 ≒ 81,088건.
- **Stage 3 — Deduplication**: 같은 매체에서 동일 본문(공백 정규화) 다중 ingest를 1행으로 collapse. 결과 76,645건.

**DB 분리·책임** (2026-05-28 도입):
- raw `data/news/news.duckdb` — `news_articles` (157,886 raw rows). **수집 원본만**, 분류 정보 없음.
- analysis `data/news/news_analysis.duckdb`
  - `news_articles` (Stage 1+2+3 적용본, 76,645)
  - `news_cleaning_runs` (빌드 메타) — **news_cleaning.py가 소유**
  - `news_classifications`, `news_prompt_versions` — **classify_news_kr*.py가 소유** (cleaning은 만들지 않음)
  - `subtopic_assignments` — **subtopic_bertopic.py가 소유** (append-only, run_timestamp별)
- 감사 로그 `data/_audit/db_updates.jsonl` — **db_audit.py가 소유**. 위 스크립트들이 자기 실행을 `audit_run()` 으로 감싸 append 한다(테이블 소유권 모델 밖 — DB 파일 안이 아니라 사이드카). `news_cleaning.py` 의 빌드 실패 → `.bak` 복원에도 이력이 살아남게 하려는 것이 위치 선택의 이유.

**워크플로우**:
```
collect (build_news_db.py)         → raw.news_articles
news_cleaning.py                   → cleaned.news_articles + news_cleaning_runs
classify_news_kr*.py               → cleaned.news_classifications + news_prompt_versions
```

**코드·CLI**: [news_cleaning.py](news_cleaning.py) — 룰 정의 + 빌드 IO + CLI가 한 모듈.
```bash
python analyze/news_cleaning.py              # 일반 빌드 (raw → analysis DB)
python analyze/news_cleaning.py --dry-run    # SQL만 출력
python analyze/news_cleaning.py --stage1-only # Stage 1 영향만 측정
```

빌드가 cleaned DB에 이미 있는 `news_classifications`를 발견하면 룰 강화·dedup으로 사라진 행의 orphan 분류만 자동 정리한다. 분류 자체는 생성·마이그레이션하지 않는다.

**Public API** (소비 스크립트는 import할 일 없음; analysis DB의 `content` 컬럼을 직접 SELECT):
```python
from news_cleaning import SANITIZE_CONTENT_SQL, RELEVANCE_WHERE, RULES_APPLIED
```

---

## 1. 배경

공급사(한국언론진흥재단 Newstore)는 word boundary 없는 단순 substring 매칭으로 데이터셋을 구성. 그 결과 KAIST·KAI·인스타그램 핸들·`인공강우` 같은 부분문자열이 잡혀 raw 157,886건 중 약 21.6%(34,066건)가 AI 무관 일반 뉴스로 누수. 추가로 매체별 boilerplate·footer·promo 텍스트가 더 큰 노이즈를 유발하는 것이 발견됨.

본 Strict 필터는 정확한 AI 보도만 추출하기 위해 다단계 정제 규칙으로 설계.

---

## 2. 규칙 개요 (실행 순서)

```
input: raw.news_articles 행 (title, content)
  ▼
┌─ Stage 1 — Boilerplate Removal (본문 정화, 행 보존) ──────┐
│ [Rule B1] YTN AI 앵커 footer · promo 라인 제거 (regex)   │
│ [Rule B2] MBC '(AI학습 포함)' substring 제거 (REPLACE 2단)│
│   → sanitized_content                                    │
└──────────────────────────────────────────────────────────┘
  ▼
┌─ Stage 2 — AI Relevance Filter (행 단위 통과/탈락) ──────┐
│ [Rule R1] 키워드 매칭 (title raw + sanitized_content)    │
│           — OR 조건                                       │
│ [Rule R2] 영문 본문 제외 (한글 < 5%, raw content)         │
│ [Rule R3] 조류인플루엔자 약자 충돌 제외 (raw content)     │
│ [Rule R4] 사이버대학 광고 통째 제외 (raw content/title)   │
│ [Rule R5] 일반 대학 모집 광고 footer 3중 조합 제외        │
│ [Rule R6] KBS '[사진기사]' placeholder stub 제외 (raw)    │
└──────────────────────────────────────────────────────────┘
  ▼ (Stage 2 통과 81,088 rows)
┌─ Stage 3 — Deduplication (행 collapse) ──────────────────┐
│ [Rule D1] (provider, MD5(content_no_ws)) 그룹별 1행 유지  │
│   keep 우선순위: published_at ASC NULLS LAST →            │
│                  byline NOT NULL →                        │
│                  provider_link_page NOT NULL →            │
│                  news_id ASC (결정적)                     │
└──────────────────────────────────────────────────────────┘
  ▼
output: news_analysis.news_articles (76,645 rows, content는 Stage 1 적용본)
```

핵심 설계 원칙:

- **title은 raw 그대로 사용**. 매체가 title에 boilerplate를 넣은 사례가 없어 cleaning 불필요.
- **Stage 1은 content만 정제** (B1 → B2). 행은 보존.
- **Stage 2 R1 키워드 매칭은 sanitized content 위에서 동작**해야 boilerplate 재진입 방지.
- **Stage 2 R2~R6는 raw content/title 기준** (영문 판정·조류 충돌·광고 매칭·placeholder 매칭은 정화 영향 받지 않음).
- 키워드 매칭(R1)은 OR, 제외 조건(R2·R3·R4·R5·R6)은 AND NOT으로 결합.
- **Stage 3 D1은 Stage 1 적용본 `content`(공백 정규화)에서 정확 매치**. 같은 매체 안에서만 묶음 (매체 간 syndication = 0건 확인).

빌드 진입점 [news_cleaning.py](news_cleaning.py)가 두 단계를 한 SQL로 합성:
`CREATE TABLE news_articles AS SELECT ..., SANITIZE_CONTENT_SQL("content") AS content FROM raw.news_articles WHERE RELEVANCE_WHERE(...)`.

---

## 3. 규칙 상세

> 이전 라벨링 (Rule 1~7)에서 변경: 본문 정화(B1·B2)와 행 필터링(R1~R5)이 본질적으로 다른 작업이라 Stage 1·2로 명시 분리. 룰 내용 자체는 동일.

### Stage 1 / Rule B1 — YTN AI 앵커 footer · promo 라인 제거

**발견 (2026-05-22)**: YTN의 라디오·자막뉴스 서비스(Y-GO, Y-ON) 콘텐츠 본문 끝에 자동 첨부되는 footer가 약 9,000건 이상의 일반 뉴스(국방·국제·사회·정치 등)를 AI 보도로 위장.

**패턴**:

| 패턴 | 등장 건수 | 비고 |
|---|---:|---|
| `AI 앵커ㅣY-GO` | 3,586 | 가장 많은 footer |
| `오디오ㅣAI앵커` | 1,334 | |
| `오디오ㅣAI 앵커` | 1,314 | |
| `YTN AI 앵커 이름 맞히고 AI 스피커 받자!` | 669 | 이벤트 광고 |
| `AI앵커 : Y-GO` | 586 | |
| `오디오: AI앵커` | 471 | |
| 기타 변형 (Y-ON, 구분자 변화) | ~1,500+ | |
| `유일하게 AI가 대체 못 하는 직업?` | 230 | 2025-09~10 promo 링크 |

**정규식**: `[^\n]*(AI ?앵커|AI가 대체 못 하는)[^\n]*`

- 줄 단위로 매칭하여 해당 줄 전체 제거 (newline은 보존).
- `AI ?앵커`: 공백 유무 모두 매칭.
- `AI가 대체 못 하는`: 단발성 promo 링크 ("유일하게 AI가 대체 못 하는 직업?").

**검증** — 진짜 AI 보도를 잃지 않는가?

- YTN 제목에 "AI 앵커/AI앵커/인공지능 앵커/AI 아나운서" 명시 6건 → **6건 모두 통과** (title에는 cleaning 미적용).
- 1차→2차 강화로 사라진 8,853건 중 본문에 "인공지능" 있는 케이스 = **1건뿐** (collateral 거의 0).
- "ChatGPT/딥러닝/머신러닝/생성형/LLM" 동반 = 0건.

### Stage 1 / Rule B2 — MBC boilerplate substring 제거

**발견 (2026-05-21)**: MBC는 2025년부터 모든 기사 footer에 `(AI학습 포함) 금지` 문구를 부착. 2025년 MBC 24,259건 중 99.2%가 이 boilerplate로 false positive (다른 매체 0%).

**정규식 아닌 substring REPLACE**:

- `(AI학습 포함)` 정확 substring 제거
- `(AI 학습 포함)` 정확 substring 제거 (띄어쓰기 변형)

**왜 정확 substring인가**: 본문 진짜 표현 `'AI 학습'` (띄어쓰기, 656건) · `'AI학습'` (붙임, 61건) 같은 정당한 표현은 보존해야 함. 괄호로 묶인 boilerplate만 정확히 제거.

### Stage 2 / Rule R1 — 키워드 매칭 (OR)

다음 조건 중 하나라도 충족하면 통과:

| 매칭 | title | cleaned content |
|---|:---:|:---:|
| `ILIKE '%인공지능%'` | ✅ | ✅ |
| `ILIKE '%인공 지능%'` | ✅ | ✅ |
| word-boundary `AI` regex | ✅ | ✅ |
| word-boundary `A.I` / `A.I.` regex | ✅ | ✅ |

**Word boundary 정규식**: `(?i)(^|[^A-Za-z0-9])AI([^A-Za-z0-9]|$)`

- 앞뒤가 ASCII 알파넘이 아닌 경우만 매칭.
- 한글 조사 (`AI가`, `AI는`, `AI를`, `AI의`)는 자연스럽게 매칭됨 (한글 = non-ASCII).
- `KAIST`, `OpenAI`, `hawaii` 같은 false positive 차단.

**`artificial intelligence` 풀스펠 제외**: 검증 결과 영문 본문 제외(Rule R2)와 완전 중복. 한국어 본문에 풀스펠만 단독 등장하는 케이스 0건 확인 (2026-05-22).

### Stage 2 / Rule R2 — 영문 본문 제외

**발견 (2026-05-22)**: YTN `[K-SCIENCE]`·`[K-BIZ]` 등 외신용 영문 단신 207건 등 한국어가 아닌 본문은 한국 도메스틱 담론 분석 대상이 아님.

**판정 기준**:

- 본문 길이 > 200자
- 한글 char 비율 < 5% (`(content 길이 - 한글 char 길이) / content 길이 > 0.95`)

raw content 기준 (cleaning 결과와 무관). 짧은 영문 헤드라인은 제외하지 않음 (200자 이하).

### Stage 2 / Rule R3 — 조류인플루엔자 약자 충돌 제외

**발견 (2026-05-22)**: 농림축산식품부 보도자료에서 'AI'가 조류인플루엔자(Avian Influenza)의 약자로 사용됨. `'AI 확진'`, `'고병원성 AI'`, `'AI 양성 반응'` 등이 word boundary 정규식을 그대로 통과.

**규모**:

- 조류 키워드 + 인공지능 부재 = **3,287건 false positive**
- 이 중 GPT가 `Public interest`로 오분류 = 1,526건 (가축전염병이 공중보건으로 잡힘)
- 진짜 보존 대상 (조류 + 인공지능 동시 언급, 예: AI로 조류인플루엔자 진단) = 24건뿐

**제외 조건**:

```sql
(content/title에 '조류인플루엔자' 또는 '조류 인플루엔자' 포함)
AND
(content/title에 '인공지능' 및 '인공 지능' 모두 부재)
```

조류 키워드가 있어도 인공지능 단어가 함께 있으면 보존.

### Stage 2 / Rule R4 — 사이버대학 광고 통째 제외

**발견 (2026-05-24)**: 사이버대학교의 신·편입생 모집 광고가 본문에 'AI공학과·인공지능학과·AI융합' 학과명을 포함시켜 Strict 필터를 통과. 사용자 명시적 보고로 확인.

**규모**:

| 항목 | 카운트 |
|---|---:|
| 사이버대학 명칭 포함 (raw) | 825 |
| └ Strict 통과 (현재) | 749 (90.8%) |
| └ 중앙일보 집중 | 703 (93.8%) |
| 진짜 AI 정책 보도 (추정) | 5건 미만 |

**제외 조건**:

```sql
content ILIKE '%사이버대학%' OR title ILIKE '%사이버대학%'
```

**왜 통째 제외인가**:

- sample 검토 결과 사이버대학 명칭 + Strict 통과 749건 중 90% 이상이 신·편입생 모집·학과 신설 광고.
- 진짜 분석 대상(사이버대학 교수 인터뷰 등)은 추정 5건 미만으로 collateral 미미.
- `사이버대학` substring 1개로 사이버대학교·사이버대학원 등 변형 모두 포괄.
- 사용자가 "공격적 제외" 옵션을 명시적으로 선택.

### Stage 2 / Rule R5 — 일반 대학 AI 학과 모집 광고 제외 (footer 3중 조합)

**발견 (2026-05-24)**: 사이버대학 제외 후에도 일반 대학(4년제·전문대·평생교육원 등)의 AI 학과 신입/편입생 모집 광고가 광범위하게 통과. 단순 "모집+AI학과명" 조합은 collateral이 과도하여 footer 시그너처로 정확 매칭 필요.

**규모**:

| 항목 | 카운트 |
|---|---:|
| 사이버대 제외 후 "모집+AI" 동시 등장 | 4,647 |
| └ 광고 footer 3중 조합 매칭 | 557 |
| └ 정책 보도·칼럼·정부 보도자료 (보존 대상) | 4,090 |

**제외 조건** (3개 그룹 모두 충족 시):

```sql
(content ILIKE '%문의%' OR content ILIKE '%입학처%')
AND
(content ILIKE '%모집요강%' OR content ILIKE '%원서접수%' OR content ILIKE '%접수마감%')
AND
(content ILIKE '%장학금%' OR content ILIKE '%등록금%')
```

**왜 3중 조합인가**:

- 그룹 ① 연락 채널 (문의/입학처) — 광고 footer 필수 요소
- 그룹 ② 모집 메커니즘 (모집요강/원서접수/접수마감) — 광고 본질
- 그룹 ③ 금전 인센티브 (장학금/등록금) — 광고에서 거의 항상 강조

세 그룹이 모두 등장하는 본문은 입학 광고로 확정. 정책 보도·칼럼은 그룹 ②·③의 결합이 거의 없어 보존됨 (sample 검증 시 collateral 추정 5% 미만).

### Stage 2 / Rule R6 — KBS `[사진기사]` placeholder stub 제외

**발견 (2026-05-30)**: KBS 33건의 본문이 정확히 `[사진기사]` 6자뿐. 2020-10-05 ~ 2021-06-08 8개월 기간에 집중. KBS 내부 메타라벨로 추정 — 사진 위주 보도에 본문 텍스트 없음을 표시. 다른 5개 매체에는 0건.

**규모**:

| 항목 | 카운트 |
|---|---:|
| 본문 = `[사진기사]` (공백 정규화) | 33 (모두 KBS) |
| 기간 | 2020-10 ~ 2021-06 |
| `none` 분류 | 19 (57.6%) |
| 조류인플루엔자 보도 (오분류) | 다수 (R3 미통과 — 본문 6자) |

**제외 조건**:

```sql
REGEXP_REPLACE(content, '\s+', '', 'g') = '[사진기사]'
```

**검증**:

- `none` 19/33 (57.6%) — title만으로 분류 못 함
- `Public interest` 10건 — 대부분 조류인플루엔자 (R3 보호 본문 길이 200자 조건 미충족)
- `Industrial policy` 2건 + `National security` 2건 — title만 보고 GPT 추정
- 분류 정보량 사실상 0. false-positive 0건 (raw_content_no_ws 정확 매치).

### Stage 3 / Rule D1 — `(provider, content_no_ws)` 그룹 collapse

**발견 (2026-05-30)**: cleaning 단계는 boilerplate·필터링만 처리하고 행 단위 중복 제거는 안 함. PK `news_id`로만 unique 보장. 같은 보도가 KBS·YTN 24h 채널에서 시각별 재방송으로 여러 번 ingest되어 cleaned에 잔존.

**측정** (R6 적용 후 cleaned 81,088 기준):

| 분류 | 그룹 | drop 행 |
|---|---:|---:|
| 안전 (n_titles=1, 같은 title·같은 본문) | 3,274 | 3,911 |
| 위험 (n_titles>1, 다른 title·같은 본문) | 405 | 532 |
| **합계 (length 무관)** | **3,679** | **4,443** |

위험 그룹 405건 분석 결과 모두 **표기 변형의 진짜 중복**:
- 단위 (`10억 (불|달러) 이상`), 따옴표 (`'척척'` vs `‘척척’`), 띄어쓰기 (`1000만원` vs `1000만 원`), 구분자 (`1000만원` vs `1,000만 원`), 접두 (`SK 최태원` vs `[기업] SK 최태원`)
- R6 이전 위험의 핵심이었던 KBS `[사진기사]` placeholder는 R6로 흡수 → false-positive 0건

**제외 SQL** ([`DEDUP_DELETE_SQL`](news_cleaning.py)):

```sql
WITH ranked AS (
  SELECT news_id,
         ROW_NUMBER() OVER (
           PARTITION BY provider, MD5(REGEXP_REPLACE(content, '\s+', '', 'g'))
           ORDER BY published_at ASC NULLS LAST,
                    (byline IS NULL),
                    (provider_link_page IS NULL),
                    news_id
         ) AS rn
  FROM news_articles
)
DELETE FROM news_articles WHERE news_id IN (SELECT news_id FROM ranked WHERE rn > 1)
```

**keep 우선순위**:
1. `published_at ASC NULLS LAST` — 가장 이른 발행본 (원본 가능성)
2. `byline IS NULL` — byline 명시 행 우선 (메타 완전성)
3. `provider_link_page IS NULL` — 원본 URL 행 우선 (출처 추적)
4. `news_id` — 동률 깨기 (재실행 안정성)

**매체별 효과**:

| 매체 | Stage 2 통과 | D1 후 | drop |
|---|---:|---:|---:|
| 중앙일보 | 32,236 | 32,141 | -95 |
| YTN | 17,896 | 16,767 | **-1,129** (24h 채널 재방송) |
| 한겨레 | 11,590 | 11,580 | -10 |
| KBS | 14,044 | 10,863 | **-3,181** (지역방송 재방송) |
| MBC | 2,783 | 2,756 | -27 |
| SBS | 2,539 | 2,538 | -1 |
| **총** | **81,088** | **76,645** | **-4,443** |

KBS·YTN 비중이 압도적 — 24시간 뉴스 채널의 시각별 재방송이 주된 ingest 중복 원인.

---

## 4. 누적 영향 (94K → 76.6K)

| 단계 | 통과 | 변동 | 누적 차이 |
|---|---:|---:|---:|
| Raw | 157,886 | — | — |
| 공급사 substring leak 제거 (R1 word boundary) | ~94,029 | -63,857 | 41.6% |
| 1차 강화 — 조류 (R3) | 90,742 | -3,287 | 42.5% |
| 2차 강화 — YTN footer (B1) | 81,888 | -8,854 | 48.1% |
| 3차 강화 — 사이버대학 + 일반대 모집 광고 (R4·R5) | 81,121 | -767 | 48.6% |
| 4차 강화 — KBS `[사진기사]` placeholder (R6) | 81,088 | -33 | 48.6% |
| 5차 — Stage 3 D1 dedup | **76,645** | -4,443 | **51.5%** |

매체별 최종 통과율 (3차 강화 후):

| 매체 | Raw | Strict 통과 | 통과율 |
|---|---:|---:|---:|
| KBS | 17,659 | 14,077 | 79.7% |
| MBC | 33,144 | 2,783 | 8.4% |
| SBS | 3,091 | 2,539 | 82.1% |
| YTN | 37,260 | 17,896 | 48.0% |
| 중앙일보 | 48,019 | 32,236 | 67.1% |
| 한겨레 | 18,713 | 11,590 | 61.9% |

MBC가 매우 낮은 통과율을 보이는 이유는 2025년 boilerplate 효과 정화(B2). YTN이 약 절반인 이유는 footer 효과 정화(B1). 중앙일보 통과율 추가 감소는 R4 사이버대학 광고 정화 효과(주 -713건).

R4·R5 합산 -767건이 예상 -1,306건(749 사이버 + 557 footer)보다 적은 이유: 두 규칙 매칭 행이 약 539건 중복 (사이버대학 광고 다수가 footer 시그너처 동시 보유). 사이버대학 명칭과 footer 3중 조합 모두 가진 행은 한 번만 제외됨.

---

## 5. 검증·디버깅 도구

### 빠른 확인 CLI

```bash
python analyze/news_cleaning.py --dry-run     # 합성된 SQL 출력
python analyze/news_cleaning.py --stage1-only # Stage 1 영향만 측정 (DB 변경 없음)
```

### Python에서 SQL 식 확인

```python
from news_cleaning import SANITIZE_CONTENT_SQL, RELEVANCE_WHERE

sanitized = SANITIZE_CONTENT_SQL("content")
where = RELEVANCE_WHERE(
    title_expr="title",
    sanitized_content_expr=sanitized,
    raw_content_expr="content",
)
print(where)  # WHERE 절 그대로 SQL에 삽입 가능
```

### 한 행의 raw vs sanitized 비교

```sql
-- analysis DB 안에서 raw ATTACH 후
ATTACH 'data/news/news.duckdb' AS news_raw (READ_ONLY);

SELECT
  a.news_id,
  a.title,
  r.content AS raw,
  a.content AS sanitized   -- 이미 Stage 1 적용본
FROM news_articles a
JOIN news_raw.news_articles r ON r.news_id = a.news_id
WHERE a.news_id = '...';
```

### Rule 별 행 수 측정

`RELEVANCE_WHERE` 결과 문자열에 SQL `replace`로 특정 룰을 끄고 비교:

```python
from news_cleaning import SANITIZE_CONTENT_SQL, RELEVANCE_WHERE
sanitized = SANITIZE_CONTENT_SQL("r.content")
where = RELEVANCE_WHERE(
    title_expr="r.title",
    sanitized_content_expr=sanitized,
    raw_content_expr="r.content",
)
# R4 (사이버대학) 끈 버전 — substring 위치를 검색해서 제거
no_r4 = where.replace(
    "AND NOT (r.content ILIKE '%사이버대학%' OR r.title ILIKE '%사이버대학%')",
    "",
)
con.execute(f"SELECT COUNT(*) FROM news_raw.news_articles r WHERE {no_r4}")
```

또는 `news_cleaning_runs` 테이블의 sanitize/relevance hash를 변경된 룰로 비교:

```sql
SELECT cleaning_version, rules_applied, cleaned_row_count, sanitize_sql_hash, relevance_where_hash
FROM news_analysis.news_cleaning_runs
ORDER BY built_at DESC;
```

---

## 6. 향후 확장 가이드

새 boilerplate·promo 패턴 발견 시:

1. **본문 끝부분(마지막 200~300자)에 자주 등장하는 정확한 텍스트 추출** — `regexp_extract`로 line 단위 빈도 분석.
2. **GPT 분류 결과와 교차 검증** — false positive가 특정 라벨(Public interest, National security 등)에 비정상 집중하는지 확인.
3. **개별 행 sample (10건 이상)** — 본문 head·tail 동시 확인으로 패턴 위치 검증.
4. **Stage 결정**:
   - 본문 정화 (행 보존)면 → Stage 1 (B-라벨). `SANITIZE_CONTENT_SQL` 안의 wrapping에 추가.
   - 행 단위 통과/탈락이면 → Stage 2 (R-라벨). `RELEVANCE_WHERE` 안의 AND NOT 절에 추가.
5. **정규식·substring 결정**:
   - 줄 단위 boilerplate → `regexp_replace` with `[^\n]*PATTERN[^\n]*` (B1 패턴)
   - 정확한 substring (괄호로 묶인 표현 등) → `REPLACE` 2단 (B2 패턴)
   - 광고 시그너처 결합 → 3중 ILIKE AND 조합 (R5 패턴)
   - 기관 명칭 일괄 → 단일 ILIKE substring (R4 패턴)
6. **collateral 측정** — 새 규칙 추가 전후 통과 카운트 차이, 그 차이의 라벨 분포 확인. `python analyze/news_cleaning.py --dry-run`으로 새 hash 확인.
7. **빌드 영향 갱신**:
   - `news_cleaning.py`의 룰 정의에 추가 + `RULES_APPLIED` 상수 라벨 추가
   - `EXPECTED_TOTAL`·`EXPECTED_BY_PROVIDER` 갱신 (새 빌드 후 검증 출력으로)
   - 본 문서에 한 항목 추가
8. **재빌드**: `python analyze/news_cleaning.py` (articles 재빌드 + 새 `cleaning_version` 메타 INSERT). 이전 cleaning_version으로 분류된 행이 orphan이 되면 자동 정리됨. 재분류는 별도 `classify_news_kr*.py` 실행.

---

## 7. 관련 문서

- 공급사 제출용 이슈 정리: [data/exports/news_dataset_issues.md](../data/exports/news_dataset_issues.md)
- 필터링 방법론 전문: [data/exports/news_filtering_process.md](../data/exports/news_filtering_process.md)
- 정화 후 descriptive stats: [data/exports/news_descriptive_strict.md](../data/exports/news_descriptive_strict.md)
- 프로젝트 가이드: [CLAUDE.md](../CLAUDE.md) §"한국 도메스틱 뉴스 Strict AI 필터"
