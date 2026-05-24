# rag_assembly — 국회 데이터 RAG 시스템

국회 법안·회의록·발언·의원 프로필을 의미 기반으로 검색하는 RAG 인프라.
analyze·collect·figures와 동급의 root 폴더로 위치.

작성: 2026-05-10. 본 문서는 변경 시 갱신.

---

## 1. 목적과 범위

### 다루는 것

`assembly_raw.duckdb`의 다음 텍스트를 임베딩·검색 가능하게 함:

| 소스 | DB 출처 | 행수 (대략) | 임베딩 단위 |
|---|---|---:|---|
| `bill` | `raw.bill_text.full_text` + `reason_and_content` | 77K | 800자 청크 (긴 법안은 split) |
| `bill_meta` | `raw.v_bill` | 97K | 1 법안 = 1 청크 (제목·발의자·위원회 카드) |
| `document` | `raw.document_text.full_text` | 26K → 약 2.5M 청크 | 800자 청크 (회의록은 발언자 마커 우선 split) |
| `speech` | `raw.speeches.text` | 84K | 발언 1건 = 1 청크 |
| `member` | `raw.v_member` | 295 | 1 의원 = 1 청크 (이름·정당·지역·위원회) |

총 약 **3.5M 청크**, **약 5.25 GB** (float16 1536-dim).

### 제외하는 것 (의도적)

- **뉴스** (`articles_classified_*.json`) — 별도 코퍼스로 유지. 향후 추가 가능 (증분 임베딩으로 약 $1)
- **`raw.nfvmtaqoaldzhobsw` (소규모 연구용역)** 등 본문 추출 실패 행은 자동 제외 (`document_text`에서 빠짐)
- 분석 산출물 (`bill_classifications` 등) — 메타필터로 활용은 하지만 임베딩 대상 아님

---

## 2. 기술 스택

| 컴포넌트 | 선택 | 사유 |
|---|---|---|
| 벡터 DB | **LanceDB 0.30+** (float16) | ChromaDB 1.3M 청크에서 OOM. LanceDB는 lazy load + 양자화 지원 |
| 임베딩 모델 | **Gemini embedding-001** (Vertex AI) | 한국어 우수, Matryoshka 1536-dim |
| 차원 | 1536 (full) | float16으로 메모리 절반 절약 (정확도 < 0.5% 손실) |
| Multi-region | **8 regions** rotation | 각 region 1M TPM × 8 = 8M TPM |
| 인증 | gcloud ADC (서비스 계정 키 X) | 기관 GCP 정책이 키 생성 차단 (`iam.disableServiceAccountKeyCreation`) |
| BM25 | rank_bm25 + kiwipiepy | 한국어 형태소 분석 (NNG/NNP/VV/VA/SL/SN 보존) |
| Reranker | bge-reranker-v2-m3 | 다국어 cross-encoder, GPU 자동 사용 |
| 결합 | RRF (k=60) | 벡터 30 + BM25 30 → 60 → rerank → top 10 |

**Vertex AI 프로젝트**: `project-21bfdbb5-2abd-4c9d-9c6`
**EMBED_CONFIG_VERSION**: `v2_lancedb_fp16_20260510`

---

## 3. 디렉토리 구조

```
rag_assembly/
├── README.md                   ← 본 문서
├── _bootstrap.py               sys.path 자동 처리 (root + collect 추가)
├── config.py                   상수 (EMBED_*, LANCE_DIR, ENABLED_REGIONS 등)
├── chunker.py                  소스별 청킹 전략
├── embedder.py                 Vertex AI gemini-embedding-001 wrapper (multi-region)
├── manifest.py                 SQLite 청크·임베딩 추적 (idempotent)
├── vectordb.py                 LanceDB wrapper (ChromaDB 호환 인터페이스)
├── bm25.py                     BM25 인덱스 (kiwipiepy + rank_bm25)
├── reranker.py                 bge-reranker-v2-m3
├── search.py                   하이브리드 검색 (벡터 + BM25 + RRF + rerank)
├── indexer.py                  전체 인덱싱 진입점
├── api.py                      Public API (search, search_bills 등)
└── data/
    ├── lance_db/               LanceDB 테이블 (chunks)
    ├── manifest.sqlite         청크 등록·추적
    ├── bm25.pkl                BM25 인덱스
    └── run/                    indexer 로그
```

---

## 4. 주요 설계 결정 (왜 그렇게 했는가)

### 4.1 왜 LanceDB인가 (ChromaDB 대신)

ChromaDB로 1.3M 청크 임베딩 시도 후 OOM 발생 (32 GB RAM, 8 GB 가용). 원인:

- ChromaDB는 collection 열기 시 HNSW 인덱스 전체를 메모리에 로드
- 양자화 native 지원 없음 (float32만)
- mmap이 Windows에서 연속 가상 주소 공간 요구
- 1M+ 청크에서 알려진 한계

LanceDB는:
- Apache Arrow/Lance 컬럼 기반, lazy load (필요한 page만 mmap)
- float16 native 지원 (메모리 절반)
- 증분 insert 시 인덱스 자동 갱신 (DuckDB VSS는 안 됨)
- 수십억 벡터까지 production 검증

DuckDB VSS도 검토했으나 **HNSW 인덱스가 INSERT 후 자동 업데이트 안 됨** (drop+rebuild 필요), 증분 sync에 불리해서 제외.

### 4.2 왜 float16인가

| 선택 | 메모리 (3.5M) | 정확도 손실 |
|---|---:|---|
| float32 | 21 GB | 0% |
| **float16** | **10.5 GB** | **< 0.5%** |
| int8 | 5.25 GB | 1~2% |
| binary | 0.7 GB | ~5% |

dense 임베딩은 float32 → float16 변환에서 cosine 유사도 영향이 무시할 수준.
나중에 메모리가 더 필요하면 int8로 추가 압축 가능 (LanceDB 인덱스 옵션 변경).

### 4.3 왜 Multi-region인가

Vertex AI의 quota는 region별 독립:
- 단일 region: 1M tokens/min → 약 60 chunks/sec
- 8 regions: 8M tokens/min → 약 300 chunks/sec (5배 가속)

검증된 region 8개 (벡터 출력 100% 동일 확인):
- us-central1, asia-northeast3, asia-southeast1, us-west1, us-east4, us-east1, europe-west1, europe-west4

429 quota burst 시 해당 region 60초 cooldown, 다음 region 자동 사용.

### 4.4 왜 발언자 마커 기반 청킹 (회의록)

회의록은 평균 75K자(약 35페이지). 단순 800자 char split하면 의미 단위 깨짐.
`◯위원장`, `◯의장`, `◯국회의원` 등 발언자 마커로 1차 split 후, 너무 큰 segment만 char split로 보강.
회의록 source(`document_text` where source='minutes_*')에만 적용.

### 4.5 왜 ChromaDB 호환 인터페이스 유지

`vectordb.py`의 `VectorDB.upsert/query/count/delete` 시그니처를 ChromaDB와 동일하게.
search.py·indexer.py·bm25.py가 영향 없이 LanceDB로 마이그레이션됨.
`collection` 속성은 `_CollectionFacade`로 wrapping.

---

## 5. 사용법

### 5.1 Python 모듈

```python
from rag_assembly.api import search, search_bills, search_speeches

# 자연어 검색
results = search("AI 안전성 관련 입법 동향", top_k=5)
for r in results:
    meta = r["metadata"]
    print(f"[{meta['source']}] {r['text'][:200]}...")

# 소스별
bills = search_bills("딥페이크 처벌법", age=22)
speeches = search_speeches("개인정보 보호 발언", dae_num="제22대")
docs = search_documents("AI 윤리 가이드라인", doc_source="report")
members = search_members("국방위원회 비례대표")

# 정확 일치
m = lookup_member_by_name("강선우")
b = lookup_bill_by_id("PRC_X1V8C0G2V0X6U1N7C3S4U4E2I9Q5S1")

# 통계
print(stats())
```

### 5.2 MCP 툴 (Claude Code)

`duckdb_mcp_server.py`에 노출된 5개 툴:

| MCP 툴 | 용도 |
|---|---|
| `mcp__assembly-db__rag_search` | 전체 검색 (source 옵션 가능) |
| `mcp__assembly-db__rag_search_bills` | 법안 본문 검색 (age 필터) |
| `mcp__assembly-db__rag_search_speeches` | 발언 검색 (dae_num 필터) |
| `mcp__assembly-db__rag_search_documents` | 회의록·보고서 검색 (doc_source 필터) |
| `mcp__assembly-db__rag_stats` | 인덱스 현황 (청크 수·소스별 분포) |

MCP 서버는 첫 호출 시 lazy 로드 (ChromaDB·BM25·임베더 1회 초기화).

### 5.3 CLI

```bash
# 인덱싱 (8 regions multi-region)
venv/Scripts/python.exe rag_assembly/indexer.py --source bill_meta speech bill document
venv/Scripts/python.exe rag_assembly/indexer.py --source member --limit 10  # 디버깅
venv/Scripts/python.exe rag_assembly/indexer.py --source all --dry-run      # 청크 수만

# BM25 빌드 (인덱싱 후 1회)
venv/Scripts/python.exe rag_assembly/bm25.py

# CLI 검색 (search.py 단독)
venv/Scripts/python.exe rag_assembly/search.py "AI 안전성 발언"
venv/Scripts/python.exe rag_assembly/search.py "강선우 의원" --source member --top_k 3
```

---

## 6. 운영 관리

### 6.1 신규 데이터 추가 (증분)

법안·회의록 신규 행이 raw DB에 들어왔을 때:

```bash
# 1. raw DB 갱신은 collect/ 측 (download_all, download_bills, download_documents)

# 2. 증분 임베딩 (이미 임베딩된 chunk_id는 manifest에서 자동 skip)
venv/Scripts/python.exe rag_assembly/indexer.py --source bill document

# 3. BM25 재구축 (BM25는 코퍼스 통계라 전체 재빌드 필요, 수 분)
venv/Scripts/python.exe rag_assembly/bm25.py
```

증분 임베딩 비용 추정:
- 신규 법안 1개: 약 8 청크 → $0.001
- 신규 회의록 1건: 약 100 청크 → $0.01
- 월간 갱신 (~5K 신규): 약 $0.5

### 6.2 인증 갱신

gcloud ADC 토큰은 7일 정도 유효. 만료 시:

```bash
gcloud auth application-default login
```

### 6.3 troubleshooting

| 증상 | 원인 | 해결 |
|---|---|---|
| `429 RESOURCE_EXHAUSTED` | TPM quota 초과 | embedder의 region rotation이 자동 처리. 지속되면 EMBED_CONCURRENCY 낮춤 |
| `ResourceNotFound` | 모델 이름 변경 또는 region 미지원 | config.ENABLED_REGIONS 검증 (`gemini-embedding-001` 가용 region만) |
| LanceDB lock error | indexer 중복 실행 | 한 번에 하나만 실행 |
| Reranker 첫 호출 시 1.1 GB 다운로드 | bge-reranker-v2-m3 모델 download | 정상, 1회만 발생 |
| 메모리 사용량 증가 | OS의 mmap page cache | LanceDB는 idle 시 자동 evict. 의도적 |
| 검색 결과가 옛 내용만 | 새 데이터 임베딩 미반영 | `indexer.py --source X` 재실행 |

### 6.4 EMBED_CONFIG_VERSION 변경 시

프롬프트·모델·차원이 바뀌면 `config.py`의 `EMBED_CONFIG_VERSION` bump.
manifest는 이전 version의 chunk_id를 별도 추적하므로 옛 임베딩과 새 임베딩 공존 가능.
완전히 옛 임베딩 무시하고 시작하려면 `lance_db/` + `manifest.sqlite` 삭제 후 재실행.

---

## 7. 알려진 한계

1. **회의록 PDF 추출 품질** — 1990년대 옛 회의록은 띄어쓰기 깨짐 (raw.document_text 본문 자체의 한계). 임베딩 가능하나 검색·표시 시 가독성 떨어짐.
2. **speech-document 일부 중복** — 본회의 22대(101건) + 소위원회(188건)는 speeches에 segment 단위로 들어가 있고 document_text에 통째로 들어가 있음. 둘 다 임베딩되어 동일 내용이 두 source로 검색됨. 의도적 (원천이 다른 컨텍스트 보존).
3. **시간 범위** — 회의록 옛 것 포함하나 speeches는 본회의 22대만 (2024.06~). 옛 본회의 발언은 document_text 통해서만 검색 가능.
4. **뉴스 미포함** — 의도적 제외. 추가 시 약 $1 + 약 1시간.

---

## 8. 비용·성능 기록

| 시점 | 작업 | 비용 | 시간 |
|---|---|---:|---|
| 2026-05-10 초기 임베딩 (3.5M chunks) | Vertex AI gemini-embedding-001 1536d | 약 $5~10 | 약 3시간 (8 region) |
| 월간 증분 (~5K) | 동일 | $0.5 | 5분 |
| ChromaDB 시도 (실패) | 1.3M chunks 임베딩 후 OOM | $5 매몰 | 14시간 후 폐기 |

GCP 무료 크레딧으로 처리 (만료 임박 시점 활용).

---

## 9. 변경 이력

- **2026-05-10**: LanceDB float16 기반 신규 구축 (ChromaDB 1.3M chunks OOM 후 전환). 8-region multi-region.
- **2026-05-10 시도 (실패)**: ChromaDB로 시작, 1.3M에서 OOM. 데이터 모두 폐기, LanceDB로 재시작.

---

## 부록: 참고 문서

- 본 프로젝트 정본: [WORKFLOW.md](../WORKFLOW.md), [CLAUDE.md](../CLAUDE.md), [CODEBOOK.md](../CODEBOOK.md)
- 이전 RAG 프로젝트(슬랙용): `~/.openclaw/workspace-socialpolicy/rag_backend/` (다른 도메인이지만 동일 스택 패턴 참조)
