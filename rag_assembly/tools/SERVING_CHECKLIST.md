# 서빙 전환 체크리스트 — 1536/Gemini → 1024/arctic-ko

`ingest_embeddings.py` 가 끝나면 **인덱스는 새것, 서빙 코드는 옛것**인 상태가 된다.
아래를 마치기 전에는 RAG 검색이 동작하지 않는다. (2026-07-26 코드 기준)

## 왜 자동으로 안 되나

`ingest` 가 쓰는 `manifest.sqlite::embed_config` 와 `rag_assembly/data/embed_config.json`
사이드카는 **기록일 뿐, 지금 읽는 코드가 하나도 없다.** 서빙 경로는 전부
`rag_assembly/config.py` 의 모듈 상수를 본다.

```
rag_assembly/config.py:35   EMBED_MODEL = "gemini-embedding-001"
rag_assembly/config.py:36   EMBED_DIM   = 1536
rag_assembly/config.py:65   EMBED_CONFIG_VERSION = "v2_lancedb_fp16_20260510"
```

## 지금 깨지는 것 (증상)

| # | 증상 | 원인 |
|---|------|------|
| 1 | MCP `rag_stats` 가 `vectordb_count`=2.6M 인데 `chunks_by_source={}` | `api.py:153` + `manifest.py:110` 가 `cfg.EMBED_CONFIG_VERSION`(v2)로 필터. 새 행은 v3로 등록됨 |
| 2 | MCP 하이브리드 검색이 1536차원 질의 벡터를 만든다 | `duckdb_mcp_server.py:766 _simple_embed_query` → `rag_assembly/_subproc_embed.py:19-27` 이 Vertex에 `outputDimensionality=1536`·`RETRIEVAL_QUERY` 로 직접 HTTP |
| 3 | `AssemblySearch()` 생성만으로 ADC 인증·Vertex 클라이언트 8개 생성 | `search.py:34` 의 무조건 `Embedder()` (`embedder.py` 는 import 시점에 google-genai 로드) |
| 4 | 질의에 `"query: "` 접두사가 안 붙는다 | arctic-ko 규약. 문서 측은 접두사 없이 임베딩됨 → 질의만 붙여야 대칭이 맞는다 |

> 참고: `VectorDB` 는 문제가 **아니다**. `vectordb.py:91` 의 `dim or cfg.EMBED_DIM` 은
> 테이블 *생성* 시에만 쓰이므로, 인자 없는 `VectorDB()` 로도 기존 1024 테이블을 열고
> 1024 질의로 검색된다(실측 확인). 진짜 병목은 **질의 벡터 생산 경로**뿐이다.

## 편집 목록

- [ ] **1. `rag_assembly/config.py`**
  - `EMBED_MODEL = "dragonkue/snowflake-arctic-embed-l-v2.0-ko"`
  - `EMBED_DIM = 1024`
  - `EMBED_CONFIG_VERSION = "v3_arctic_ko_1024_fp16_20260726"` ← ingest 기본값과 **정확히** 일치해야 `chunks_by_source` 가 산다
  - 새 상수: `EMBED_REVISION = "55ec6e9358a56d56af759bc8372e970caf8c305f"`, `EMBED_QUERY_PREFIX = "query: "`, `EMBED_MAX_SEQ_LEN = 1024`
  - Vertex 전용 상수(`ENABLED_REGIONS`, `EMBED_TASK_TYPE_*`, `RATE_LIMIT_RPM`)는 로컬 모델에선 무의미 — 지우지 말고 "미사용" 주석만 달아 두면 diff가 작다

- [ ] **2. 질의 임베더 교체 (핵심)**
  - `rag_assembly/embedder.py` 의 `Embedder`(Vertex)를 대체할 로컬 임베더를 만든다.
    `SentenceTransformer(cfg.EMBED_MODEL, revision=cfg.EMBED_REVISION, device=...)`,
    `max_seq_length = cfg.EMBED_MAX_SEQ_LEN`, `encode(cfg.EMBED_QUERY_PREFIX + q, normalize_embeddings=True)`.
  - **`"query: "` 는 질의에만.** 문서 측은 GPU에서 접두사 없이 임베딩됐다
    (`embed_config.json::prefix` 가 그 계약을 기록한다).
  - `embed_query()` 시그니처(`str -> list[float]`)는 유지 — `search.py:51` 이 그대로 쓴다.
  - 모델 로드가 수 초 걸리므로 lazy + 프로세스 내 싱글턴으로.

- [ ] **3. `rag_assembly/search.py:34`**
  - `self.embedder = Embedder()` 를 lazy property 로. 지금은 BM25-only·필터 검색에도
    임베더가 무조건 붙는다.

- [ ] **4. `rag_assembly/_subproc_embed.py` (MCP 경로)**
  - Vertex 원시 HTTP → 로컬 모델 호출로 교체. 단, **호출마다 568M 모델을 새로 로드하면
    질의당 수 초**가 붙는다. 둘 중 하나:
    - (a) 상주 데몬(유닉스 소켓/파이프)으로 바꾸고 MCP는 문자열만 주고받기, 또는
    - (b) in-proc 전환 — FastMCP 컨텍스트에서 hang 하던 원인은 google-genai 의 네트워크
      호출이었으므로, 네트워크가 사라진 로컬 모델은 subprocess 우회 자체가 불필요할 수 있다.
      **(b)를 먼저 시도**하고, hang이 재현되면 (a)로.
  - `outputDimensionality`·`task_type` 파라미터는 삭제 (Vertex 전용).

- [ ] **5. 사후 검증**
  - `python -c "from rag_assembly.api import stats; print(stats())"` → `chunks_by_source` 가 비지 않는지
  - 알려진 문장으로 self-query: 원문 청크가 top1, distance ≈ 0
  - MCP `rag_search` 한국어 질의 1건 → 상위 결과가 주제적으로 맞는지 (질의 접두사 유무로 A/B 비교하면 접두사 효과를 눈으로 확인 가능)
  - `rag_assembly/data/embed_config.json` 의 `model.name`·`dim`·`prefix.query` 가 위 편집과 일치하는지

## 되돌리기

LanceDB 테이블은 이름이 같다(`chunks`). 옛 1536 인덱스로 롤백하려면 백업본이 필요하다 —
`rag_assembly/data/lance_db` 를 통째로 복사해 두고 시작할 것. (현재 남아 있는 테이블은
0행·dim 1536 빈 껍데기라 ingest 가 자동 drop·재생성한다.)
