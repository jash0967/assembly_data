# 통합 계획: DB 일관성 복구 + 법안 원문·분류 DB 통합

> 작성일: 2026-04-18
> 상태: 초안 (사용자 승인 대기)
> 작성 근거: 4차 라운드테이블 합의 + 대수 필드 실측 감사
> 관련 문서: [WORKFLOW.md](WORKFLOW.md), [CLAUDE.md](CLAUDE.md), [CODEBOOK.md](CODEBOOK.md)

## 선결 읽기 (fresh agent가 실행 전 반드시 읽을 것)

이 계획만으로는 배경 맥락이 부족합니다. 실행 전에 다음을 순서대로 읽으세요:

1. **[CLAUDE.md](CLAUDE.md)** — 프로젝트 두 워크스트림 개요, 정본 스크립트 리스트, 한국 AI 법안 2단계 필터 핵심 원칙, `replicate_carvao/`·`kr_analysis/` 냉동 폴더 역할
2. **[WORKFLOW.md](WORKFLOW.md)** — 각 소스 수집·필터·분류 파이프라인 상세, 파일 체크리스트, 설계 결정 근거
3. **[CODEBOOK.md](CODEBOOK.md)** — DuckDB 37개 테이블의 API 코드·한글명·컬럼 의미 (`nzmimeepazxkubdpn` 같은 cryptic name이 무엇인지)
4. **이 계획서 전체** — 한 번 통독 후 Phase 0부터 실행

그리고 코드는 최소 다음을 훑어야 합니다:
- [config.py](config.py) — `ApiSpec`, `APIS` 리스트, `AGE_YEAR_RANGE`
- [collector.py](collector.py) — `_generate_tasks`, `save_rows`, strategy 처리 분기
- [classify_bills.py](classify_bills.py) — 2단계 GPT 필터 (`stage1_keyword_filter_kr`, `stage2_gpt_filter_kr`, `AI_FILTER_PROMPT`)
- [bill_loaders.py](bill_loaders.py) — 현재 함수 시그니처 (보존 대상)
- [download_bills.py](download_bills.py) — PDF 크롤링 + fitz 텍스트 추출 흐름

## 0. 목표

1. **대수(age) 일관성**: 13~22대 데이터가 API 제공 범위 내에서 통일된 `age INTEGER` 컬럼 또는 표준화된 대수 식별을 갖도록 정리
2. **법안 원문 DB 통합**: `data/bill_txt_{age}/*.json` (77,000개 파일)을 DuckDB 테이블 `bill_text`로 이관. PDF는 파일시스템 유지
3. **법안 분류 결과 DB 통합**: `data/bills_classified_kr_*.json`, `data/kr_{age}_ai_filtered.json`을 DuckDB 테이블로 이관
4. **미래 수집 자동 일관성**: `ApiSpec` 메타데이터 + write-time age 주입 + 수집 후 validator로 드리프트 차단

## 1. 범위 (IN / OUT)

### IN (이 계획이 다루는 것)
- Assembly Open API 데이터 (`data/assembly.duckdb`)의 대수 필드 표준화
- 법안 원문 JSON → DB 이관
- 법안 분류 JSON → DB 이관
- Stage-2 AI 필터 결과 DB 이관
- `config.py::ApiSpec` 필드 확장
- `collector.py` write-time age 주입
- `validate_collection.py` 신설
- `download_bills.py`, `classify_bills.py`, `bill_loaders.py` 리팩토링
- CODEBOOK·WORKFLOW·CLAUDE.md 갱신

### OUT (이 계획에서 제외, 추후 별도 논의)
- 뉴스 데이터 관련 모든 변경 (Guardian / NYT / Naver)
- PDF 원본 DB 이관 (파일시스템 유지)
- `download_all.py` 전면 재수집
- 분류 프롬프트 변경
- 보고서 (`report_expanded_draft.md`) 내용 변경
- 신규 figure 추가
- 테이블 드롭 (이전 논의에서 철회)

## 2. 현재 상태 진단 (실측 기반)

### 2.1 대수 컬럼 현황

| 분류 | 수 | 상태 |
|------|---|------|
| `AGE/DAE_NUM` 표준 대수 필드 있음 | 11개 | ✓ 정상 |
| `ERACO`, `REGDAESU`, `UNIT_CD` 등 대수 문자열 있음 | 10개 | ✓ 정상, 표준화 여지 |
| 비표준 대수 필드 있음 (`PROFILE_UNIT_CD`, `ORD_NUM`, `DIV`, `YR` 등) | 4개 | ⚠ 컬럼명 표준화 필요 |
| 현직 API, 대수 필드 없음 | 5개 | ⚠ `age=22` 상수 주입 필요 |
| 날짜 기반, 대수 필드 없음 | 3개 | ⚠ 날짜 → 대수 매핑 필요 |
| BILL_ID 기반 lookup 테이블, 대수 미전파 | 1개 | ⚠ 조인 전파 필요 |

### 2.2 세부 테이블별 액션 ⇣

#### A. 현직 API → `age=22` 상수
| 테이블 | 내용 | 행수 | 근거 |
|--------|------|------|------|
| `nwvrqwxyaytdsfvhu` | 의원 인적사항 | 295 | `UNITS: 제22대`, 현직 의원 수 일치 |
| `negnlnyvatsjwocar` | SNS 정보 | 295 | 동일 |
| `nepjpxkkabqiqpbvk` | 정당 의석수 | 9 | 현재 정당 분포 |
| `nxrvzonlafugpqjuh` | 위원회 현황 | 356 | 현 위원회 + 위원장 |
| `nktulghcadyhmiqxi` | 위원회 위원 명단 | 524 | 현 위원들 |

#### B. 비표준 대수 필드 → 표준 `age` 파생
| 테이블 | 원 컬럼 | 파싱 |
|--------|---------|------|
| `nyzrglyvagmrypezq` (위원회 경력) | `PROFILE_UNIT_CD` | `(cd - 100000)` → 정수 |
| `nnzoijvcaiexypqaf` (연구단체 활동) | `DIV` (`제22대 국회`) | regex `제(\d+)대` |
| `nahfbzwvatmaxscwq` (겸직 결정) | `ORD_NUM` (`22대`) | regex `(\d+)대` |

#### C. 날짜 기반 → 대수 매핑
| 테이블 | 날짜 컬럼 | 주의 |
|--------|-----------|------|
| `nbqbmccpamsvwebkn` (정책세미나) | `HOST_DT` | 총선월(5월) 경계 |
| `npbzvuwvasdqldskm` (기자회견) | `TAKING_DATE` | 동일 |
| `nmfcjtvmajsbhhckf` (의정보고서) | `PUBLISH_DT` or `UPDATE_DT` | 동일 |
| `nztwkhgzakucszgls` (사업예산) | `YR` (연도) | 예산은 다년 집행, 복잡 |

#### D. BILL_ID 조인
| 테이블 | 조인 대상 |
|--------|-----------|
| `billinfodetail` (107,300) | `billrcp.BILL_ID` → `ERACO` 파싱, 또는 `nzmimeepazxkubdpn.AGE` |

#### E. 포맷 혼재
- `speeches.dae_num`: `"22"` vs `"제22대"` 혼재 → 통일
- `billrcp.ERACO`: `"제N대"` 정상 + 특수 시기(국가보위입법회의·국가재건최고회의·비상국무회의) 2,283건 → age=-1 플래그 or 별도 분류

## 3. 데이터 흐름 변경 요약

### Before
```
Open API → DuckDB (30+ tables, age 부재 9개, 포맷 혼재)
                ↓  (일부만)
PDF 웹크롤링 → data/bill_pdf_{age}/*.pdf  →  fitz 추출  →  data/bill_txt_{age}/*.json
                                                                    ↓
Classification → data/bills_classified_kr_*.json  + kr_*_ai_filtered.json
                                                                    ↓
                                                      bill_loaders.py (JSON + DB 조인)
                                                                    ↓
                                                      figures/regenerate_all.py
```

### After
```
Open API → DuckDB (모든 per-age 테이블에 age 컬럼, 표준화)
                ↓  (age 자동 주입 by ApiSpec.age_source)
PDF 웹크롤링 → data/bill_pdf_{age}/*.pdf  →  fitz 추출  →  DuckDB::bill_text
                                                                    ↓
Classification → DuckDB::bill_classifications (+ prompt_versions)
              + DuckDB::bill_ai_filter
                                                                    ↓
                                                      v_kr_bills_analysis (통합 뷰)
                                                                    ↓
                                                      bill_loaders.py (DB 쿼리만)
                                                                    ↓
                                                      figures/regenerate_all.py
```

## 4. 단계별 실행 계획

### Phase 0 — 준비 (30분)
1. **백업**
   ```bash
   python -c "import duckdb; con=duckdb.connect('data/assembly.duckdb'); con.execute(\"EXPORT DATABASE 'data/_backup/pre-age-migration-20260418' (FORMAT PARQUET)\"); con.close()"
   cp data/assembly.duckdb data/_backup/assembly.duckdb.20260418.bak
   ```
2. **MCP 서버 정지** — 쓰기 충돌 방지
3. **현재 상태 snapshot**: 각 테이블 row count + age 분포를 `data/_audit/pre_migration.json`에 기록

### Phase 1 — ApiSpec 메타데이터 강화 (1시간, 코드만)

1. **`config.py::ApiSpec` 필드 추가**:
   ```python
   @dataclass(frozen=True)
   class ApiSpec:
       api_id: str
       name_kr: str
       required_params: dict
       strategy: str
       table_name: str
       phase: int = 1
       # 신규
       age_behavior: str           # "per_age" | "current_only" | "by_date" | "by_bill_id" | "ageless"
       age_source: str             # "param:AGE" | "column:ERACO" | "constant:22"
                                   #   | "date:HOST_DT" | "join:billrcp.BILL_ID" | "column:UNIT_CD" | "none"
   ```
2. **37개 ApiSpec에 필드 명시** — 현 실동작을 그대로 기록. 5개 현직 API는 `age_behavior="current_only", age_source="constant:22"`.
3. **`collector.py::save_rows` 수정** — write-time에 `age INTEGER` 컬럼 주입. 소스별 dispatch:
   - `constant:N` → `age = N`
   - `column:ERACO` → regex `제(\d+)대$` → int (특수값은 `-1`)
   - `column:UNIT_CD` → `(int(cd) - 100000)` → int
   - `column:REGDAESU` → int
   - `date:FIELD` → Python `AGE_YEAR_RANGE` 매핑
   - `join:tbl.col` → post-insert phase-2 hook (후처리)
   - `param:AGE` → `task_key`에서 `__AGE_(\d+)` 추출
   - `none` → `age` 컬럼 없이 저장 (ageless 전용)

### Phase 2 — 기존 데이터 Backfill (1시간)

스크립트: `backfill_ages.py` 신설. 한 번만 실행, 이후에는 Phase 1의 write-time 주입이 처리.

1. **A군 5개 테이블**: `ALTER TABLE t ADD COLUMN age INTEGER; UPDATE t SET age = 22 WHERE age IS NULL`
2. **B군 4개 테이블** (비표준 필드):
   ```sql
   ALTER TABLE nyzrglyvagmrypezq ADD COLUMN age INTEGER;
   UPDATE nyzrglyvagmrypezq SET age = CAST(PROFILE_UNIT_CD AS INTEGER) - 100000 WHERE age IS NULL;
   -- 유사하게 nnzoijvcaiexypqaf(DIV), nahfbzwvatmaxscwq(ORD_NUM)
   ```
3. **C군 3개 테이블** (날짜 기반):
   ```python
   def date_to_age(date_str):
       year = int(date_str[:4])
       for age, (start, end) in AGE_YEAR_RANGE.items():
           if start <= year <= end:
               return age
       return None
   ```
   - 경계월(각 대수 시작 5월)은 경고 로그에 기록
4. **D군 BILLINFODETAIL**:
   ```sql
   ALTER TABLE billinfodetail ADD COLUMN age INTEGER;
   UPDATE billinfodetail d
   SET age = (
       SELECT CAST(regexp_extract(r.ERACO, '(\d+)', 1) AS INTEGER)
       FROM billrcp r WHERE r.BILL_ID = d.BILL_ID
   ) WHERE age IS NULL;
   ```
5. **`speeches.dae_num` 포맷 통일**:
   ```sql
   UPDATE speeches SET dae_num = '제' || dae_num || '대'
   WHERE dae_num ~ '^[0-9]+$';
   ```
6. **`billrcp.ERACO` 특수값 처리**: age=-1 + 별도 `ERACO_SPECIAL` 컬럼으로 분류 저장 or 별도 테이블 `billrcp_special`로 격리

### Phase 3 — 법안 원문 DB 이관 (1시간)

1. **스키마 생성**:
   ```sql
   CREATE TABLE bill_text (
       bill_id       VARCHAR PRIMARY KEY,
       age           INTEGER NOT NULL,
       reason_and_content TEXT,
       full_text     TEXT,
       pdf_path      VARCHAR,
       extractor_version VARCHAR DEFAULT 'fitz-1.0',
       extracted_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   );
   ```
2. **Backfill 스크립트** `migrate_bill_text.py`:
   - 4개 age 폴더 순회 (`data/bill_txt_19` ~ `data/bill_txt_22`)
   - 각 JSON 파일에서 `{bill_id, reason_and_content, full_text}` 추출
   - `INSERT INTO bill_text ... ON CONFLICT (bill_id) DO UPDATE` (1000 row commit batch)
   - `pdf_path` = `data/bill_pdf_{age}/PRC_*.pdf` 매핑
3. **`download_bills.py` 수정**:
   - `process_bill()`의 최종 단계에서 JSON write 제거, DB INSERT로 대체
   - resume 검사도 `os.path.exists(txt_path)` 대신 DB 조회
   - 시작 시 `SELECT bill_id FROM bill_text` 한 번에 메모리 로드 (77K bill_id)
4. **JSON 아카이브**: `data/bill_txt_{age}/` → `data/_archive/bill_txt_{age}/` 이동. 3개월 후 삭제 예정

### Phase 4 — 분류 결과 DB 이관 (30분)

1. **스키마 생성**:
   ```sql
   CREATE TABLE prompt_versions (
       version VARCHAR PRIMARY KEY,
       description TEXT,
       released_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   );

   CREATE TABLE bill_classifications (
       bill_id       VARCHAR,
       prompt_version VARCHAR REFERENCES prompt_versions(version),
       primary_attr  VARCHAR,
       secondary_attr VARCHAR,
       tertiary_attr VARCHAR,
       classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
       PRIMARY KEY (bill_id, prompt_version)
   );

   CREATE TABLE bill_ai_filter (
       bill_id       VARCHAR PRIMARY KEY,
       classification VARCHAR CHECK (classification IN ('core', 'adjacent', 'unrelated')),
       stage1_mention_count INTEGER,
       gpt_reason    TEXT,
       ai_provisions TEXT,
       filtered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   );

   CREATE VIEW v_bill_classifications_current AS
   SELECT bc.*
   FROM bill_classifications bc
   JOIN (
       SELECT version FROM prompt_versions ORDER BY released_at DESC LIMIT 1
   ) cur ON bc.prompt_version = cur.version;
   ```
2. **Backfill 스크립트** `migrate_bill_classifications.py`:
   - `prompt_versions`에 `("v2_en_20260418", "영문 v2 프롬프트", now())` 추가
   - `data/bills_classified_kr_{age}.json` 4개 읽어 INSERT (270여건)
   - `data/kr_{age}_ai_filtered.json` 4개 읽어 `bill_ai_filter`로 INSERT
3. **`classify_bills.py` 수정**:
   - `run_target()`의 최종 저장이 JSON write 대신 `INSERT INTO bill_classifications`
   - Stage-2 필터도 `INSERT INTO bill_ai_filter`
   - resume도 DB 조회로
4. **JSON 아카이브**: `data/bills_classified_kr_*.json`, `data/kr_*_ai_filtered.json` → `data/_archive/` 이동

### Phase 5 — `bill_loaders.py` 리팩토링 + 소비자 검증 (1시간)

1. **통합 분석 뷰 생성**:
   ```sql
   CREATE VIEW v_kr_bills_analysis AS
   SELECT
       b.bill_id, b.age, b.bill_name, b.proposer, b.lead_proposer,
       b.propose_date, b.committee, b.proc_result,
       t.reason_and_content, t.full_text,
       c.primary_attr, c.secondary_attr, c.tertiary_attr,
       f.classification AS ai_relevance,
       f.gpt_reason, f.ai_provisions
   FROM v_bill b
   LEFT JOIN bill_text t USING (bill_id)
   LEFT JOIN v_bill_classifications_current c USING (bill_id)
   LEFT JOIN bill_ai_filter f USING (bill_id);
   ```
2. **`bill_loaders.py` 내부 교체**:
   - `load_kr_bills()` 함수 시그니처 유지
   - 내부를 JSON 파일 순회 → `con.execute("SELECT * FROM v_kr_bills_analysis WHERE age IN ...").fetchall()`로 변경
   - 반환 타입 list[dict]는 유지 (소비자 호환성)
3. **소비자 검증**:
   - `python figures/regenerate_all.py` 실행 → fig01~05 정상 재생성 확인
   - `python replicate_carvao/gen_us_report.py` — 미국은 변경 안 되었으므로 그대로
   - `python kr_analysis/validate_tfidf_lda.py` — 변경된 로더로 작동 확인

### Phase 6 — 수집 후 Validator (30분)

신설 `validate_collection.py`:
```python
def validate(con):
    errors = []
    for spec in APIS:
        if spec.age_behavior == "per_age":
            null_count = con.execute(
                f'SELECT COUNT(*) FROM "{spec.table_name}" WHERE age IS NULL'
            ).fetchone()[0]
            if null_count > 0:
                errors.append(f"{spec.table_name}: {null_count} NULL ages")

        if spec.age_behavior == "current_only":
            distinct_ages = con.execute(
                f'SELECT COUNT(DISTINCT age) FROM "{spec.table_name}"'
            ).fetchone()[0]
            if distinct_ages > 1:
                errors.append(f"{spec.table_name}: expected 1 age, got {distinct_ages}")
    # ... 외 각종 체크
    return errors
```

`download_all.py` 마지막 단계에서 호출, 실패 시 exit 1.

### Phase 7 — 문서화 (30분)

1. **CODEBOOK.md** 갱신:
   - 각 테이블 옆에 "AGE source: ERACO parse / UNIT_CD / constant:22 / BILL_ID join / date:YYYY-MM" 표시
   - 신규 테이블 `bill_text`, `bill_classifications`, `bill_ai_filter`, `prompt_versions` 추가
2. **WORKFLOW.md** 갱신:
   - 데이터 흐름 다이어그램 After 버전으로 교체
   - Phase 변경 이력 추가
3. **CLAUDE.md** 갱신:
   - DB 통합 완료 명시
   - `bill_loaders.py`는 DB 쿼리 얇은 wrapper
4. **`duckdb_mcp_server.py::get_overview()`**:
   - 신규 테이블·뷰 안내 추가
   - AI 정책 분석 섹션: "DuckDB의 `v_kr_bills_analysis` 사용" 명시

## 5. 예상 소요 및 리스크

| Phase | 소요 | 주 리스크 | 대응 |
|-------|------|-----------|------|
| 0 준비 | 30분 | — | — |
| 1 ApiSpec 확장 | 1시간 | 37개 API 현 동작 오해 | 실측 기반 선언 |
| 2 Backfill SQL | 1시간 | 날짜 경계월 오판 | 경고 로그 + 수동 검토 |
| 3 bill_text 이관 | 1시간 | 77K row 삽입 속도 | 1000-row batch commit |
| 4 분류 이관 | 30분 | prompt_version FK 꼬임 | 선삽입 순서 고정 |
| 5 loader 리팩토링 | 1시간 | figures 깨짐 | 즉시 검증 실행 |
| 6 validator | 30분 | — | — |
| 7 문서화 | 30분 | — | — |
| **총** | **~5시간** | | |

- **네트워크 사용량**: 0 (재수집·재크롤링 없음)
- **다운타임**: MCP 서버 정지 ~5시간
- **롤백**: Phase 0 backup에서 DB 파일 복원 + `data/_archive/` JSON 파일 복원

## 6. 검증 체크리스트

Phase 7 완료 후 확인:

- [ ] 모든 `per_age` 테이블 `COUNT(age IS NULL) = 0`
- [ ] 모든 `current_only` 테이블 `distinct age = 1`
- [ ] `bill_text` 행수 ≈ 77,000 (기존 JSON 파일 수와 ±50 이내)
- [ ] `bill_classifications` 행수 = 기존 `bills_classified_kr_*.json`의 모든 엔트리 합계
- [ ] `v_kr_bills_analysis` 쿼리로 22대 200건 확인 (보고서 수치와 일치)
- [ ] `figures/regenerate_all.py` 5개 그림 재생성, 시각적 동등성
- [ ] `duckdb_mcp_server.py`의 `list_tables()` 에 신규 테이블 나타남
- [ ] `_backup/` 존재 + 원본 복원 가능

## 7. 성공 기준 (이 계획이 달성하려는 것)

1. ✓ 13~22대 데이터가 **한 개의 `age INTEGER` 컬럼 표준**으로 조회 가능
2. ✓ 향후 `download_all.py` 재실행 시 새 레코드도 자동으로 age 부여됨 (write-time 주입)
3. ✓ validator가 드리프트 발생 시 **CI 수준으로 알림** (exit 1)
4. ✓ 법안 원문이 SQL로 검색 가능 (keyword × 속성 × 대수 cross-tab)
5. ✓ 분류 결과 버전 관리 (prompt_version) — 향후 프롬프트 개선 시 과거와 비교 가능
6. ✓ `bill_loaders.py`가 JSON-DB 이원 접근을 더 이상 하지 않음
7. ✓ 기존 보고서 figures 모두 그대로 재생성됨 (scope 증명)

## 8. 실행 후 이 계획의 유효 기간

- 이 계획은 **실행 한 번으로 일회성 완료**가 목표
- 이후 수집·분류 작업은 Phase 1의 `ApiSpec` + Phase 6의 `validator`로 자동 보장
- 뉴스 DB 이관, 신규 분석 축(Fig 6, 7) 등은 별도 계획으로 분리

---

## 부록 A. ApiSpec 전수표 (37개 API × age_behavior × age_source)

실행 단계 Phase 1에서 각 `ApiSpec`에 아래 값을 정확히 선언할 것. 실측 DB 감사 기반.

`age_behavior` 값 정의:
- `per_age` — 대수별로 독립 레코드 존재 (13~22 중 API 제공 범위)
- `current_only` — API가 현직(22대)만 반환
- `by_date` — 대수 아닌 날짜로 구분, 사후 매핑 필요
- `by_bill_id` — lookup 테이블, 모 테이블에서 age 전파
- `ageless` — 대수와 무관한 데이터 (없음; 방어용)

`age_source` 값 정의:
- `param:FIELD` — 수집 시 API 파라미터로 전달된 대수 (task_key `__AGE_{n}` 파싱)
- `column:FIELD` — 응답에 이미 들어오는 컬럼 파싱
- `constant:N` — 상수 부여
- `date:FIELD` — 날짜 컬럼을 `AGE_YEAR_RANGE`로 매핑
- `join:TABLE.COL` — 조인 기반 (Phase 2 후처리)
- `none` — 대수 없음 (ageless 전용)

| # | api_id | 한글명 | 현재 strategy | **age_behavior** | **age_source** | 비고 |
|---|--------|--------|---------------|-----------------|----------------|------|
| 1 | nwvrqwxyaytdsfvhu | 의원 인적사항 | none | current_only | constant:22 | API가 현직만 반환 |
| 2 | nexgtxtmaamffofof | 의원 이력 | none | current_only | column:UNIT_CD | UNIT_CD=100022 |
| 3 | nyzrglyvagmrypezq | 위원회 경력 | none | current_only | column:PROFILE_UNIT_CD | 22대 의원 경력 |
| 4 | nzmimeepazxkubdpn | 발의법률안 | age | per_age | param:AGE | 13~22 |
| 5 | nuvypcdgahexhvrjt | 상임위 활동 | age | per_age | param:DAE_NUM | 18~22 (API 한계) |
| 6 | negnlnyvatsjwocar | SNS 정보 | none | current_only | constant:22 | 현직만 |
| 7 | nbqbmccpamsvwebkn | 정책 세미나 | year_host | by_date | date:HOST_DT | 연도 → 대수 매핑 |
| 8 | numwhtqhavaqssfle | 연구단체 등록 | age | per_age | param:REGDAESU | 16~22 |
| 9 | npbzvuwvasdqldskm | 기자회견 | year | by_date | date:TAKING_DATE | 연도 → 대수 |
| 10 | nojepdqqaweusdfbi | 표결정보 | lookup_bill_age | per_age | param:AGE | 20~22 (API 한계) |
| 11 | ncocpgfiaoituanbr | 의안별 표결현황 | age | per_age | param:AGE | 20~22 |
| 12 | BILLRCP | 의안 접수목록 | none | per_age | column:ERACO | 10~22 + 특수값 |
| 13 | BILLINFODETAIL | 의안 상세정보 | lookup_bill | by_bill_id | join:billrcp.BILL_ID | Phase 2 후처리 |
| 14 | nzivskufaliivfhpb | 역대 의안 통계 | none | per_age | column:ERACO | 10대~ |
| 15 | nvqbafvaajdiqhehi | 청원 계류 | none | current_only | constant:22 | API 현직만 |
| 16 | ncryefyuaflxnqbqo | 청원 처리 | age | per_age | param:AGE | 13~22 |
| 17 | nepjpxkkabqiqpbvk | 정당 의석수 | none | current_only | constant:22 | 현 정당 분포 |
| 18 | nxrvzonlafugpqjuh | 위원회 현황 | none | current_only | constant:22 | 현 위원회 |
| 19 | nktulghcadyhmiqxi | 위원회 위원 명단 | none | current_only | constant:22 | 현 위원 |
| 20 | ndiwuqmpambgvnfsj | 위원회 계류법률안 | committee | current_only | constant:22 | API 현직만 |
| 21 | nwbpacrgavhjryiph | 본회의 처리안건 | age | per_age | param:AGE | 13~22 |
| 22 | nrvsawtaauyihadij | 인사청문회 | none | per_age | param:AGE | 20~22 |
| 23 | nqfvrbsdafrmuzixe | 날짜별 의정활동 | daily | per_age | param:AGE | 13~22 (daily) |
| 24 | ngytonzwavydlbbha | 전원위 회의록 | age_year | per_age | param:DAE_NUM | 16, 17, 21만 |
| 25 | nztwkhgzakucszgls | 사업예산 | none | by_date | date:YR | 2019~, 대수 경계 애매 |
| 26 | nzbyfwhwaoanttzje | 본회의 회의록 | age_year | per_age | param:DAE_NUM | 13~22 |
| 27 | ncwgseseafwbuheph | 위원회 회의록 | age_year | per_age | param:DAE_NUM | 13~22 |
| 28 | VCONFSUBCCONFLIST | 소위원회 회의록 | none | per_age | column:ERACO | 16~22 |
| 29 | VCONFDETAIL | 회의록 상세정보 | lookup_conf | per_age | column:ERACO | VCONFSUBC에서 조인 유입 |
| 30 | VCONFBILLCONFLIST | 의안별 회의록 | lookup_bill | per_age | column:ERACO | 20~22 |
| 31 | nxcxrdmpaonzzbkic | 외교협의회 | none | per_age | column:UNIT_CD | 현재 20대만 수집됨 |
| 32 | nbicgazsalnfamoyp | 친선협회 | none | per_age | column:UNIT_CD | 20~22 |
| 33 | nahfbzwvatmaxscwq | 겸직 결정 | none | per_age | column:ORD_NUM | `22대` 포맷 파싱 |
| 34 | nnzoijvcaiexypqaf | 연구단체 활동 실적 | none | per_age | column:DIV | `제22대 국회` 파싱 |
| 35 | nmfcjtvmajsbhhckf | 의정보고서 | none | by_date | date:PUBLISH_DT | 날짜 → 대수 |
| 36 | nfvmtaqoaldzhobsw | 연구용역 결과보고서 | age_unit | per_age | column:UNIT_CD | 20~22 |
| 37 | ncrwiahparxrpodcv | 연구단체 연구활동 | age | per_age | param:REGDAESU | 16~22 |

### 표 해석 요지
- **per_age 29개** — 대수가 명확한 API. param 또는 column 기반 age 추출
- **current_only 8개** — API가 현직만 반환하므로 `age=22` 상수
- **by_date 4개** — 날짜 기반이라 사후 매핑 (Phase 2)
- **by_bill_id 1개** (BILLINFODETAIL) — 모 테이블 조인 (Phase 2 후처리)

## 부록 B. 엣지케이스 처리 규칙

### B.1 ERACO 문자열 파싱
정규식: `^제(\d+)대$`

```python
import re
def parse_eraco(eraco: str) -> int | None:
    if not eraco:
        return None
    m = re.match(r"^제(\d+)대$", eraco.strip())
    if m:
        return int(m.group(1))
    return None  # 특수값 처리로 넘어감
```

**특수값 매핑 테이블** (BILLRCP에만 존재, 2,283건):
| ERACO 원문 | age 값 | 비고 |
|-----------|--------|------|
| `국가보위입법회의` | `-3` | 1980-1981, 10대-11대 사이 |
| `국가재건최고회의` | `-2` | 1961-1963, 5대-6대 사이 |
| `비상국무회의` | `-1` | 1972-1973, 유신 |

**방침**: 특수값은 음수 age로 저장하고 `billrcp_special_mapping` 참조 테이블 별도 생성. 일반 분석 쿼리는 `WHERE age >= 1` 필터로 정상 국회만 대상.

### B.2 날짜 → 대수 매핑
기준 테이블: `config.py::AGE_YEAR_RANGE`

```python
# config.py 이미 있음
AGE_YEAR_RANGE: dict[int, tuple[int, int]] = {
    13: (1988, 1992), 14: (1992, 1996), 15: (1996, 2000),
    16: (2000, 2004), 17: (2004, 2008), 18: (2008, 2012),
    19: (2012, 2016), 20: (2016, 2020), 21: (2020, 2024),
    22: (2024, 2028),
}
```

**경계월 규칙**:
- 국회 임기 시작일은 **매년 5월 30일**
- 날짜 기반 매핑 시:
  - 해당 연도 **1월 1일 ~ 5월 29일**: 이전 대수
  - 해당 연도 **5월 30일 이후**: 새 대수
- 예: `2024-04-15` → 21대, `2024-06-01` → 22대

```python
from datetime import date
def date_to_age(d: date) -> int | None:
    y = d.year
    # 5월 30일 이전이면 이전 대수 기준 연도로 계산
    if d.month < 5 or (d.month == 5 and d.day < 30):
        effective_year = y
        # 이전 대수 탐색
        for age, (start, end) in AGE_YEAR_RANGE.items():
            if start <= effective_year <= end and effective_year < end:  # 시작연도-대수 매핑
                return age
    else:
        effective_year = y
        for age, (start, end) in AGE_YEAR_RANGE.items():
            if start <= effective_year <= end:
                return age
    return None
```

**검증 쿼리** (Phase 2 후):
```sql
-- 날짜 기반 매핑 결과 샘플 확인
SELECT age, MIN(HOST_DT), MAX(HOST_DT), COUNT(*)
FROM nbqbmccpamsvwebkn GROUP BY age ORDER BY age;
-- age=N 의 날짜 범위가 AGE_YEAR_RANGE[N]과 일치해야 함
```

### B.3 UNIT_CD 파싱
`UNIT_CD` 값은 `100013`~`100022` 형식. 파싱: `int(unit_cd) - 100000`.

```python
def parse_unit_cd(unit_cd: str) -> int | None:
    if not unit_cd or not unit_cd.isdigit():
        return None
    n = int(unit_cd)
    if 100001 <= n <= 100099:
        return n - 100000
    return None
```

### B.4 `speeches.dae_num` 혼재 포맷 통일
```sql
-- 형식 A: "22" (숫자만) → "제22대"로 통일
-- 형식 B: "제22대" → 그대로 유지
UPDATE speeches
SET dae_num = '제' || dae_num || '대'
WHERE dae_num ~ '^[0-9]+$';

-- 검증
SELECT dae_num, COUNT(*) FROM speeches GROUP BY dae_num ORDER BY dae_num;
-- 모든 값이 '제\d+대' 형식이어야 함
```

### B.5 `ORD_NUM` / `DIV` 파싱 (겸직·연구단체)
```python
# nahfbzwvatmaxscwq.ORD_NUM = "22대"
# nnzoijvcaiexypqaf.DIV = "제22대 국회"
def parse_age_korean(s: str) -> int | None:
    m = re.search(r"(\d+)대", s or "")
    return int(m.group(1)) if m else None
```

### B.6 BILLINFODETAIL age 조인 전파
```sql
UPDATE billinfodetail d
SET age = (
    SELECT CASE
        WHEN r.ERACO ~ '^제\d+대$' THEN CAST(regexp_extract(r.ERACO, '(\d+)', 1) AS INTEGER)
        ELSE -1  -- 특수값
    END
    FROM billrcp r
    WHERE r.BILL_ID = d.BILL_ID
    LIMIT 1
)
WHERE age IS NULL;
```
- 조인 실패한 BILL_ID가 있을 수 있음 → age=NULL 잔존 허용 (Phase 6 validator가 감지해서 경고)

### B.7 `prompt_versions` 명명 규칙
- 포맷: `v{major}_{lang}_{YYYYMMDD}`
- 예: `v2_en_20260418` = 영문 v2 프롬프트, 2026-04-18 릴리스
- 기존 분류 JSON은 모두 `v2_en_20260418`로 migration (한 번에)
- 미래 프롬프트 수정 시 새 version 발급 + `INSERT INTO prompt_versions`
- `v_bill_classifications_current` 뷰는 자동으로 최신 참조

## 부록 C. 보존해야 할 기존 함수 시그니처

Phase 5에서 `bill_loaders.py` 내부를 DB 쿼리로 교체하되, **인터페이스는 절대 바꾸지 말 것**. 소비자 다수가 의존.

```python
# bill_loaders.py 현재 시그니처 — 유지
def load_kr_bills(
    ages: Iterable[int] = (19, 20, 21, 22),
    enrich: bool = True,
) -> list[dict]:
    """
    각 dict 키:
      primary, secondary, tertiary, id, title, age,
      (enrich=True일 때) propose_date, proposer, committee
    """

def load_us_bills(
    congresses: Iterable[int] = (118, 119),
    enrich: bool = True,
) -> list[dict]:
    """... """

def load_eu_bills(
    include: Iterable[str] = ("act", "amendments"),
    enrich: bool = True,
) -> list[dict]:
    """..."""
```

**변경 금지**:
- 함수명, 파라미터 이름·기본값, 반환 타입(list[dict])
- dict 키 이름 (primary, secondary, tertiary, id, title, age, propose_date, proposer, committee)

**내부 변경 OK**:
- 파일 순회 로직 → SQL 쿼리로 교체
- `data/bills_classified_kr_*.json` 읽기 → `SELECT FROM bill_classifications`
- `data/bill_txt_*/*.json` 메타 추출 → `SELECT FROM bill_text JOIN v_bill`

**검증**: Phase 5 후 `figures/regenerate_all.py`와 `kr_analysis/validate_tfidf_lda.py` 실행해서 예외 없이 완료해야 함.

`classify_bills.py`와 `download_bills.py`는 내부 구현 변경 OK. CLI 동작은 보존:
- `python classify_bills.py all` — 동일 출력 (단 JSON 대신 DB에 저장)
- `python download_bills.py --age 22` — 동일 동작 (단 JSON 대신 DB)

## 부록 D. 결정 로그 (왜 이렇게 설계됐는가)

### D.1 뉴스는 제외 (사용자 지시)
2026-04-18 세션에서 사용자 명시: "뉴스는 아직 고려 대상이 아니야. 계획에서 완전히 제외해."
→ Guardian/NYT/Naver 관련 모든 변경 배제. `news_*_classified.json` 건드리지 않음.

### D.2 PDF는 파일시스템 유지
- 규모: `data/bill_pdf_{age}/` 총 ~77,000 파일, 아마 1~2GB
- 4차 라운드테이블에서 DB Engineer·Architect·Analyst 전원 동의: PDF BLOB 저장은 DB 성능·백업 부담
- 텍스트 추출 결과만 DB화. PDF는 원본 아카이브로 disk 유지

### D.3 14개 테이블 드롭 철회
3차 라운드에서 Analyst가 제안한 "AI 신호 희박 14 테이블 드롭"은 사용자가 2026-04-18 세션에서 명시 철회:
"분류추가도 필요없고, 제거할 것은 하나도 없어."
→ 모든 기존 테이블 유지. 드롭 안 함.

### D.4 전면 재수집 거부
4차 라운드의 Architect는 "drop and redownload" 제안. 그러나 Pragmatist의 경고 수용:
- `download_all.py` 재실행 2-5일, 쿼터 이슈
- 현재 보고서 `report_expanded_draft.md`는 이미 렌더됨
- 재수집은 사용자 통증의 ~30%만 해결하고 100% 비용
→ **재수집 없음**. Phase 1의 코드 변경(write-time age 주입) + Phase 2의 backfill로 충분

### D.5 prompt_version 도입
- 현재 분류 결과는 단일 프롬프트 버전 기반. 그러나 향후 프롬프트 개선 가능성
- 복합 PK `(bill_id, prompt_version)`로 old vs new 공존 지원
- 일상 쿼리는 `v_bill_classifications_current` 뷰로 friction 없음

### D.6 Stage-2 필터 캐시도 DB로
- 이전 논의에서 Analyst "JSON 유지도 OK"라 판단
- 그러나 DB 통합 일관성 위해 `bill_ai_filter` 테이블로 이관 결정
- 크기 작음 (수백 행), 일관성 이득 > 유지 비용

### D.7 `bill_loaders.py` 삭제 X, 내부 교체 O
- 4차 라운드 Analyst "list[dict] 반환 편의 유지"
- 함수 시그니처 보존 + 내부만 DB 쿼리로 교체
- 소비자 (figures, reports, validators) 코드 변경 불필요

### D.8 ERACO 특수값은 age=-1, -2, -3 분리
- BILLRCP에 국가보위입법회의 등 2,283건 — 버리기엔 아깝고 섞기엔 위험
- age가 -1, -2, -3으로 음수면 일반 쿼리 `WHERE age >= 1`에서 자동 배제
- 필요 시 `WHERE age < 0` 로 특수 분석 가능

## 부록 E. Phase별 검증 SQL

### Phase 0 (백업 후) — 현재 상태 스냅샷
```sql
-- 행수 기록
COPY (
    SELECT table_name,
           (SELECT COUNT(*) FROM duckdb_tables t2 WHERE t2.table_name = t.table_name) AS exists_check
    FROM duckdb_tables t
    WHERE schema_name = 'main'
    ORDER BY table_name
) TO 'data/_audit/pre_migration_tables.csv';

-- 대수별 행 분포
COPY (
    SELECT 'v_bill' AS t, age, COUNT(*) AS n FROM v_bill GROUP BY age
    UNION ALL
    SELECT 'speeches', dae_num, COUNT(*) FROM speeches GROUP BY dae_num
    -- ... 각 테이블
) TO 'data/_audit/pre_migration_age_dist.csv';
```

### Phase 1 (ApiSpec 확장 후)
```python
# 모든 ApiSpec이 age_behavior/age_source 선언했는지 검증
from config import APIS
assert all(hasattr(s, 'age_behavior') and hasattr(s, 'age_source') for s in APIS)
assert all(s.age_behavior in ('per_age','current_only','by_date','by_bill_id','ageless') for s in APIS)
# 37개 전수 확인
assert len(APIS) == 37
```

### Phase 2 (Backfill 후)
```sql
-- 1. 모든 current_only 테이블: age=22 단일값
SELECT 'nwvrqwxyaytdsfvhu', COUNT(DISTINCT age), MIN(age), MAX(age) FROM nwvrqwxyaytdsfvhu
UNION ALL
SELECT 'negnlnyvatsjwocar', COUNT(DISTINCT age), MIN(age), MAX(age) FROM negnlnyvatsjwocar;
-- 기대: COUNT(DISTINCT)=1, MIN=MAX=22

-- 2. per_age 테이블 대수 분포 (예: v_bill은 원래와 동일해야)
SELECT age, COUNT(*) FROM v_bill GROUP BY age ORDER BY age;

-- 3. speeches.dae_num 포맷 일관성
SELECT COUNT(*) FROM speeches WHERE dae_num ~ '^[0-9]+$';
-- 기대: 0 (모두 '제N대' 포맷)

-- 4. BILLINFODETAIL age 전파
SELECT COUNT(*) FROM billinfodetail WHERE age IS NULL;
-- 기대: 작은 수 (조인 실패 고아)
```

### Phase 3 (bill_text 이관 후)
```sql
-- 행수 일치 검증 (77K 기대)
SELECT age, COUNT(*) FROM bill_text GROUP BY age ORDER BY age;
-- 파일 수와 비교:
--   ls data/bill_txt_19/*.json | wc -l 결과와 각 age 행수 일치

-- 샘플 텍스트 확인
SELECT bill_id, LENGTH(full_text), SUBSTR(reason_and_content, 1, 100)
FROM bill_text WHERE age = 22 LIMIT 3;
```

### Phase 4 (분류 이관 후)
```sql
-- 각 대수별 분류 건수
SELECT b.age, COUNT(DISTINCT c.bill_id)
FROM v_bill b JOIN bill_classifications c USING (bill_id)
WHERE c.prompt_version = 'v2_en_20260418'
GROUP BY b.age ORDER BY b.age;
-- 기대: 22대 200, 21대 58, 20대 13, 19대 0

-- Stage-2 필터 카운트
SELECT classification, COUNT(*) FROM bill_ai_filter GROUP BY classification;
-- 기대: core, adjacent, unrelated 각각 존재

-- 현재 버전 뷰 작동
SELECT COUNT(*) FROM v_bill_classifications_current;
```

### Phase 5 (loader 교체 후)
```bash
# 소비자 스크립트가 예외 없이 실행
python figures/regenerate_all.py 2>&1 | tail -10
# 기대: "All figures regenerated."

python kr_analysis/validate_tfidf_lda.py 2>&1 | head -20
# 기대: 예외 없이 진행 (출력은 이전과 유사)
```

### Phase 6 (validator 신설 후)
```bash
python validate_collection.py
echo "Exit code: $?"
# 기대: 0 (모든 검증 통과)

# 일부러 깨뜨려서 exit 1 확인
# duckdb: UPDATE v_bill SET age = NULL WHERE bill_id = 'PRC_xxx';
python validate_collection.py
# 기대: exit 1 + 명확한 에러 메시지
```

### Phase 7 (문서화 후)
- `CODEBOOK.md` 각 테이블 섹션에 "AGE source: ..." 줄 추가됨
- `WORKFLOW.md` 데이터 흐름 다이어그램 After 버전으로 교체됨
- `CLAUDE.md` DB 통합 완료 명시

## 부록 F. 파일 변경 목록

### 생성
- `validate_collection.py` — Phase 6
- `backfill_ages.py` — Phase 2 (일회성)
- `migrate_bill_text.py` — Phase 3 (일회성)
- `migrate_bill_classifications.py` — Phase 4 (일회성)
- `data/_backup/pre-age-migration-20260418/` — Phase 0 backup (디렉토리)
- `data/_audit/pre_migration_tables.csv` 등 — Phase 0 snapshot

### 수정
- `config.py` — ApiSpec에 age_behavior/age_source 필드 추가, 37 API 선언
- `collector.py` — save_rows에 write-time age 주입 분기
- `download_bills.py` — JSON write → DB INSERT, resume 검사 DB 기반
- `classify_bills.py` — JSON write → DB INSERT (내부 구현만)
- `bill_loaders.py` — 내부 구현 DB 쿼리로 교체 (시그니처 보존)
- `duckdb_mcp_server.py` — get_overview() 텍스트 갱신
- `CODEBOOK.md`, `WORKFLOW.md`, `CLAUDE.md` — Phase 7

### 이동 (아카이브)
- `data/bill_txt_{19,20,21,22}/` → `data/_archive/bill_txt_{19,20,21,22}/` — Phase 3 후
- `data/bills_classified_kr_{19,20,21,22}.json` → `data/_archive/` — Phase 4 후
- `data/kr_{19,20,21,22}_ai_filtered.json` → `data/_archive/` — Phase 4 후

### 삭제 없음
이 계획은 기존 테이블·데이터를 삭제하지 않음. 이동만 함. 3개월 후 사용자 승인 하에 아카이브 삭제 가능.

---

**현재 상태: 초안. 실행 승인 대기.**

실행 시 각 Phase를 독립 커밋으로 나눠 진행. Phase 3, 4, 5 순서는 엄격 (text → classification → loader).

Fresh agent는 이 문서 + 선결 읽기 자료(CLAUDE.md, WORKFLOW.md, CODEBOOK.md)로 독립 실행 가능. 엣지케이스 또는 예상 외 상황 발생 시 부록 B의 규칙 재확인, 필요 시 사용자 확인.
