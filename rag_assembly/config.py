"""rag_assembly 전용 설정.

- 임베딩 모델·차원
- ChromaDB·manifest 경로
- Vertex AI 프로젝트·리전
- 청킹 파라미터

repo root config.py(DB 경로 등)와 별도. 양쪽 다 import 가능.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Vertex AI
GCP_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "project-21bfdbb5-2abd-4c9d-9c6")
GCP_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

# Multi-region rotation: 각 region당 1M tokens/min 독립 quota
# 검증 완료 (2026-05-10): 7 regions 모두 gemini-embedding-001 지원, 벡터 100% 동일
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

# 임베딩 모델
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 1536           # Matryoshka, 3072 → 1536 절약
EMBED_BATCH_SIZE = 50      # 배치 작게 → 응답 빠름
EMBED_CONCURRENCY = 28     # 7 regions × 4 workers — 7M TPM 활용
EMBED_TASK_TYPE_DOC = "RETRIEVAL_DOCUMENT"
EMBED_TASK_TYPE_QUERY = "RETRIEVAL_QUERY"

# Rate limit (Vertex AI gemini-embedding-001 default 600 RPM)
RATE_LIMIT_RPM = 500       # 안전 마진
RATE_LIMIT_TPM = None      # 토큰 기반 제한은 별도

# 청킹 파라미터
CHUNK_SIZE = 800           # chars
CHUNK_OVERLAP = 200        # chars
SHORT_DOC_THRESHOLD = 1500 # 이보다 짧으면 단일 청크로

# 저장 경로
LANCE_DIR = DATA_DIR / "lance_db"
MANIFEST_DB = DATA_DIR / "manifest.sqlite"
BM25_PKL = DATA_DIR / "bm25.pkl"
RUN_DIR = DATA_DIR / "run"
RUN_DIR.mkdir(exist_ok=True)

# LanceDB 테이블명
TABLE_NAME = "chunks"

# 벡터 저장 dtype (float32 보다 메모리 절반, 정확도 손실 < 0.5%)
VECTOR_DTYPE = "float16"  # 'float16' | 'float32'

# 임베딩 대상 (수정 시 manifest의 embed_config_version도 bump)
EMBED_CONFIG_VERSION = "v2_lancedb_fp16_20260510"

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
