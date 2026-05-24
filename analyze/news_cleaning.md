# 한국 도메스틱 뉴스 데이터 클리닝 — Strict AI 필터

본 문서는 `data/news/news.duckdb`의 `news_articles` 테이블(157,886건)을 한국 AI 정책 담론 분석 대상으로 정제하는 Strict 필터의 설계·근거·영향을 정리.

코드 정의: [news_cleaning.py](news_cleaning.py)
적용 진입점: `from news_cleaning import STRICT_WHERE`

---

## 1. 배경

공급사(한국언론진흥재단 Newstore)는 word boundary 없는 단순 substring 매칭으로 데이터셋을 구성. 그 결과 KAIST·KAI·인스타그램 핸들·`인공강우` 같은 부분문자열이 잡혀 raw 157,886건 중 약 21.6%(34,066건)가 AI 무관 일반 뉴스로 누수. 추가로 매체별 boilerplate·footer·promo 텍스트가 더 큰 노이즈를 유발하는 것이 발견됨.

본 Strict 필터는 정확한 AI 보도만 추출하기 위해 다단계 정제 규칙으로 설계.

---

## 2. 규칙 개요 (실행 순서)

```
input: news_articles 행 (title, content)
  ▼
[Rule 1] YTN AI 앵커 footer · promo 라인 제거 (regex_replace)
[Rule 2] MBC boilerplate substring 제거 (REPLACE 2단)
  → cleaned_content
  ▼
[Rule 3] 키워드 매칭 (title raw + cleaned_content) — OR 조건
  ▼
[Rule 4] 영문 본문 제외 (한글 비율 5% 미만 + 200자 이상)
[Rule 5] 조류인플루엔자 약자 충돌 제외
[Rule 6] 사이버대학 광고 통째 제외
[Rule 7] 일반 대학 AI 학과 모집 광고 제외 (footer 3중 조합)
  ▼
output: Strict 통과 행
```

핵심 설계 원칙:

- **title은 raw 그대로 사용**. 매체가 title에 boilerplate를 넣은 사례가 없어 cleaning 불필요.
- **content만 두 단계 정제** (Rule 1 → Rule 2).
- 키워드 매칭(Rule 3)은 OR 조합으로 polyglot 한국어/영문 모두 잡음.
- 제외 조건(Rule 4·5·6·7)은 AND NOT으로 결합.

---

## 3. 규칙 상세

### Rule 1 — YTN AI 앵커 footer · promo 라인 제거

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

### Rule 2 — MBC boilerplate substring 제거

**발견 (2026-05-21)**: MBC는 2025년부터 모든 기사 footer에 `(AI학습 포함) 금지` 문구를 부착. 2025년 MBC 24,259건 중 99.2%가 이 boilerplate로 false positive (다른 매체 0%).

**정규식 아닌 substring REPLACE**:

- `(AI학습 포함)` 정확 substring 제거
- `(AI 학습 포함)` 정확 substring 제거 (띄어쓰기 변형)

**왜 정확 substring인가**: 본문 진짜 표현 `'AI 학습'` (띄어쓰기, 656건) · `'AI학습'` (붙임, 61건) 같은 정당한 표현은 보존해야 함. 괄호로 묶인 boilerplate만 정확히 제거.

### Rule 3 — 키워드 매칭 (OR)

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

**`artificial intelligence` 풀스펠 제외**: 검증 결과 영문 본문 제외(Rule 4)와 완전 중복. 한국어 본문에 풀스펠만 단독 등장하는 케이스 0건 확인 (2026-05-22).

### Rule 4 — 영문 본문 제외

**발견 (2026-05-22)**: YTN `[K-SCIENCE]`·`[K-BIZ]` 등 외신용 영문 단신 207건 등 한국어가 아닌 본문은 한국 도메스틱 담론 분석 대상이 아님.

**판정 기준**:

- 본문 길이 > 200자
- 한글 char 비율 < 5% (`(content 길이 - 한글 char 길이) / content 길이 > 0.95`)

raw content 기준 (cleaning 결과와 무관). 짧은 영문 헤드라인은 제외하지 않음 (200자 이하).

### Rule 5 — 조류인플루엔자 약자 충돌 제외

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

### Rule 6 — 사이버대학 광고 통째 제외

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

### Rule 7 — 일반 대학 AI 학과 모집 광고 제외 (footer 3중 조합)

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

---

## 4. 누적 영향 (94K → 81.1K)

| 단계 | 통과 | 변동 | 누적 차이 |
|---|---:|---:|---:|
| Raw | 157,886 | — | — |
| 공급사 substring leak 제거 (Rule 3 word boundary) | ~94,029 | -63,857 | 41.6% |
| 1차 강화 — 조류 (Rule 5) | 90,742 | -3,287 | 42.5% |
| 2차 강화 — YTN footer (Rule 1) | 81,888 | -8,854 | 48.1% |
| 3차 강화 — 사이버대학 + 일반대 모집 광고 (Rule 6·7) | **81,121** | -767 | **48.6%** |

매체별 최종 통과율 (3차 강화 후):

| 매체 | Raw | Strict 통과 | 통과율 |
|---|---:|---:|---:|
| KBS | 17,659 | 14,077 | 79.7% |
| MBC | 33,144 | 2,783 | 8.4% |
| SBS | 3,091 | 2,539 | 82.1% |
| YTN | 37,260 | 17,896 | 48.0% |
| 중앙일보 | 48,019 | 32,236 | 67.1% |
| 한겨레 | 18,713 | 11,590 | 61.9% |

MBC가 매우 낮은 통과율을 보이는 이유는 2025년 boilerplate 효과 정화(Rule 2). YTN이 약 절반인 이유는 footer 효과 정화(Rule 1). 중앙일보 통과율 추가 감소는 Rule 6 사이버대학 광고 정화 효과(주 -713건).

Rule 6·7 합산 -767건이 예상 -1,306건(749 사이버 + 557 footer)보다 적은 이유: 두 규칙 매칭 행이 약 539건 중복 (사이버대학 광고 다수가 footer 시그너처 동시 보유). 사이버대학 명칭과 footer 3중 조합 모두 가진 행은 한 번만 제외됨.

---

## 5. 검증·디버깅 도구

### Python에서 SQL 확인

```python
from news_cleaning import STRICT_WHERE, CLEANED_CONTENT_SQL
print(STRICT_WHERE)
# WHERE 절을 그대로 SQL에 삽입 가능
```

### 한 행을 cleaning 결과로 확인

```sql
SELECT
  news_id,
  title,
  REPLACE(REPLACE(
    regexp_replace(content, '[^\n]*(AI ?앵커|AI가 대체 못 하는)[^\n]*', '', 'g'),
    '(AI학습 포함)', ''), '(AI 학습 포함)', '') AS cleaned
FROM news_articles
WHERE news_id = '...'
```

### Rule 별로 행 수 측정

각 규칙을 끄고 켜며 비교:

```python
# 모든 규칙 적용
SELECT COUNT(*) FROM news_articles WHERE {STRICT_WHERE}

# Rule 6 (사이버대학) 끈 버전
strict_no_cyber = STRICT_WHERE.replace(
    "AND NOT (content ILIKE '%사이버대학%' OR title ILIKE '%사이버대학%')", ""
)
```

---

## 6. 향후 확장 가이드

새 boilerplate·promo 패턴 발견 시:

1. **본문 끝부분(마지막 200~300자)에 자주 등장하는 정확한 텍스트 추출** — `regexp_extract`로 line 단위 빈도 분석.
2. **GPT 분류 결과와 교차 검증** — false positive가 특정 라벨(Public interest, National security 등)에 비정상 집중하는지 확인.
3. **개별 행 sample (10건 이상)** — 본문 head·tail 동시 확인으로 패턴 위치 검증.
4. **정규식·substring 결정**:
   - 줄 단위 boilerplate → `regexp_replace` with `[^\n]*PATTERN[^\n]*`
   - 정확한 substring (괄호로 묶인 표현 등) → `REPLACE` 2단
   - 광고 시그너처 결합 → 3중 ILIKE AND 조합 (Rule 7 패턴)
   - 기관 명칭 일괄 → 단일 ILIKE substring (Rule 6 패턴)
5. **collateral 측정** — 새 규칙 추가 전후 통과 카운트 차이, 그 차이의 라벨 분포 확인.

새 규칙을 `news_cleaning.py`에 추가하고 본 문서에 한 항목 추가.

---

## 7. 관련 문서

- 공급사 제출용 이슈 정리: [data/exports/news_dataset_issues.md](../data/exports/news_dataset_issues.md)
- 필터링 방법론 전문: [data/exports/news_filtering_process.md](../data/exports/news_filtering_process.md)
- 정화 후 descriptive stats: [data/exports/news_descriptive_strict.md](../data/exports/news_descriptive_strict.md)
- 프로젝트 가이드: [CLAUDE.md](../CLAUDE.md) §"한국 도메스틱 뉴스 Strict AI 필터"
