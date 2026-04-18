# 계획: 문서 다운로드·텍스트 추출 파이프라인 복원

> 작성일: 2026-04-18
> 상태: 초안 (사용자 승인 대기)
> 배경: 소규모 연구용역 보고서 / 연구단체 연구활동 보고서 / 회의록을 DB 내 URL로부터
> 받아서 텍스트로 변환하던 스크립트가 소실됨. 레거시 JSON(`data/txt/22대/report|research/`,
> `data/minutes_txt/`)만 일부 남아있고 커버리지가 불완전함.

## 0. 목표

1. 국회 Open API로 수집된 행 중 **PDF/HWP 링크가 있는 문서**를 재현 가능한 파이프라인으로 수집·추출
2. 출력을 DuckDB 단일 테이블(`document_text`)로 통합 → [PLAN_db_consolidation.md](PLAN_db_consolidation.md) Phase 3~4 패턴 일관성 유지
3. **PDF / HWP / HWPX / 없음**의 모든 경우를 우아하게 처리 (실패 시 상태 레코드 남김)
4. 기존 `data/_archive/` 레거시 JSON은 seed로 재활용 (재다운로드 최소화)

---

## 1. 범위 및 데이터 소스 (IN / OUT)

### IN — 1차 (우선순위 높음)

| # | 테이블 (한글명) | 행수 | URL 컬럼 | 확보 상태 |
|---|---|---|---|---|
| 1 | `ncrwiahparxrpodcv` (연구단체 연구활동 보고서) | 1,740 | `PDF_DOWN_URL` | 컬럼 값 존재 (1,737) |
| 2 | `nfvmtaqoaldzhobsw` (소규모 연구용역 결과보고서) | 1,786 | ⚠ 없음 (FILE_ID만) | **URL 생성 로직 연구 필요** |

### IN — 2차 (선택, 대용량)

| # | 테이블 (한글명) | 행수 | URL 컬럼 | 비고 |
|---|---|---|---|---|
| 3 | `nzbyfwhwaoanttzje` (본회의 회의록) | 26,381 | `PDF_LINK_URL` | `record.assembly.go.kr/.../pdf.do?id=N` |
| 4 | `ncwgseseafwbuheph` (위원회 회의록) | 426,171 | `PDF_LINK_URL` (419,425) | **가장 큰 볼륨** |
| 5 | `ngytonzwavydlbbha` (전원위 회의록) | 113 | `PDF_LINK_URL` | |
| 6 | `vconfsubcconflist` (소위원회 회의록) | 193 | `DOWN_URL` | 기존 `minutes_txt/소위원회`로 이미 일부 있음 |
| 7 | `vconfdetail` (회의록 상세) | 193 | `DOWN_URL` | |
| 8 | `vconfbillconflist` (의안별 회의록) | 177,973 | `DOWN_URL` | 대부분 #3/#4와 중복 |

### OUT (현 계획 제외)

- `data/txt/22대/report/` 의 "연구단체 활동계획서" — 위 8개 테이블에 직접 대응하는 행 없음. 별도 조사 필요
- 법안 PDF — 이미 [download_bills.py](download_bills.py)에서 처리
- **본 계획은 1차(연구 보고서 2종) 먼저, 2차(회의록)는 용량 때문에 사용자 승인 후 별도 실행**

---

## 2. 레거시 아티팩트 활용

`data/txt/22대/research/` (575개) + `data/txt/22대/report/` (213개) + `data/minutes_txt/` (289개) 는 이미 추출된 텍스트. 이를:

1. 신설 `document_text` 테이블로 **먼저 이관** (Phase 0에서 seed 제공, 재다운로드 불필요)
2. 나머지 미커버 행만 실제 다운로드 시도

레거시 JSON 스키마가 소스별로 다름 (§4 참고) — 매핑 테이블 필요.

---

## 3. 스키마 설계

### 단일 통합 테이블 `document_text`

```sql
CREATE TABLE document_text (
    doc_id             VARCHAR NOT NULL,      -- 소스 테이블의 식별자 (FILE_ID / CONF_ID / nttId 등)
    source             VARCHAR NOT NULL,      -- 'research' | 'report' | 'minutes_plenary' |
                                              -- 'minutes_committee' | 'minutes_subcommittee' |
                                              -- 'minutes_committee_of_whole' | 'minutes_bill'
    source_table       VARCHAR,               -- 원천 테이블명 (참조용)
    age                INTEGER,               -- 13~22
    title              VARCHAR,               -- RPT_TITLE / REPORT_TITLE / etc.
    author             VARCHAR,               -- ASBLM_NM / RE_NAME / speaker 등
    doc_date           VARCHAR,               -- YYYY-MM-DD 또는 YYYY
    url                VARCHAR,               -- 원본 URL
    file_format        VARCHAR,               -- 'pdf' | 'hwp' | 'hwpx' | 'unknown'
    file_path          VARCHAR,               -- 로컬 raw 파일 경로 (있으면)
    full_text          TEXT,                  -- 추출된 전체 텍스트
    text_length        INTEGER,               -- full_text 길이
    status             VARCHAR NOT NULL,      -- enum, §5 참고
    error_message      TEXT,                  -- 실패 시 스택트레이스 요약
    extractor_version  VARCHAR,               -- 'fitz-1.0' | 'hwp5-0.1' | 'hwpx-0.1'
    fetched_at         TIMESTAMP,             -- 파일 다운로드 시각
    extracted_at       TIMESTAMP,             -- 텍스트 추출 완료 시각
    PRIMARY KEY (doc_id, source)
);

CREATE INDEX idx_document_text_age   ON document_text(age);
CREATE INDEX idx_document_text_source ON document_text(source);
CREATE INDEX idx_document_text_status ON document_text(status);
```

**PK = `(doc_id, source)`** — 같은 숫자 ID가 여러 소스 테이블에서 쓰일 수 있으므로 분리.

**왜 단일 테이블인가**:
- `bill_text` 패턴과 다른 이유: 문서 종류가 7가지 이상 → per-source 테이블 7개는 과함
- 공통 분석 쿼리가 가능 (예: "22대 전체 문서 전문 검색")
- 필요 시 `v_research_text`, `v_minutes_text` 뷰로 분리

---

## 4. 레거시 JSON → `document_text` 매핑

### `data/txt/*/research/*.json` → `source='research'`
기존 스키마: `{id, category, subcategory, age, date, full_text, title, member}`
매핑:
- `doc_id` ← `id` (FILE_ID)
- `source` ← `'research'`
- `source_table` ← `'nfvmtaqoaldzhobsw'`
- `title` ← `title`, `author` ← `member`, `doc_date` ← `date`
- `full_text` ← `full_text`, `text_length` ← `full_text_length`
- `status` ← `'extracted_ok'` (이미 성공한 것)

### `data/txt/*/report/*.json` → `source='report'` ⚠
기존 스키마: `{id, category, subcategory, age, date, full_text, title, group_name}`
매핑:
- `doc_id` ← `id` (문자열 키 — 보고서 제목 기반이라 길고 충돌 가능성 있음)
- `source` ← `'report'`
- `source_table` ← **대응 테이블 없음** (`nnzoijvcaiexypqaf`(연구단체 활동실적)가 후보이나 스키마 불일치)
- 주의: 이들은 API 테이블과 직접 조인 안 됨. 별도 seed로 보존만

### `data/minutes_txt/*/본회의/*.json` → `source='minutes_plenary'`
기존 스키마: `{conf_id, source, dae_num, conf_date, full_text, speeches}`
- `doc_id` ← `conf_id`
- `source_table` ← `'nzbyfwhwaoanttzje'`
- `age` ← parse_eraco(dae_num)

### `data/minutes_txt/*/소위원회/*.json` → `source='minutes_subcommittee'`
- `doc_id` ← `conf_id`
- `source_table` ← `'vconfsubcconflist'`

### `data/txt/*/conf/*.json` → `source='minutes_plenary'` (중복 확인 필요)
위의 본회의 JSON과 conf_id가 겹칠 수 있음. 이관 시 ON CONFLICT DO NOTHING 또는 full_text 긴 쪽 우선.

---

## 5. 상태 (`status`) 분기

| 값 | 의미 | 재시도? |
|----|------|---------|
| `no_url` | 소스 행에 URL이 비어있음 | 불가 (데이터 부재) |
| `url_404` | HTTP 404/410 | 조건부 (소스 URL 갱신 시) |
| `url_error` | 네트워크·타임아웃·5xx | ○ (재실행 시 자동) |
| `downloaded` | raw 파일만 확보, 아직 추출 전 | ○ (추출 단계만) |
| `extracted_ok` | 텍스트 확보 | skip |
| `extract_failed` | 다운로드 성공, 추출 실패 (HWP 파서 오류 등) | ○ |
| `format_unsupported` | PDF/HWP/HWPX 가 아닌 형식 (예: 이미지 스캔만) | 불가 (수동 검토) |

재시도 로직: `status IN ('url_error', 'extract_failed')` 인 행만 재시도.

---

## 6. 파이프라인 단계

### 단계 A — 포맷 감지
다운로드 직후 파일의 **매직 바이트**로 판정 (URL 확장자 신뢰 금지):
- `%PDF-` → `pdf`
- `\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1` (OLE2) → `hwp` (HWP 5.0)
- `PK\x03\x04` + MIME `application/haansofthwpx` → `hwpx` (또는 zip 구조 확인)
- 기타 → `unknown` → `format_unsupported`

### 단계 B — 다운로드
```python
def download_one(row) -> DownloadResult:
    url = row['url']
    if not url: return DownloadResult(status='no_url')
    try:
        r = requests.get(url, headers=UA, timeout=60, stream=True)
        if r.status_code in (404, 410):
            return DownloadResult(status='url_404')
        r.raise_for_status()
        raw = r.content  # 전량 메모리 — 보고서는 대개 수 MB
        fmt = detect_format(raw)
        if fmt == 'unknown':
            return DownloadResult(status='format_unsupported')
        path = save_to_disk(raw, row['source'], row['doc_id'], fmt)
        return DownloadResult(status='downloaded', path=path, format=fmt)
    except (requests.Timeout, requests.ConnectionError, Exception) as e:
        return DownloadResult(status='url_error', err=str(e))
```

파일 저장 경로:
```
data/docs/
  research/{FILE_ID}.{pdf|hwp|hwpx}
  report/{doc_id}.{pdf|hwp|hwpx}
  minutes/{conf_id}.pdf
```

PDF 원본 디스크 유지는 [PLAN_db_consolidation.md §D.2](PLAN_db_consolidation.md) 결정과 동일 (DB BLOB 지양).

### 단계 C — 텍스트 추출
포맷별 디스패치:

```python
def extract(path: str, fmt: str) -> str:
    if fmt == 'pdf':
        return extract_pdf_fitz(path)       # 기존 download_bills와 동일
    if fmt == 'hwp':
        return extract_hwp_pyhwp(path)
    if fmt == 'hwpx':
        return extract_hwpx_xml(path)
    raise UnsupportedFormatError(fmt)
```

#### HWP 처리 — 라이브러리 선택지

| 라이브러리 | 장점 | 단점 |
|---|---|---|
| **pyhwp** (`hwp5proc`) | 순수 Python, 안정적, pip 설치 | 최신 HWP 서식 일부 누락 가능 |
| **hwp5** (CLI `hwp5txt`) | pyhwp의 CLI 도구, 간편 | 서브프로세스 오버헤드 |
| **hwp-parser** (Node.js) | 최신 | Node 의존 추가 |
| **external: soffice headless** | LibreOffice 변환 → pdf/txt | 무거움, Windows 설치 까다로움 |

**추천**: `pyhwp` Python 라이브러리 사용 + 실패 시 `hwp5txt` CLI fallback.

```python
import subprocess
from hwp5.xmlmodel import Hwp5File  # pyhwp 패키지

def extract_hwp_pyhwp(path: str) -> str:
    try:
        from hwp5.proc import text
        return text.do(Hwp5File(path))
    except Exception:
        # Fallback: CLI
        result = subprocess.run(['hwp5txt', path], capture_output=True, timeout=60)
        if result.returncode == 0:
            return result.stdout.decode('utf-8', errors='replace')
        raise
```

#### HWPX 처리
HWPX = ZIP 안에 XML. `zipfile` + XML 파싱:

```python
import zipfile
import xml.etree.ElementTree as ET

def extract_hwpx_xml(path: str) -> str:
    chunks = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if name.startswith('Contents/section') and name.endswith('.xml'):
                tree = ET.parse(z.open(name))
                for t in tree.iter():
                    if t.text: chunks.append(t.text)
    return '\n'.join(chunks)
```

### 단계 D — DB 저장
```python
UPSERT = """
INSERT INTO document_text (doc_id, source, source_table, age, title, author,
    doc_date, url, file_format, file_path, full_text, text_length,
    status, extractor_version, fetched_at, extracted_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now(), now())
ON CONFLICT (doc_id, source) DO UPDATE SET
    full_text = EXCLUDED.full_text, text_length = EXCLUDED.text_length,
    status = EXCLUDED.status, file_format = EXCLUDED.file_format,
    file_path = EXCLUDED.file_path, extracted_at = now();
"""
```

단일 쓰기 연결 + 락 (기존 `download_bills.py` 패턴).

---

## 7. 단계별 실행 계획

### Phase 0 — `nfvmtaqoaldzhobsw` URL 생성 로직 조사 (30분~2시간)

**미해결 문제**: 이 테이블은 `FILE_ID`만 있고 `PDF_DOWN_URL` 컬럼이 없음.

조사 방법:
1. 국회 사이트에서 `FILE_ID=123003685` 인 보고서를 수동 검색 → 실제 다운로드 URL 패턴 확인
2. `data/txt/22대/research/123006433.json` 의 `id` 필드와 `FILE_ID` 간 관계 검증
3. 가능 후보 URL 패턴:
   - `https://www.assembly.go.kr/portal/bbs/B0000134/view.do?nttId={FILE_ID}` (상세 페이지 → 파일 URL 스크레이핑)
   - `https://www.assembly.go.kr/portal/cmmn/file/fileDown.do?atchFileId=<해시>&fileSn=N` (어딘가에 FILE_ID → 해시 매핑 있음)

조사 결과에 따라:
- (a) 직접 URL 구성 가능 → 바로 1차 실행
- (b) 상세 페이지 스크레이핑 필요 → 추가 requests 단계 넣음
- (c) API/포털로 얻을 수 없음 → `nfvmtaqoaldzhobsw` 는 **레거시 JSON seed만 사용, 추가 다운로드 불가**로 결정

### Phase 1 — 스키마 생성 + 레거시 seed 이관 (30분)
1. `CREATE TABLE document_text` 실행
2. `migrate_legacy_docs.py` — 세 폴더 walk:
   - `data/txt/*/research/` → source=`'research'`
   - `data/txt/*/report/` → source=`'report'`
   - `data/minutes_txt/*/본회의/` → source=`'minutes_plenary'`
   - `data/minutes_txt/*/소위원회/` → source=`'minutes_subcommittee'`
   - `data/txt/*/conf/` → source=`'minutes_plenary'` (conf_id 중복 시 skip)
3. 행수 검증

### Phase 2 — `download_documents.py` 작성 (2시간)
공통 골격:
```python
python download_documents.py --source research [--age 22] [--limit 50] [--workers 4]
python download_documents.py --source report
python download_documents.py --source minutes_plenary
python download_documents.py --all --dry-run  # 어떤 것들이 다운로드될지 미리 보기
```

내부 구조:
- `SOURCES` dict에 각 소스별 (source_table, id_column, url_resolver, metadata_extractor)
- `url_resolver` = `lambda row: row['PDF_DOWN_URL']` 또는 특수 로직 (nfvmtaqoaldzhobsw 의 경우)
- 워커 풀에서 병렬 다운로드, 단일 writer 스레드로 DB 직렬화
- `--resume` (기본) = `status IN ('extracted_ok')` 인 것만 skip. 실패건 (`url_error`, `extract_failed`)은 자동 재시도

### Phase 3 — HWP 처리 검증 (1시간)
- `pyhwp` 설치 (`pip install pyhwp`)
- 실제 HWP 파일 한 건 수동 다운로드 → 추출 테스트
- 실패 시 `hwp5txt` CLI fallback 검증
- HWPX 샘플 찾아서 동일 검증

### Phase 4 — 1차 실행 (연구 보고서, 1~2시간)
- `ncrwiahparxrpodcv` (1,737건) 다운로드·추출
- `nfvmtaqoaldzhobsw` (1,786건) — Phase 0 결과에 따라
- 각 소스별 `status` 분포 리포트

### Phase 5 — 2차 (선택, 회의록) — 사용자 승인 후
- 회의록 총합 ~450K 건 → **시간/디스크 대규모** (건당 평균 2MB PDF 가정 시 ~900GB)
- 먼저 샘플 100건으로 평균 크기 측정 → 전체 예상치 계산 후 사용자 확인
- 대안: 회의록은 `speeches`/`speech_issues` 테이블로 대체 가능하면 Phase 5 생략

### Phase 6 — 문서 갱신
- [CODEBOOK.md](CODEBOOK.md) §13 에 `document_text` 테이블 추가
- [WORKFLOW.md](WORKFLOW.md) 데이터 흐름 다이어그램 갱신
- [CLAUDE.md](CLAUDE.md) 에 `download_documents.py` 캐논 목록 추가

---

## 8. 파일 변경 목록

### 생성
- `download_documents.py` — Phase 2 (캐논 스크립트)
- `migrate_legacy_docs.py` — Phase 1 (일회성)
- `scripts/probe_nfvm_url.py` — Phase 0 (URL 패턴 조사 도구)
- `data/docs/` 디렉토리 — 다운로드된 raw 파일 (gitignore)

### 수정
- `validate_collection.py` — `document_text` 테이블 존재 확인 추가
- `.gitignore` — `data/docs/` 추가
- `CODEBOOK.md`, `WORKFLOW.md`, `CLAUDE.md` — Phase 6

### 삭제 없음
레거시 `data/txt/`, `data/minutes_txt/` 는 이관 후 `data/_archive/` 로 이동 (기존 패턴).

---

## 9. 리스크 및 대응

| 리스크 | 확률 | 대응 |
|---|---|---|
| HWP 파서 실패율 높음 | 중 | `pyhwp`+CLI fallback, `status='extract_failed'` 누적 모니터링 |
| 국회 사이트 rate limit | 중 | `--workers 4` 제한, 재시도 + exponential backoff |
| `nfvmtaqoaldzhobsw` URL 구성 불가 | 중 | Phase 0 조사 결과에 따라 해당 소스만 skip |
| 회의록 450K 다운로드 = 거대 | 고 | Phase 5 사용자 승인 gate |
| `record.assembly.go.kr` 접근 정책 | 저 | User-Agent 설정, 필요 시 세션 쿠키 |

---

## 10. 결정 필요 사항 (사용자 확인)

1. **1차 스코프만 할지, 회의록까지 한 번에 갈지**? 계획은 1차 우선 + 2차 승인 gate로 가정.
2. **HWP 처리 라이브러리**: `pyhwp` 추천 vs 다른 선택지.
3. **Raw 파일 보관**: `data/docs/` 에 원본 PDF/HWP 유지 (권장) vs 텍스트만 남기고 폐기.
4. **`nfvmtaqoaldzhobsw` URL 조사 결과 (b)/(c)인 경우**: 레거시 seed 575건만 쓰고 추가 다운로드 포기 vs 수동 조사 후 재개.
5. **`data/txt/*/report/` 레거시 보고서 (213건)**: API 테이블과 직접 대응 없음. `document_text` 에 seed로만 남길지 / 제외할지.

---

**현재 상태: 초안. 실행 승인 대기.**
