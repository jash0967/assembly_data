"""rag_assembly 전용 설정.

- 임베딩 모델·차원·질의 prefix
- LanceDB·manifest 경로
- 청킹 파라미터

repo root config.py(DB 경로 등)와 별도. 양쪽 다 import 가능.

임베딩 백엔드 (2026-07-27 전환):
  Vertex AI gemini-embedding-001 (1536-dim) → 로컬 sentence-transformers
  dragonkue/snowflake-arctic-embed-l-v2.0-ko (1024-dim, fp16).
  아래 EMBED_* 상수가 서빙 경로의 **유일한** 진실 공급원이다
  (embedder.py / _subproc_embed.py / search.py / indexer.py 가 전부 여기를 본다).
  문서 측 인덱스를 만든 embed_config(= data/embed_config.json 사이드카,
  manifest.sqlite::embed_config)와 반드시 일치해야 한다 —
  `python -m embedder --contract` 로 대조 가능.

인덱스 위치 (2026-07-27):
  `RAG_DATA_DIR` 환경변수로 DATA_DIR(= LanceDB·BM25·manifest.sqlite·
  embed_config.json 의 부모)을 옮길 수 있다. **미설정 시 기존과 동일하게
  rag_assembly/data.** duckdb_mcp_server.py 도 같은 변수를 같은 규칙으로 읽으므로
  둘이 갈라지지 않는다. 전체 환경변수 목록·규칙은 duckdb_mcp_server.py
  모듈 docstring(§경로 환경변수)이 정본이다.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _resolve_data_dir() -> Path:
    """RAG_DATA_DIR 환경변수 → DATA_DIR. 미설정·빈 값이면 ROOT/"data" (기존 동작).

    설정 시 ~ 와 $VAR/%VAR% 를 확장한 뒤 절대경로로 정규화한다 (Windows 드라이브
    문자·역슬래시 안전). 상대경로는 프로세스 CWD 기준이므로 절대경로 권장.
    duckdb_mcp_server.py::_env_path() 와 **동일한 규칙**이어야 한다.
    """
    raw = (os.environ.get("RAG_DATA_DIR") or "").strip()
    if not raw:
        return ROOT / "data"
    return Path(os.path.abspath(os.path.expanduser(os.path.expandvars(raw))))


def _ensure_dir(p: Path) -> None:
    """디렉터리 보장. 실패해도 import를 죽이지 않는다 (stderr 경고만).

    기본 경로에서는 이미 존재하므로 no-op. RAG_DATA_DIR 오타로 만들 수 없는
    경로가 들어와도 MCP 서버가 뜨긴 해야 한다(DuckDB 도구는 살아 있어야 하고,
    경로 진단은 duckdb_mcp_server.py 가 stderr로 안내한다).
    """
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"[rag_config] 디렉터리 생성 실패: {p} "
                         f"({type(e).__name__}: {e}) — RAG_DATA_DIR 확인\n")


DATA_DIR = _resolve_data_dir()
_ensure_dir(DATA_DIR)

# ── (미사용) Vertex AI 잔존 상수 ───────────────────────
# 로컬 모델 전환으로 서빙·인덱싱 어느 경로도 더 이상 읽지 않는다.
# 옛 1536 인덱스 재현이 필요할 때를 위한 기록으로만 남긴다.
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "project-21bfdbb5-2abd-4c9d-9c6")
GCP_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

# (미사용) Multi-region rotation: 각 region당 1M tokens/min 독립 quota
ENABLED_REGIONS = [
    "us-central1",       # 회복 후 가장 빠름 (~450ms 한국에서)
    "asia-northeast3",   # Seoul
    "asia-southeast1",   # Singapore
    "us-west1",
    "us-east4",
    "us-east1",
    "europe-west1",
    "europe-west4",
]
EMBED_TASK_TYPE_DOC = "RETRIEVAL_DOCUMENT"    # (미사용) Vertex 전용
EMBED_TASK_TYPE_QUERY = "RETRIEVAL_QUERY"     # (미사용) Vertex 전용
RATE_LIMIT_RPM = 500       # (미사용) Vertex 전용
RATE_LIMIT_TPM = None      # (미사용) Vertex 전용
EMBED_CONCURRENCY = 28     # (미사용) Vertex 멀티리전 워커 수

# ── 임베딩 모델 (로컬 sentence-transformers) ───────────
EMBED_MODEL = "dragonkue/snowflake-arctic-embed-l-v2.0-ko"
# HF revision 고정 — 문서 측 임베딩과 같은 가중치임을 보장 (run_manifest.json)
EMBED_REVISION = "55ec6e9358a56d56af759bc8372e970caf8c305f"
EMBED_DIM = 1024
EMBED_MAX_SEQ_LEN = 1024   # 문서 측 embed_remote.py::MAX_SEQ_LEN 과 동일해야 함
EMBED_NORMALIZE = True     # 저장·질의 모두 L2 정규화 (metric=cosine 전제)

# 비대칭 prefix 규약 (arctic-embed 모델 카드):
#   질의 측에만 "query: " — 문서 측은 원문 그대로.
# ⚠ 이 두 상수 말고 다른 곳에 prefix 문자열을 하드코딩하지 말 것.
#   주입 지점은 embedder.py::_encode() 한 곳뿐이다.
EMBED_QUERY_PREFIX = "query: "
EMBED_DOC_PREFIX = ""

# 로컬 인코딩 파라미터
EMBED_BATCH_SIZE = 32          # 문서 배치 (질의는 1건)
EMBED_TORCH_DTYPE = "float16"  # CUDA 연산 dtype
EMBED_TORCH_DTYPE_CPU = "float32"   # CPU에서 fp16은 느리고 일부 커널 미지원
# 디바이스: 환경변수 우선 → CUDA 가용 시 cuda → 없으면 cpu
EMBED_DEVICE = os.environ.get("RAG_EMBED_DEVICE", "").strip() or None

# 청킹 파라미터
CHUNK_SIZE = 800           # chars
CHUNK_OVERLAP = 200        # chars
SHORT_DOC_THRESHOLD = 1500 # 이보다 짧으면 단일 청크로

# 저장 경로
LANCE_DIR = DATA_DIR / "lance_db"
MANIFEST_DB = DATA_DIR / "manifest.sqlite"
BM25_PKL = DATA_DIR / "bm25.pkl"
RUN_DIR = DATA_DIR / "run"
_ensure_dir(RUN_DIR)

# LanceDB 테이블명
TABLE_NAME = "chunks"

# 벡터 저장 dtype (float32 보다 메모리 절반, 정확도 손실 < 0.5%)
VECTOR_DTYPE = "float16"  # 'float16' | 'float32'

# 임베딩 대상 (수정 시 manifest의 embed_config_version도 bump)
# ingest_embeddings.py::DEFAULT_EMBED_CONFIG_VERSION 과 **정확히** 같아야
# manifest.chunk_counts_by_source() 가 새 인덱스 행을 센다.
EMBED_CONFIG_VERSION = "v3_arctic_ko_1024_fp16_20260726"

# ingest_embeddings.py 가 남기는 계약 기록 (읽기 전용 대조용)
EMBED_CONFIG_SIDECAR = DATA_DIR / "embed_config.json"

# (호환용) 옛 ChromaDB 경로 — 이미 삭제됐지만 코드 검색 시 참조용
CHROMA_DIR = DATA_DIR / "chroma_db"  # 사용 금지, lance_db 사용
COLLECTION_NAME = TABLE_NAME

# 검색 파라미터
VECTOR_TOP_K = 30
BM25_TOP_K = 30
RRF_K = 60                 # Reciprocal Rank Fusion 상수
FINAL_TOP_K = 10           # 최종 반환 (rerank 후)

# Reranker
RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
RERANK_BATCH = 32

# DB 경로 (repo root config 재활용 — 이름 충돌 회피 위해 importlib 사용)
def _repo_root_db_paths():
    import importlib.util
    p = ROOT.parent / "config.py"
    spec = importlib.util.spec_from_file_location("_repo_config", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.RAW_DB_PATH, m.ANALYSIS_DB_PATH

RAW_DB_PATH, ANALYSIS_DB_PATH = _repo_root_db_paths()


# ── embed_config 계약 대조 ────────────────────────────

def embed_contract_mismatches() -> list[str]:
    """data/embed_config.json(문서 측 실제 설정) vs 위 EMBED_* 상수 대조.

    사이드카 **파일 자체가 없으면** [] (아직 ingest 전 — 검사 대상이 없다).
    파일이 있는데 키가 없거나(model 통째 누락, dim: null 등) 값이 다르면 둘 다
    보고한다 — model.revision만 예외(부재·null이 정상, 아래 주석 참조).
    import 시점에 부르지 말 것: 파일 I/O이고, 불일치는 '경고'지 치명이 아니다
    (호출자가 로깅·표시).
    """
    p = Path(EMBED_CONFIG_SIDECAR)
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return [f"embed_config.json 읽기 실패: {type(e).__name__}: {e}"]

    if not isinstance(d, dict):
        return [f"embed_config.json 형식 오류: 최상위가 {type(d).__name__} (dict여야 함)"]

    model = d.get("model") or {}
    prefix = d.get("prefix") or {}
    if not isinstance(model, dict):
        model = {}
    if not isinstance(prefix, dict):
        prefix = {}

    # 키 부재와 값 불일치는 **다른 사건**이다. 예전 구현은 `got is not None`으로
    # 둘 다 조용히 통과시켜서, model 키가 통째로 없거나 dim: null 인 사이드카도
    # '일치'로 보고했다 (계약 검사기가 사실상 무력화). 이제 둘 다 보고한다.
    _MISSING = object()
    pairs = [
        ("model.name", model.get("name", _MISSING), EMBED_MODEL),
        ("model.dim", model.get("dim", _MISSING), EMBED_DIM),
        ("model.max_seq_tokens", model.get("max_seq_tokens", _MISSING),
         EMBED_MAX_SEQ_LEN),
        ("prefix.query", prefix.get("query", _MISSING), EMBED_QUERY_PREFIX),
        ("prefix.document", prefix.get("document", _MISSING), EMBED_DOC_PREFIX),
        ("embed_config_version", d.get("embed_config_version", _MISSING),
         EMBED_CONFIG_VERSION),
    ]
    # revision만은 부재·null이 정상 상태다 (로컬 경로 모델로 임베딩하면 기록할
    # revision이 없다). 값이 실제로 적혀 있을 때만 대조한다.
    rev = model.get("revision")
    if rev:
        pairs.append(("model.revision", rev, EMBED_REVISION))

    out = []
    for k, got, want in pairs:
        if got is _MISSING:
            out.append(f"{k}: 사이드카에 키 없음 (config={want!r})")
        elif got != want:
            out.append(f"{k}: 사이드카={got!r} != config={want!r}")
    return out
