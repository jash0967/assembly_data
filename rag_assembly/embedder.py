"""로컬 임베딩 wrapper — dragonkue/snowflake-arctic-embed-l-v2.0-ko.

2026-07-27: Vertex AI gemini-embedding-001(1536-dim) → 로컬
sentence-transformers(1024-dim, fp16)로 교체. ADC 자격증명·네트워크 의존 제거.

설계 요점
---------
1. **prefix 주입 지점은 여기 한 곳뿐이다.**
   질의: `cfg.EMBED_QUERY_PREFIX` ("query: ") 를 붙인다.
   문서: `cfg.EMBED_DOC_PREFIX` ("") — 즉 원문 그대로. 문서 측 인덱스가
   prefix 없이 만들어졌으므로(embed_remote.py) 이 비대칭을 깨면 검색 품질이
   조용히 무너진다. 다른 모듈에서 "query: " 를 하드코딩하지 말 것.

2. **모델은 프로세스당 1회 로드**(`_MODEL` 싱글턴 + `_MODEL_LOCK`).
   568M 모델 로드는 수 초 걸리므로 질의마다 다시 만들면 안 된다.

3. **stdout 오염 방지.** MCP 서버는 stdin/stdout JSON-RPC라 로드 구간의
   print() 한 줄이 protocol을 깬다. import 시 진행률 환경변수를 끄고,
   추가로 호출자가 `set_load_guard()`로 컨텍스트 매니저 팩토리를 등록하면
   **모델 로드 구간만** 그 가드 안에서 실행한다 (duckdb_mcp_server.py 가
   자신의 `_worker_stdout_guard`를 등록한다). fd 레벨 redirect는 쓰지 않는다.

4. **GPU 없거나 VRAM 부족이면 CPU 폴백.** 4060(8GB)을 다른 작업과 공유하므로
   로드 OOM·인코딩 OOM 둘 다 흡수한다: 배치 반감 → 그래도 안 되면 CPU 재로드.
"""
import _bootstrap  # noqa: F401

import contextlib
import logging
import os
import threading

# 진행률·텔레메트리 차단 — sentence_transformers/huggingface_hub import 전에.
# 이미 설정된 값은 존중. 여기서 **offline은 켜지 않는다** — 그건 전역이 아니라
# 로드 호출 단위(local_files_only)로 건다. pinned_snapshot_is_cached() 참조.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import numpy as np

import config as cfg


log = logging.getLogger(__name__)


def pinned_snapshot_is_cached() -> bool:
    """고정 revision 스냅샷이 이미 HF 캐시에 있는지. **부수효과 없음.**

    True면 `_build()`가 SentenceTransformer(..., local_files_only=True)로 로드해
    huggingface.co HEAD/GET을 원천 차단한다 — 느린 왕복·네트워크 장애 시 hang·
    hub의 stdout 경고("You are sending unauthenticated requests...")를 막는 것이
    목적이고, 그건 예전 방식(os.environ["HF_HUB_OFFLINE"]="1")과 동일하다.

    ⚠ 전역 env를 세팅하지 않는 이유: huggingface_hub은 import 시점에
    HF_HUB_OFFLINE을 모듈 상수로 굳히므로, 한 번 켜면 나중에 되돌려도 그 프로세스의
    **모든** HF 모델이 영구히 다운로드 불가가 된다(reranker·미캐시 모델까지).
    MCP 서버처럼 import만으로 이 모듈을 들이는 호스트에서 특히 해롭다.
    캐시에 없으면 False → 인자를 붙이지 않아 최초 1회 다운로드가 정상 동작한다.
    """
    if not cfg.EMBED_REVISION or os.path.exists(os.path.expanduser(cfg.EMBED_MODEL)):
        return False
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"),
        "hub")
    snap = os.path.join(hub, "models--" + cfg.EMBED_MODEL.replace("/", "--"),
                        "snapshots", cfg.EMBED_REVISION)
    return os.path.isdir(snap)


# ── 프로세스 단위 모델 캐시 ────────────────────────────

_MODEL = None                       # SentenceTransformer | None
_MODEL_DEVICE: str | None = None    # 실제로 로드된 디바이스
_MODEL_LOCK = threading.RLock()     # 로드 직렬화 (가드 창도 이 안에서만 열림)
_LOAD_GUARD = None                  # 로드 구간용 contextmanager 팩토리 (옵션)


def set_load_guard(factory) -> None:
    """모델 로드 구간을 감쌀 컨텍스트 매니저 팩토리 등록.

    MCP 서버처럼 stdout이 protocol 채널인 호스트가 자신의 stdout 가드를
    넘긴다. 등록하지 않으면 아무것도 하지 않는다(nullcontext).
    """
    global _LOAD_GUARD
    _LOAD_GUARD = factory


def is_loaded() -> bool:
    """모델이 이미 이 프로세스에 로드돼 있는지 (호출자의 가드 결정용)."""
    return _MODEL is not None


def loaded_device() -> str | None:
    return _MODEL_DEVICE


def _pick_device() -> str:
    """cfg.EMBED_DEVICE(=env RAG_EMBED_DEVICE) 우선, 없으면 CUDA 가용성으로 결정."""
    if cfg.EMBED_DEVICE:
        return cfg.EMBED_DEVICE
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception as e:  # noqa: BLE001 — torch 부재/드라이버 문제 → CPU
        log.warning("CUDA 확인 실패 → CPU 사용: %s: %s", type(e).__name__, e)
    return "cpu"


def _is_oom(e: BaseException) -> bool:
    try:
        import torch
        oom_cls = getattr(torch.cuda, "OutOfMemoryError", None)
        if oom_cls is not None and isinstance(e, oom_cls):
            return True
    except Exception:  # noqa: BLE001
        pass
    s = str(e).lower()
    return "out of memory" in s or "cuda error" in s


def _build(device: str):
    """SentenceTransformer 실제 생성. 호출자가 _MODEL_LOCK을 잡고 있어야 한다.

    prefix는 여기서 다루지 않는다. 모델 카드가 prompts={"query": "query: "}를
    싣고 있지만 default_prompt_name=null 이라 encode()는 아무 prompt도 적용하지
    않는다. 우리는 _encode()에서 직접 붙인다 — pooling이 CLS + include_prompt=true
    이므로 수동 prepend와 prompt_name="query"는 **같은 벡터**가 된다(실측 확인).
    """
    import torch
    from sentence_transformers import SentenceTransformer

    dtype_name = (cfg.EMBED_TORCH_DTYPE_CPU if device.startswith("cpu")
                  else cfg.EMBED_TORCH_DTYPE)
    torch_dtype = {"float16": torch.float16,
                   "bfloat16": torch.bfloat16,
                   "float32": torch.float32}[dtype_name]
    kwargs = dict(device=device, model_kwargs={"torch_dtype": torch_dtype})
    # 로컬 경로 모델이면 revision 개념이 없다 (HF repo id일 때만 고정).
    if cfg.EMBED_REVISION and not os.path.exists(os.path.expanduser(cfg.EMBED_MODEL)):
        kwargs["revision"] = cfg.EMBED_REVISION
    # offline 강제는 **이 호출 하나**로 한정 (전역 env 오염 금지 — 위 docstring).
    offline = pinned_snapshot_is_cached()
    if offline:
        kwargs["local_files_only"] = True

    log.info("임베딩 모델 로드: %s (device=%s, dtype=%s, rev=%s, local_files_only=%s)",
             cfg.EMBED_MODEL, device, dtype_name,
             (cfg.EMBED_REVISION or "-")[:12], offline)
    m = SentenceTransformer(cfg.EMBED_MODEL, **kwargs)
    m.max_seq_length = cfg.EMBED_MAX_SEQ_LEN
    m.eval()
    return m


def load_model(force_device: str | None = None):
    """모델 lazy 로드 + 캐시. 이미 로드됐으면 그대로 반환.

    CUDA 로드가 OOM/드라이버 문제로 실패하면 CPU로 한 번 폴백한다.
    """
    global _MODEL, _MODEL_DEVICE
    if _MODEL is not None and force_device is None:
        return _MODEL
    with _MODEL_LOCK:
        if _MODEL is not None and force_device is None:
            return _MODEL
        device = force_device or _pick_device()
        guard = _LOAD_GUARD() if _LOAD_GUARD is not None else contextlib.nullcontext()
        with guard:
            try:
                model = _build(device)
            except Exception as e:  # noqa: BLE001
                if device.startswith("cpu"):
                    raise
                log.warning("%s 로드 실패(%s: %s) → CPU 폴백",
                            device, type(e).__name__, e)
                _cuda_empty_cache()
                model = _build("cpu")
                device = "cpu"
            # 이전 모델이 있으면(디바이스 전환) 참조를 끊고 VRAM 회수
            if _MODEL is not None and _MODEL is not model:
                _MODEL = None
                _cuda_empty_cache()
            _MODEL = model
            _MODEL_DEVICE = device
        log.info("임베딩 모델 준비 완료 (device=%s, max_seq_length=%s)",
                 device, _MODEL.max_seq_length)
        return _MODEL


def unload_model() -> None:
    """모델 해제 (테스트·디바이스 전환용)."""
    global _MODEL, _MODEL_DEVICE
    with _MODEL_LOCK:
        _MODEL = None
        _MODEL_DEVICE = None
    _cuda_empty_cache()


def _cuda_empty_cache() -> None:
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


# ── 인코딩 ─────────────────────────────────────────────

def _encode(texts: list[str], prefix: str,
            batch_size: int | None = None) -> np.ndarray:
    """prefix 주입 + 정규화 인코딩. **prefix가 문자열에 붙는 유일한 지점.**

    OOM 시 배치 반감 → batch_size==1 에서도 OOM이면 CPU 재로드 후 1회 재시도.
    반환: (N, EMBED_DIM) float32, 각 행 L2 norm ≈ 1.
    """
    if not texts:
        return np.zeros((0, cfg.EMBED_DIM), dtype=np.float32)
    payload = [prefix + t for t in texts] if prefix else list(texts)
    bs = batch_size or cfg.EMBED_BATCH_SIZE
    cpu_retried = False
    while True:
        model = load_model()
        try:
            vecs = model.encode(
                payload,
                batch_size=bs,
                normalize_embeddings=cfg.EMBED_NORMALIZE,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return np.asarray(vecs, dtype=np.float32)
        except Exception as e:  # noqa: BLE001
            if not _is_oom(e):
                raise
            if bs > 1:
                new_bs = max(1, bs // 2)
                log.warning("[OOM] batch_size %d → %d 재시도", bs, new_bs)
                bs = new_bs
                _cuda_empty_cache()
                continue
            if cpu_retried or (_MODEL_DEVICE or "").startswith("cpu"):
                raise
            log.warning("[OOM] batch_size=1 에서도 실패 → CPU 폴백 후 재시도")
            cpu_retried = True
            bs = cfg.EMBED_BATCH_SIZE
            load_model(force_device="cpu")


def embed_query(query: str) -> list[float]:
    """질의 1건 → 1024-dim 단위벡터. **"query: " prefix가 붙는다.**"""
    return _encode([query], cfg.EMBED_QUERY_PREFIX, batch_size=1)[0].tolist()


def embed_queries(queries: list[str]) -> list[list[float]]:
    """질의 다건. prefix 동일 적용."""
    return _encode(queries, cfg.EMBED_QUERY_PREFIX).tolist()


def embed_documents(texts: list[str]) -> list[list[float]]:
    """문서 다건. **prefix 없음**(cfg.EMBED_DOC_PREFIX = "")."""
    return _encode(texts, cfg.EMBED_DOC_PREFIX).tolist()


# ── 하위호환 클래스 래퍼 ───────────────────────────────

class Embedder:
    """구 Vertex Embedder와 같은 표면(embed_query / embed_documents).

    상태는 모두 모듈 싱글턴에 있으므로 인스턴스를 몇 개 만들어도
    모델은 프로세스당 1회만 로드된다. 생성자는 모델을 로드하지 않는다
    (lazy) — BM25 전용·필터 전용 경로에서 GPU를 잡지 않기 위해서.
    """

    def __init__(self, warm: bool = False):
        self.model_name = cfg.EMBED_MODEL
        self.dim = cfg.EMBED_DIM
        self.batch_size = cfg.EMBED_BATCH_SIZE
        if warm:
            load_model()

    @property
    def device(self) -> str | None:
        return loaded_device()

    def embed_query(self, query: str) -> list[float]:
        return embed_query(query)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return embed_documents(texts)


# ── CLI 자가 검증 ──────────────────────────────────────

def _cli() -> None:
    """python embedder.py [--contract] [질의문]

    prefix 주입·차원·정규화를 실측으로 확인한다.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="인공지능 기본법 규제 샌드박스")
    ap.add_argument("--contract", action="store_true",
                    help="data/embed_config.json 과 config 상수 대조만")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    mism = cfg.embed_contract_mismatches()
    if mism:
        print("embed_config 계약 불일치:")
        for m in mism:
            print("  -", m)
    else:
        p = cfg.EMBED_CONFIG_SIDECAR
        print(f"embed_config 계약: {'일치' if p.exists() else '사이드카 없음(대조 생략)'}")
    if args.contract:
        return

    qv = embed_query(args.query)
    dv = embed_documents([args.query])[0]
    qn = float(np.linalg.norm(qv))
    dn = float(np.linalg.norm(dv))
    cos = float(np.dot(qv, dv))
    print(f"device        : {loaded_device()}")
    print(f"query prefix  : {cfg.EMBED_QUERY_PREFIX!r}")
    print(f"doc prefix    : {cfg.EMBED_DOC_PREFIX!r}")
    print(f"query dim/norm: {len(qv)} / {qn:.6f}")
    print(f"doc   dim/norm: {len(dv)} / {dn:.6f}")
    print(f"cos(q, d)     : {cos:.6f}  (prefix 차이만큼 1.0 미만이어야 정상)")


if __name__ == "__main__":
    _cli()
