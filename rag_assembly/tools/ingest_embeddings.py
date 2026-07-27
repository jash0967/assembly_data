#!/usr/bin/env python3
"""회수한 임베딩 샤드(emb_*.parquet) + 청크(chunks_*.parquet) → 로컬 RAG 인덱스 재조립.

3-스크립트 체계의 3번째 단계. GPU 머신에서 만든 임베딩을 받아와서
rag_assembly 인덱스(LanceDB + BM25 + manifest)를 다시 세운다.

    export_chunks.py        (로컬, DuckDB → chunks_*.parquet)
    bundle/embed_remote.py  (GPU 머신, chunks_*.parquet → emb_*.parquet)
    ingest_embeddings.py  ← 여기 (로컬, emb_* + chunks_* → LanceDB/BM25/manifest)

모델: dragonkue/snowflake-arctic-embed-l-v2.0-ko (568M, 1024-dim, fp16)
  - 문서 측 prefix 없음
  - 질의 측만 "query: " prefix  ← 서빙 코드가 반드시 지켜야 함
  이 정책은 manifest.sqlite::embed_config 테이블과 data/embed_config.json
  사이드카에 **기록만** 된다. 현재 서빙 코드(rag_assembly/api.py·search.py·
  embedder.py·_subproc_embed.py, duckdb_mcp_server.py)는 이 기록을 읽지 않고
  rag_assembly/config.py 의 상수(gemini-embedding-001 / 1536)를 본다.
  → 적재가 끝나도 RAG 검색은 그대로는 동작하지 않는다. 반드시
     working/embed_bundle/SERVING_CHECKLIST.md 의 편집을 마칠 것.

데이터 계약 (3 스크립트 공유):
  chunks_*.parquet : chunk_id(str), text(str), source(str), metadata_json(str)
  emb_*.parquet    : chunk_id(str), vector(fixed_size_list<float16,1024>)
                     ※ list<float32> 형태도 읽을 수 있게 허용(저장은 항상 fp16)
                     샤드당 50,000 청크, 임시파일→rename 원자적 쓰기
  run_manifest.json / _DONE.json : GPU 실행 출처 증명 (--emb-dir 안에 필수)

동작:
  0. --emb-dir 의 run_manifest.json·_DONE.json 검증 (dry_run=false, complete=true,
     model/dim/shard_size/총행수 일치). 없거나 불일치면 중단 (exit 2)
  1. 파일 discover + 스키마 검증 (모든 샤드의 parquet 스키마는 메타만 읽어 전수 검사)
  2. chunk_id 대조 — chunks ↔ emb 완전 일치 아니면 중단 (exit 2)
     + 로컬 chunks_*.parquet sha256 ↔ run_manifest 기록 재대조 (전송 무결성)
  3. 벡터 값 검사 (샘플 샤드 전수 로드: dim / NaN·Inf / L2 norm 통계)
  4. LanceDB 적재 (스트리밍, 샤드 LRU 캐시로 메모리 상한 유지)
  5. HNSW 인덱스 빌드
  6. BM25 인덱스 빌드 (rag_assembly/bm25.py 재사용)
  7. manifest 등록 + embed_config 기록 (sqlite + JSON 사이드카)

Usage:
    .venv/bin/python working/embed_bundle/ingest_embeddings.py \
        --emb-dir  working/embed_bundle/out/emb \
        --chunks-dir working/embed_bundle/out/chunks \
        --dry-run

    .venv/bin/python working/embed_bundle/ingest_embeddings.py \
        --emb-dir ... --chunks-dir ...            # 전체 재조립
    ... --skip-bm25                                # 벡터만 (BM25는 나중에)

Exit code: 0 정상 / 1 실행 오류 / 2 검증 실패(chunk_id 불일치 등)
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


# ── 새 임베딩 설정 (기본값) ─────────────────────────────
# EMBED_CONFIG_VERSION은 manifest 행의 키이므로 재실행 간 안정적이어야 한다.
# (런타임 날짜로 만들지 말 것 — 재실행마다 새 버전이 생겨 resume이 깨진다.)
DEFAULT_EMBED_CONFIG_VERSION = "v3_arctic_ko_1024_fp16_20260726"
DEFAULT_MODEL_NAME = "dragonkue/snowflake-arctic-embed-l-v2.0-ko"
DEFAULT_DIM = 1024
DEFAULT_QUERY_PREFIX = "query: "   # 질의 측에만 붙인다
DEFAULT_DOC_PREFIX = ""            # 문서 측은 prefix 없음
# embed_remote.py 의 MAX_SEQ_LEN 과 같은 값이어야 한다 (embed_config 기록이
# 실제 인코딩 설정과 일치하도록). 실제 값은 run_manifest.json 에서 읽어 덮어쓴다.
DEFAULT_MAX_SEQ_TOKENS = 1024      # XLM-R 토크나이저 기준 (모델 한계는 8192)
DEFAULT_METRIC = "cosine"

CHUNK_REQUIRED_COLS = ("chunk_id", "text", "source", "metadata_json")
EMB_REQUIRED_COLS = ("chunk_id", "vector")

RUN_MANIFEST_NAME = "run_manifest.json"
DONE_NAME = "_DONE.json"
DRY_MARKER = "_DRY_RUN"
SERVING_CHECKLIST = "working/embed_bundle/SERVING_CHECKLIST.md"

EXIT_OK, EXIT_ERROR, EXIT_VALIDATION = 0, 1, 2

log = logging.getLogger("ingest")


class IngestAbort(Exception):
    """데이터·상태 불일치로 중단 (exit 2)."""


# ── rag_assembly 모듈 로딩 ──────────────────────────────

def load_rag_modules(repo_root: Path):
    """rag_assembly의 config/vectordb/manifest/bm25를 안전하게 import.

    주의: repo root와 rag_assembly 양쪽에 config.py가 있다. rag_assembly의
    모듈들은 `import config`로 *rag_assembly/config.py*를 기대하므로, 어떤
    rag 모듈보다 먼저 sys.modules["config"]에 그 파일을 직접 바인딩해 둔다.
    (rag_assembly/_bootstrap.py는 rag_assembly에 config.py가 있으면 repo root를
     sys.path에 넣지 않으므로 평소엔 문제가 없지만, 여기선 cwd·sys.path[0]가
     달라 해석이 흔들릴 수 있어 명시적으로 고정한다.)
    """
    rag_dir = repo_root / "rag_assembly"
    if not (rag_dir / "config.py").exists():
        raise RuntimeError(f"rag_assembly not found under {repo_root}")
    if str(rag_dir) not in sys.path:
        sys.path.insert(0, str(rag_dir))
    if "config" not in sys.modules:
        spec = importlib.util.spec_from_file_location("config", rag_dir / "config.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["config"] = mod
        spec.loader.exec_module(mod)
    rag_cfg = sys.modules["config"]
    if not hasattr(rag_cfg, "LANCE_DIR"):
        raise RuntimeError(
            f"sys.modules['config'] is {getattr(rag_cfg, '__file__', '?')} — "
            "repo root config.py가 잡혔다. rag_assembly/config.py여야 함.")
    import vectordb as vectordb_mod
    import manifest as manifest_mod
    return rag_cfg, vectordb_mod, manifest_mod


def load_bm25_module():
    """bm25는 kiwipiepy·bm25s를 끌고 오므로 실제로 쓸 때만 import."""
    import bm25 as bm25_mod
    return bm25_mod


# ── 파일 discover ───────────────────────────────────────

def _sha256_file(path: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(buf), b""):
            h.update(block)
    return h.hexdigest()


def load_emb_provenance(emb_dir: Path, dim: int, allow_unverified: bool) -> dict:
    """--emb-dir 의 run_manifest.json + _DONE.json 을 읽어 출처를 검증한다.

    가짜 벡터(dry-run 산출물)는 차원·L2 노름·chunk_id가 전부 정상이라 아래
    데이터 검증을 그대로 통과한다. 유일하게 구별되는 근거가 이 두 파일이므로
    **없으면 진행하지 않는다** (--allow-unverified-emb 로만 우회 가능).

    반환: {"model", "revision", "dim", "shard_size", "max_seq_len",
           "doc_prefix", "query_prefix", "expected_rows", "n_shards",
           "input_sha256": {name: sha}, "verified": bool}
    """
    if not emb_dir.is_dir():
        raise IngestAbort(f"--emb-dir 가 폴더가 아니다: {emb_dir}")
    man_p = emb_dir / RUN_MANIFEST_NAME
    done_p = emb_dir / DONE_NAME
    missing = [p.name for p in (man_p, done_p) if not p.exists()]
    if (emb_dir / DRY_MARKER).exists() and not allow_unverified:
        raise IngestAbort(
            f"{emb_dir}/{DRY_MARKER} 발견 — 이 폴더는 --dry-run 산출물(가짜 벡터)이다. "
            "GPU 머신에서 실제 실행한 결과를 회수할 것.")
    if missing:
        msg = (f"{emb_dir} 에 {', '.join(missing)} 가 없다 — GPU 실행 출처를 확인할 수 없다. "
               "embeddings 폴더를 통째로(매니페스트 포함) 회수했는지 확인할 것.")
        if not allow_unverified:
            raise IngestAbort(msg + " (정말 강행하려면 --allow-unverified-emb)")
        log.warning("출처 검증 생략(--allow-unverified-emb): %s", msg)
        return {"verified": False, "input_sha256": {}}

    man = json.loads(man_p.read_text(encoding="utf-8"))
    done = json.loads(done_p.read_text(encoding="utf-8"))

    problems: list[str] = []
    if man.get("dry_run") or done.get("dry_run"):
        problems.append("run_manifest/_DONE 의 dry_run=true — 가짜 벡터다")
    if not done.get("complete"):
        problems.append(f"_DONE.json complete={done.get('complete')} "
                        f"(vectors={done.get('vectors')} / "
                        f"expected={done.get('expected_rows')}) — GPU 실행 미완료")
    if man.get("dim") != dim:
        problems.append(f"run_manifest dim={man.get('dim')} ≠ --dim {dim}")
    if done.get("dim") != man.get("dim"):
        problems.append(f"_DONE dim={done.get('dim')} ≠ run_manifest dim={man.get('dim')}")
    if done.get("fingerprint") != man.get("fingerprint"):
        problems.append("_DONE 과 run_manifest 의 입력 fingerprint 불일치 "
                        "— 서로 다른 실행의 파일이 섞였다")
    if done.get("model") != man.get("model"):
        problems.append(f"_DONE model={done.get('model')!r} ≠ "
                        f"run_manifest model={man.get('model')!r}")
    if problems:
        head = f"{emb_dir} 의 GPU 실행 기록이 신뢰할 수 없다:"
        if not allow_unverified:
            raise IngestAbort(head + "\n  - " + "\n  - ".join(problems))
        log.warning("%s\n  - %s\n  → --allow-unverified-emb 로 강행", head,
                    "\n  - ".join(problems))

    info = {
        "verified": not problems,
        "model": man.get("model"),
        "revision": man.get("revision"),
        "dim": man.get("dim"),
        "shard_size": man.get("shard_size"),
        "max_seq_len": man.get("max_seq_len"),
        "doc_prefix": man.get("doc_prefix"),
        "query_prefix": man.get("query_prefix"),
        "expected_rows": man.get("total_rows"),
        "n_shards": man.get("n_shards"),
        "vectors": done.get("vectors"),
        "dtype_compute": man.get("dtype_compute"),
        "input_sha256": {i["name"]: i.get("sha256")
                         for i in man.get("inputs", []) if i.get("name")},
        "input_sha256_state": man.get("input_sha256_state"),
        "finished_at": done.get("finished_at"),
    }
    log.info("── GPU 실행 출처: model=%s rev=%s dim=%s shard_size=%s "
             "max_seq_len=%s vectors=%s (%s)",
             info["model"], (info["revision"] or "-")[:12], info["dim"],
             info["shard_size"], info["max_seq_len"], info["vectors"],
             info["finished_at"])
    return info


def check_emb_shape(prov: dict, emb_files: list[Path], emb_rows: int,
                    allow_unverified: bool) -> None:
    """회수한 샤드 수·행수가 GPU 기록과 맞는지 (부분 회수 탐지)."""
    if not prov.get("verified") and allow_unverified:
        return
    problems = []
    if prov.get("n_shards") is not None and prov["n_shards"] != len(emb_files):
        problems.append(f"샤드 수 {len(emb_files)} ≠ run_manifest n_shards "
                        f"{prov['n_shards']} — 일부만 회수됐다")
    if prov.get("expected_rows") is not None and prov["expected_rows"] != emb_rows:
        problems.append(f"벡터 행수 {emb_rows} ≠ run_manifest total_rows "
                        f"{prov['expected_rows']}")
    if problems:
        if allow_unverified:
            log.warning("회수 형상 불일치(강행): %s", "; ".join(problems))
        else:
            raise IngestAbort("; ".join(problems))


def verify_chunk_hashes(chunk_files: list[Path], prov: dict) -> list[str]:
    """로컬 chunks_*.parquet 의 sha256 을 GPU 기록과 재대조 (전송 무결성 폐회로)."""
    recorded = {k: v for k, v in (prov.get("input_sha256") or {}).items() if v}
    if not recorded:
        log.warning("run_manifest 에 입력 sha256 기록이 없다 (state=%s) — "
                    "전송 무결성 대조 생략", prov.get("input_sha256_state"))
        return []
    problems = []
    for p in chunk_files:
        want = recorded.get(p.name)
        if want is None:
            problems.append(f"{p.name}: GPU 기록에 없는 청크 파일 "
                            "— chunks 세대가 다르다")
            continue
        got = _sha256_file(p)
        if got != want:
            problems.append(f"{p.name}: sha256 불일치 (GPU {want[:16]}… / "
                            f"로컬 {got[:16]}…) — 임베딩된 텍스트와 지금 적재하려는 "
                            "텍스트가 다르다")
    absent = sorted(set(recorded) - {p.name for p in chunk_files})
    if absent:
        problems.append(f"GPU가 임베딩한 청크 파일이 로컬에 없다: "
                        f"{absent[:5]}{' ...' if len(absent) > 5 else ''}")
    if not problems:
        log.info("── 전송 무결성 OK: chunks %d개 sha256 일치 (GPU 기록 대조)",
                 len(chunk_files))
    return problems


def discover(directory: Path, patterns: tuple[str, ...], kind: str) -> list[Path]:
    if not directory.is_dir():
        raise FileNotFoundError(f"{kind} dir not found: {directory}")
    found: dict[str, Path] = {}
    for pat in patterns:
        for p in directory.glob(pat):
            if p.is_file():
                found[p.name] = p
    # 미완성 파일 방어: 원자적 쓰기의 임시파일이 남아 있으면 무시
    files = sorted(p for n, p in found.items()
                   if not n.endswith((".tmp", ".part", ".inprogress")))
    if not files:
        raise FileNotFoundError(
            f"no {kind} parquet in {directory} (patterns={list(patterns)})")
    return files


# ── 스키마 검증 (데이터 안 읽고 메타만) ──────────────────

def _is_stringy(t: pa.DataType) -> bool:
    return (pa.types.is_string(t) or pa.types.is_large_string(t)
            or pa.types.is_string_view(t))


def verify_chunk_schema(path: Path) -> tuple[int, list[str]]:
    """(rows, problems). parquet footer만 읽는다."""
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    problems = []
    for col in CHUNK_REQUIRED_COLS:
        if col not in schema.names:
            problems.append(f"{path.name}: missing column '{col}'")
            continue
        t = schema.field(col).type
        if not _is_stringy(t):
            problems.append(f"{path.name}: column '{col}' type={t}, expected string")
    return pf.metadata.num_rows, problems


def _vector_type_info(t: pa.DataType) -> tuple[str, int | None, pa.DataType | None]:
    """vector 컬럼 타입 → (kind, declared_dim, value_type)."""
    if pa.types.is_fixed_size_list(t):
        return "fixed_size_list", t.list_size, t.value_type
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_list_view(t):
        return "list", None, t.value_type
    return "invalid", None, None


def verify_emb_schema(path: Path, dim: int) -> tuple[int, str, list[str]]:
    """(rows, vector_kind, problems). parquet footer만 읽는다."""
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    problems = []
    for col in EMB_REQUIRED_COLS:
        if col not in schema.names:
            problems.append(f"{path.name}: missing column '{col}'")
    if problems:
        return pf.metadata.num_rows, "invalid", problems
    if not _is_stringy(schema.field("chunk_id").type):
        problems.append(f"{path.name}: chunk_id type="
                        f"{schema.field('chunk_id').type}, expected string")
    kind, declared, vt = _vector_type_info(schema.field("vector").type)
    if kind == "invalid":
        problems.append(f"{path.name}: vector type={schema.field('vector').type} — "
                        "fixed_size_list<float16,DIM> 또는 list<float32>여야 함")
    else:
        if declared is not None and declared != dim:
            problems.append(f"{path.name}: vector dim={declared}, expected {dim}")
        if vt is not None and not (pa.types.is_float16(vt)
                                   or pa.types.is_float32(vt)
                                   or pa.types.is_float64(vt)):
            problems.append(f"{path.name}: vector value type={vt}, expected float16/32")
    return pf.metadata.num_rows, kind, problems


# ── 벡터 → numpy ────────────────────────────────────────

def vectors_to_matrix(col, dim: int, where: str) -> np.ndarray:
    """vector 컬럼(ChunkedArray/Array) → (n, dim) float16 ndarray."""
    chunks = col.chunks if isinstance(col, pa.ChunkedArray) else [col]
    mats = []
    for arr in chunks:
        if len(arr) == 0:
            continue
        if arr.null_count:
            raise ValueError(f"{where}: vector 컬럼에 null {arr.null_count}건")
        kind, declared, vt = _vector_type_info(arr.type)
        if kind == "invalid":
            raise TypeError(f"{where}: unsupported vector type {arr.type}")
        if kind == "list":
            lens = pc.list_value_length(arr)
            lo = pc.min(lens).as_py()
            hi = pc.max(lens).as_py()
            if lo != hi or lo != dim:
                raise ValueError(f"{where}: list 길이 {lo}~{hi}, expected {dim}")
        elif declared != dim:
            raise ValueError(f"{where}: vector dim={declared}, expected {dim}")
        flat = arr.flatten().to_numpy(zero_copy_only=False)
        mats.append(np.ascontiguousarray(flat).reshape(-1, dim))
    if not mats:
        return np.zeros((0, dim), dtype=np.float16)
    mat = mats[0] if len(mats) == 1 else np.concatenate(mats, axis=0)
    return mat


def vector_stats(mat: np.ndarray, where: str) -> tuple[float, float, float]:
    """(norm_min, norm_mean, norm_max). NaN/Inf 있으면 예외."""
    if mat.size == 0:
        return (0.0, 0.0, 0.0)
    f32 = mat.astype(np.float32, copy=False)
    if not np.isfinite(f32).all():
        bad = int((~np.isfinite(f32)).any(axis=1).sum())
        raise ValueError(f"{where}: NaN/Inf 포함 행 {bad}건")
    norms = np.linalg.norm(f32, axis=1)
    return float(norms.min()), float(norms.mean()), float(norms.max())


# ── 검증 패스 ───────────────────────────────────────────

class Validation:
    def __init__(self):
        self.chunk_ids: set[str] = set()
        self.chunk_dups: list[str] = []
        self.chunk_rows = 0
        self.source_counts: dict[str, int] = {}
        self.emb_shard_of: dict[str, int] = {}
        self.emb_dups: list[str] = []
        self.emb_rows = 0
        self.problems: list[str] = []
        self.norm_samples: list[tuple[str, float, float, float]] = []
        self.vector_kinds: set[str] = set()


def validate(chunk_files: list[Path], emb_files: list[Path], dim: int,
             sample_shards: int) -> Validation:
    """chunk_id 대조 + 스키마 전수검사 + 벡터 샘플 검사."""
    v = Validation()

    log.info("── 스키마 검사 (parquet footer only)")
    for p in chunk_files:
        rows, probs = verify_chunk_schema(p)
        v.chunk_rows += rows
        v.problems += probs
    for p in emb_files:
        rows, kind, probs = verify_emb_schema(p, dim)
        v.emb_rows += rows
        v.vector_kinds.add(kind)
        v.problems += probs
    log.info("   chunks %d files / %d rows | emb %d files / %d rows | vector kind=%s",
             len(chunk_files), v.chunk_rows, len(emb_files), v.emb_rows,
             ",".join(sorted(v.vector_kinds)) or "-")
    if v.problems:
        return v

    log.info("── chunk_id 수집 (chunks 측)")
    for i, p in enumerate(chunk_files):
        tbl = pq.read_table(p, columns=["chunk_id", "source"])
        for name, cnt in zip(*_value_counts(tbl.column("source"))):
            v.source_counts[name] = v.source_counts.get(name, 0) + cnt
        for cid in tbl.column("chunk_id").to_pylist():
            if cid is None or cid == "":
                v.problems.append(f"{p.name}: chunk_id가 비어있는 행 존재")
                break
            if cid in v.chunk_ids:
                if len(v.chunk_dups) < 20:
                    v.chunk_dups.append(cid)
            else:
                v.chunk_ids.add(cid)
        del tbl
        log.info("   [%d/%d] %s → 누적 %d", i + 1, len(chunk_files), p.name,
                 len(v.chunk_ids))

    log.info("── chunk_id 수집 (emb 측)")
    for si, p in enumerate(emb_files):
        ids = pq.read_table(p, columns=["chunk_id"]).column("chunk_id").to_pylist()
        for cid in ids:
            if cid in v.emb_shard_of:
                if len(v.emb_dups) < 20:
                    v.emb_dups.append(cid)
            else:
                v.emb_shard_of[cid] = si
        log.info("   [%d/%d] %s → %d rows (누적 %d)", si + 1, len(emb_files),
                 p.name, len(ids), len(v.emb_shard_of))

    log.info("── 벡터 값 검사 (샘플 샤드)")
    for p in _sample(emb_files, sample_shards):
        col = pq.read_table(p, columns=["vector"]).column("vector")
        mat = vectors_to_matrix(col, dim, p.name)
        lo, mean, hi = vector_stats(mat, p.name)
        v.norm_samples.append((p.name, lo, mean, hi))
        log.info("   %s: %d×%d L2norm min=%.4f mean=%.4f max=%.4f",
                 p.name, mat.shape[0], mat.shape[1], lo, mean, hi)
        del mat, col
    return v


def _value_counts(col) -> tuple[list[str], list[int]]:
    vc = pc.value_counts(col.combine_chunks() if isinstance(col, pa.ChunkedArray) else col)
    names = [str(x) for x in vc.field("values").to_pylist()]
    counts = [int(x) for x in vc.field("counts").to_pylist()]
    return names, counts


def _sample(files: list[Path], n: int) -> list[Path]:
    if n < 0 or n >= len(files):
        return list(files)
    if n == 0:
        return []
    if n == 1:
        return [files[0]]
    step = (len(files) - 1) / (n - 1)
    idx = sorted({int(round(i * step)) for i in range(n)})
    return [files[i] for i in idx]


def report_validation(v: Validation, allow_duplicates: bool) -> list[str]:
    """치명적 문제 목록 반환 (빈 리스트면 통과)."""
    fatal = list(v.problems)
    for side, dups in (("chunks", v.chunk_dups), ("emb", v.emb_dups)):
        if not dups:
            continue
        msg = f"{side} 측 중복 chunk_id {len(dups)}건+ (예: {dups[:5]})"
        if allow_duplicates:
            log.warning("%s — --allow-duplicates로 무시", msg)
        else:
            fatal.append(msg)

    emb_ids = v.emb_shard_of.keys()
    missing = v.chunk_ids - emb_ids            # 벡터 없는 청크
    orphan = emb_ids - v.chunk_ids             # 청크 없는 벡터
    log.info("── chunk_id 대조: chunks=%d, emb=%d, 벡터없음=%d, 고아벡터=%d",
             len(v.chunk_ids), len(v.emb_shard_of), len(missing), len(orphan))
    if missing:
        fatal.append(f"벡터가 없는 청크 {len(missing)}건 (예: {sorted(missing)[:5]}) — "
                     "GPU 측 샤드가 덜 회수됐거나 chunks가 갱신됨")
    if orphan:
        fatal.append(f"대응 청크가 없는 벡터 {len(orphan)}건 (예: {sorted(orphan)[:5]}) — "
                     "emb/chunks 세대 불일치")
    return fatal


# ── emb 샤드 LRU 캐시 ───────────────────────────────────

class ShardCache:
    """emb 샤드를 최대 N개까지만 메모리에 유지 (샤드당 50k×1024×2B ≈ 100MB)."""

    def __init__(self, files: list[Path], dim: int, capacity: int = 3):
        self.files = files
        self.dim = dim
        self.capacity = max(1, capacity)
        self._cache: OrderedDict[int, tuple[dict[str, int], np.ndarray]] = OrderedDict()
        self.loads = 0
        self.norm_sum = 0.0
        self.norm_n = 0
        self.norm_min = float("inf")
        self.norm_max = 0.0
        self._stats_done: set[int] = set()

    def get(self, shard_idx: int) -> tuple[dict[str, int], np.ndarray]:
        hit = self._cache.get(shard_idx)
        if hit is not None:
            self._cache.move_to_end(shard_idx)
            return hit
        p = self.files[shard_idx]
        tbl = pq.read_table(p, columns=["chunk_id", "vector"])
        ids = tbl.column("chunk_id").to_pylist()
        mat = vectors_to_matrix(tbl.column("vector"), self.dim, p.name)
        del tbl
        if len(ids) != mat.shape[0]:
            raise ValueError(f"{p.name}: chunk_id {len(ids)}행 vs vector "
                             f"{mat.shape[0]}행 불일치")
        if shard_idx not in self._stats_done:
            lo, mean, hi = vector_stats(mat, p.name)   # NaN/Inf 전수 검사도 겸함
            self.norm_sum += mean * mat.shape[0]
            self.norm_n += mat.shape[0]
            self.norm_min = min(self.norm_min, lo)
            self.norm_max = max(self.norm_max, hi)
            self._stats_done.add(shard_idx)
        entry = ({cid: i for i, cid in enumerate(ids)}, mat)
        self._cache[shard_idx] = entry
        self._cache.move_to_end(shard_idx)
        self.loads += 1
        while len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
        return entry

    @property
    def norm_mean(self) -> float:
        return self.norm_sum / self.norm_n if self.norm_n else 0.0


# ── LanceDB 적재 ────────────────────────────────────────

def open_vectordb(vectordb_mod, lance_dir: Path, table: str, dim: int,
                  dtype: str, reset: bool):
    """VectorDB 오픈. 기존 테이블 차원이 다르면 비어있을 때만 자동 재생성."""
    import lancedb
    fresh = True
    if lance_dir.exists():
        db = lancedb.connect(str(lance_dir))
        if table in db.table_names():
            t = db.open_table(table)
            existing_dim = _table_dim(t)
            rows = t.count_rows()
            log.info("기존 LanceDB 테이블 '%s': %d rows, dim=%s",
                     table, rows, existing_dim)
            if reset or (existing_dim != dim and rows == 0):
                log.warning("테이블 drop 후 재생성 (reset=%s, dim %s→%d, rows=%d)",
                            reset, existing_dim, dim, rows)
                db.drop_table(table)
            elif existing_dim != dim:
                raise IngestAbort(
                    f"기존 테이블 dim={existing_dim} ≠ 새 dim={dim} 이고 "
                    f"{rows} rows가 들어있다. 구 인덱스를 버릴 거면 --reset.")
            else:
                fresh = rows == 0
    vdb = vectordb_mod.VectorDB(path=lance_dir, table_name=table,
                                dim=dim, dtype=dtype)
    return vdb, fresh


def existing_chunk_ids(vdb) -> set[str]:
    """LanceDB에 이미 들어있는 chunk_id 집합 (chunk_id 컬럼만 projection).

    2.6M행 × 16자 ≈ 250MB. vector 컬럼을 읽지 않는 게 핵심 —
    to_arrow()로 전체를 읽으면 벡터 5GB까지 딸려온다.
    """
    n = vdb.table.count_rows()
    if n == 0:
        return set()
    tbl = vdb.table.search().select(["chunk_id"]).limit(n).to_arrow()
    return set(tbl.column("chunk_id").to_pylist())


def prior_embed_config_version(manifest) -> str | None:
    """manifest.sqlite::embed_config의 최근 기록 버전 (없으면 None)."""
    row = manifest.con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='embed_config'"
    ).fetchone()
    if not row:
        return None
    row = manifest.con.execute(
        "SELECT embed_config_ver FROM embed_config ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def prior_vector_stats(manifest, sidecar: Path, version: str) -> dict | None:
    """같은 버전으로 이미 기록된 L2 노름 통계 (없으면 None).

    이어하기 실행은 샤드를 하나도 로드하지 않을 수 있고(신규 0행), 그때
    ShardCache 통계는 0/None 이다. 그 값으로 덮으면 embed_config의 노름 기록이
    '평균 0'이 되어 계약 문서로서 쓸모가 없어진다 → 기존 값을 보존한다.
    """
    def _ok(d) -> dict | None:
        if isinstance(d, dict) and d.get("l2_norm_mean"):
            return d
        return None

    try:
        row = manifest.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='embed_config'"
        ).fetchone()
        if row:
            row = manifest.con.execute(
                "SELECT payload_json FROM embed_config WHERE embed_config_ver=?",
                [version]).fetchone()
            if row and row[0]:
                got = _ok((json.loads(row[0]) or {}).get("vectors"))
                if got:
                    return got
    except Exception as e:                                  # sqlite/JSON 손상
        log.warning("이전 embed_config 노름 통계 조회 실패: %s", e)
    if sidecar.exists():
        try:
            d = json.loads(sidecar.read_text(encoding="utf-8"))
            if d.get("embed_config_version") == version:
                return _ok(d.get("vectors"))
        except json.JSONDecodeError:
            pass
    return None


def _table_dim(t) -> int | None:
    try:
        f = t.schema.field("vector")
    except KeyError:
        return None
    kind, declared, _ = _vector_type_info(f.type)
    return declared


def build_arrow_batch(vectordb_mod, chunk_ids: list[str], texts: list[str],
                      metas: list[dict], vectors: np.ndarray, dim: int,
                      dtype: str) -> pa.Table:
    """VectorDB.upsert와 동일한 스키마의 arrow Table 생성.

    bulk 적재에서 merge_insert(=full table join)를 매 배치마다 도는 걸 피하려고
    table.add() 경로에 쓴다. 스키마·메타 변환은 vectordb 모듈의 것을 그대로
    재사용해 정의가 갈라지지 않게 한다.
    """
    n = len(chunk_ids)
    keys = vectordb_mod._META_KEYS
    meta_cols: dict[str, list] = {k: [None] * n for k in keys}
    for i, meta in enumerate(metas):
        rec = vectordb_mod._meta_to_record(meta or {})
        for k in keys:
            meta_cols[k][i] = rec[k]
    vec_type = pa.float16() if dtype == "float16" else pa.float32()
    np_dtype = np.float16 if dtype == "float16" else np.float32
    vec = np.ascontiguousarray(vectors, dtype=np_dtype)
    cols = {
        "chunk_id": pa.array(chunk_ids, type=pa.string()),
        "text": pa.array(texts, type=pa.string()),
        "vector": pa.FixedSizeListArray.from_arrays(
            pa.array(vec.reshape(-1), type=vec_type), dim),
    }
    for k in keys:
        cols[k] = pa.array(meta_cols[k],
                           type=pa.int32() if k in ("age", "chunk_idx") else pa.string())
    return pa.Table.from_pydict(cols,
                                schema=vectordb_mod._arrow_schema(dim, dtype))


def manifest_items(chunk_ids: list[str], texts: list[str],
                   metas: list[dict]) -> list[dict]:
    """indexer.py::flush()와 동일한 규칙으로 manifest 행 구성."""
    out = []
    for cid, text, meta in zip(chunk_ids, texts, metas):
        meta = meta or {}
        out.append(dict(
            chunk_id=cid,
            source=meta.get("source"),
            ref_id=str(meta.get("bill_id") or meta.get("doc_id")
                       or meta.get("conf_id") or meta.get("mona_cd") or ""),
            sub_source=meta.get("sub_source") or meta.get("doc_source"),
            text_len=len(text or ""),
        ))
    return out


def _parse_meta(raw: str | None, src: str | None, cid: str, path: Path) -> dict:
    """metadata_json 문자열 → dict. source 컬럼 값으로 보강."""
    if raw:
        try:
            m = json.loads(raw)
        except json.JSONDecodeError as e:
            raise IngestAbort(f"{path.name} chunk_id={cid}: metadata_json 파싱 실패 — {e}")
        if not isinstance(m, dict):
            raise IngestAbort(f"{path.name} chunk_id={cid}: metadata_json이 object가 아님")
    else:
        m = {}
    if not m.get("source") and src:
        m["source"] = src
    return m


def ingest(chunk_files: list[Path], cache: ShardCache, emb_shard_of: dict[str, int],
           vdb, vectordb_mod, manifest, dim: int, dtype: str,
           batch_rows: int, use_add: bool,
           skip_ids: set[str] | None = None,
           dedupe: bool = False) -> tuple[int, int]:
    """chunks 파일을 스트리밍하며 벡터를 붙여 LanceDB에 적재.

    skip_ids: 이미 LanceDB에 있는 chunk_id (중단된 실행 이어하기용).
              벡터 적재만 건너뛰고 manifest 등록은 그대로 한다
              (add 직후 crash로 manifest만 빠진 경우를 메꾸기 위해).
    dedupe:   chunks 측에 중복 chunk_id가 있을 때(--allow-duplicates) 두 번째
              이후 등장분을 skip해 LanceDB에 중복 행이 생기지 않게 한다.

    반환: (적재 행수, 건너뛴 행수)
    """
    skip_ids = set(skip_ids) if (skip_ids and dedupe) else (skip_ids or set())
    total = 0
    skipped = 0
    t_start = time.time()
    for path in chunk_files:
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=batch_rows,
                                     columns=list(CHUNK_REQUIRED_COLS)):
            ids = batch.column("chunk_id").to_pylist()
            texts = batch.column("text").to_pylist()
            srcs = batch.column("source").to_pylist()
            metas_raw = batch.column("metadata_json").to_pylist()

            keep = ([i for i, cid in enumerate(ids) if cid not in skip_ids]
                    if skip_ids else list(range(len(ids))))
            if len(keep) != len(ids):
                # 이미 적재된 행: manifest만 채우고 벡터 적재는 생략
                done_idx = [i for i, cid in enumerate(ids) if cid in skip_ids]
                manifest.register_chunks(manifest_items(
                    [ids[i] for i in done_idx],
                    [texts[i] or "" for i in done_idx],
                    [_parse_meta(metas_raw[i], srcs[i], ids[i], path) for i in done_idx]))
                skipped += len(done_idx)
                ids = [ids[i] for i in keep]
                texts = [texts[i] for i in keep]
                srcs = [srcs[i] for i in keep]
                metas_raw = [metas_raw[i] for i in keep]
                if not ids:
                    continue
            n = len(ids)

            metas = [_parse_meta(metas_raw[i], srcs[i], ids[i], path)
                     for i in range(n)]

            # 샤드별로 묶어서 gather (정렬돼 있으면 샤드 1~2개만 건드림)
            vectors = np.empty((n, dim), dtype=np.float16)
            by_shard: dict[int, list[int]] = {}
            for i, cid in enumerate(ids):
                by_shard.setdefault(emb_shard_of[cid], []).append(i)
            for shard_idx in sorted(by_shard):
                id_to_row, mat = cache.get(shard_idx)
                rows = by_shard[shard_idx]
                src_rows = [id_to_row[ids[i]] for i in rows]
                vectors[rows] = mat[src_rows]

            texts = ["" if t is None else t for t in texts]
            if use_add:
                tbl = build_arrow_batch(vectordb_mod, ids, texts, metas,
                                        vectors, dim, dtype)
                vdb.table.add(tbl)
            else:
                vdb.upsert(chunk_ids=ids, texts=texts,
                           embeddings=vectors, metadatas=metas)
            manifest.register_chunks(manifest_items(ids, texts, metas))
            if dedupe:
                skip_ids.update(ids)

            total += n
            if total % (batch_rows * 20) < batch_rows:
                rate = total / max(time.time() - t_start, 0.001)
                log.info("   적재 %d행 (skip %d, %s, %.0f rows/s, shard loads=%d)",
                         total, skipped, path.name, rate, cache.loads)
    log.info("── LanceDB 적재 완료: 신규 %d행 / 기존 skip %d행, %.1f분, shard loads=%d",
             total, skipped, (time.time() - t_start) / 60, cache.loads)
    return total, skipped


# ── embed_config 기록 ───────────────────────────────────

_EMBED_CONFIG_SCHEMA = """
CREATE TABLE IF NOT EXISTS embed_config (
    embed_config_ver TEXT PRIMARY KEY,
    model_name       TEXT NOT NULL,
    dim              INTEGER NOT NULL,
    dtype            TEXT,
    metric           TEXT,
    query_prefix     TEXT,
    doc_prefix       TEXT,
    max_seq_tokens   INTEGER,
    vector_norm_mean REAL,
    n_chunks         INTEGER,
    lance_dir        TEXT,
    table_name       TEXT,
    bm25_dir         TEXT,
    emb_dir          TEXT,
    chunks_dir       TEXT,
    git_sha          TEXT,
    created_at       TIMESTAMP DEFAULT (CURRENT_TIMESTAMP),
    payload_json     TEXT
);
"""


def git_sha(repo_root: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def write_embed_config(manifest, sidecar: Path, payload: dict) -> None:
    """manifest.sqlite::embed_config + JSON 사이드카에 기록.

    질의 prefix·차원·모델명을 서빙 측에 넘기기 위한 계약 기록.
    ⚠ 2026-07-26 현재 이 기록을 읽는 서빙 코드는 없다 — rag_assembly/api.py·
    search.py·embedder.py·_subproc_embed.py 와 duckdb_mcp_server.py 는 모두
    rag_assembly/config.py 의 상수를 본다. 적재 후 SERVING_CHECKLIST.md 의
    편집을 마쳐야 검색이 새 인덱스와 맞물린다.
    사이드카 JSON은 sqlite를 열지 않고도 읽을 수 있게 같은 내용을 중복 기록한 것.
    """
    con = manifest.con
    con.executescript(_EMBED_CONFIG_SCHEMA)
    con.execute(
        "INSERT INTO embed_config (embed_config_ver, model_name, dim, dtype, metric,"
        " query_prefix, doc_prefix, max_seq_tokens, vector_norm_mean, n_chunks,"
        " lance_dir, table_name, bm25_dir, emb_dir, chunks_dir, git_sha, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(embed_config_ver) DO UPDATE SET"
        "  model_name=excluded.model_name, dim=excluded.dim, dtype=excluded.dtype,"
        "  metric=excluded.metric, query_prefix=excluded.query_prefix,"
        "  doc_prefix=excluded.doc_prefix, max_seq_tokens=excluded.max_seq_tokens,"
        "  vector_norm_mean=excluded.vector_norm_mean, n_chunks=excluded.n_chunks,"
        "  lance_dir=excluded.lance_dir, table_name=excluded.table_name,"
        "  bm25_dir=excluded.bm25_dir, emb_dir=excluded.emb_dir,"
        "  chunks_dir=excluded.chunks_dir, git_sha=excluded.git_sha,"
        "  created_at=CURRENT_TIMESTAMP, payload_json=excluded.payload_json",
        [payload["embed_config_version"], payload["model"]["name"],
         payload["model"]["dim"], payload["storage"]["dtype"],
         payload["storage"]["metric"], payload["prefix"]["query"],
         payload["prefix"]["document"], payload["model"]["max_seq_tokens"],
         payload["vectors"]["l2_norm_mean"], payload["counts"]["chunks"],
         payload["storage"]["lance_dir"], payload["storage"]["table"],
         payload["storage"]["bm25_dir"], payload["source"]["emb_dir"],
         payload["source"]["chunks_dir"], payload["git_sha"],
         json.dumps(payload, ensure_ascii=False)])
    con.commit()
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, sidecar)
    log.info("embed_config 기록: %s + manifest.sqlite::embed_config", sidecar)


# ── BM25 ────────────────────────────────────────────────

class _NoVectorTable:
    """bm25.py가 쓰는 vdb.table.to_arrow()만 흉내내되 vector 컬럼을 뺀 스캔.

    bm25.BM25Index.build()는 `vdb.table.to_arrow().select(_LOAD_COLS)`로
    전체를 읽는데, to_arrow()가 vector까지 물고 오는 게 함정이다.
    1024-dim fp16 × 263만 행 = 약 5.4GB가 필요 없이 잡힌다.
    projection을 미리 걸어 그만큼을 덜어낸다.
    """

    def __init__(self, table):
        self._t = table

    def to_arrow(self):
        cols = [f.name for f in self._t.schema if f.name != "vector"]
        n = self._t.count_rows()
        return self._t.search().select(cols).limit(n or 1).to_arrow()


class _NoVectorVDB:
    def __init__(self, vdb):
        self.table = _NoVectorTable(vdb.table)


def _bm25_source(vdb):
    """projection 스캔이 가능하면 vector를 뺀 뷰를, 아니면 원본 vdb를 준다."""
    try:
        probe = vdb.table.search().select(["chunk_id"]).limit(1).to_arrow()
        if probe.schema.names == ["chunk_id"]:
            return _NoVectorVDB(vdb)
    except Exception as e:      # lancedb 버전 차이 등
        log.warning("projection 스캔 불가(%s) — bm25가 vector까지 로드한다", e)
    return vdb


def build_bm25(bm25_root: Path, vdb, version: str, n_chunks: int,
               workers: int, force: bool) -> None:
    """rag_assembly/bm25.py의 BM25Index.build를 그대로 재사용.

    build_state.json으로 '어느 embed_config_version 위에서 만든 인덱스인가'를
    표시한다. 청킹이 바뀌면 chunk_id 자체가 바뀌므로 옛 sub-index를 resume하면
    검색 결과가 오염된다 → 버전이 다르면 강제 재빌드.
    """
    bm25_mod = load_bm25_module()
    bm25_root.mkdir(parents=True, exist_ok=True)
    state_path = bm25_root / "build_state.json"
    prior = None
    if state_path.exists():
        try:
            prior = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prior = None
    has_subindex = any(p.is_dir() and (p / "chunk_meta.pkl").exists()
                       for p in bm25_root.iterdir())
    if prior is None and has_subindex:
        log.warning("BM25 sub-index는 있는데 build_state.json이 없다 "
                    "(구 버전 인덱스로 간주) → 강제 재빌드")
        force = True
    elif prior and prior.get("embed_config_version") != version:
        log.warning("BM25 인덱스가 다른 버전(%s)으로 빌드됨 → 강제 재빌드",
                    prior.get("embed_config_version"))
        force = True
    elif prior:
        log.info("같은 버전(%s)의 BM25 상태 발견 → 완성된 sub-index는 resume",
                 version)

    state_path.write_text(json.dumps({
        "embed_config_version": version,
        "n_chunks": n_chunks,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    bm = bm25_mod.BM25Index(bm25_root)
    bm.build(_bm25_source(vdb), n_workers=workers, force=force)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ── main ────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--emb-dir", required=True, type=Path,
                   help="emb_XXXX.parquet 디렉터리 (GPU 머신에서 회수)")
    p.add_argument("--chunks-dir", required=True, type=Path,
                   help="chunks_*.parquet 디렉터리 (export_chunks.py 산출)")
    p.add_argument("--skip-bm25", action="store_true",
                   help="BM25 인덱스 빌드 생략 (벡터만 적재)")
    p.add_argument("--dry-run", action="store_true",
                   help="검증만 수행. LanceDB·manifest·BM25 일절 쓰지 않음")

    g = p.add_argument_group("임베딩 설정 (embed_config로 기록됨)")
    g.add_argument("--embed-config-version", default=DEFAULT_EMBED_CONFIG_VERSION)
    g.add_argument("--model-name", default=None,
                   help=f"기본: --emb-dir/run_manifest.json 에 기록된 실제 모델명 "
                        f"(기록이 없을 때만 {DEFAULT_MODEL_NAME}). "
                        f"명시했는데 기록과 다르면 중단한다")
    g.add_argument("--dim", type=int, default=DEFAULT_DIM)
    g.add_argument("--query-prefix", default=DEFAULT_QUERY_PREFIX,
                   help='질의 측 prefix (기본 "query: ")')
    g.add_argument("--doc-prefix", default=DEFAULT_DOC_PREFIX,
                   help="문서 측 prefix (기본 없음)")
    g.add_argument("--max-seq-tokens", type=int, default=None,
                   help=f"기본: run_manifest 의 max_seq_len "
                        f"(기록이 없을 때만 {DEFAULT_MAX_SEQ_TOKENS})")

    g2 = p.add_argument_group("출력 위치 (기본: rag_assembly/config.py)")
    g2.add_argument("--lance-dir", type=Path, default=None)
    g2.add_argument("--table", default=None)
    g2.add_argument("--bm25-dir", type=Path, default=None)
    g2.add_argument("--manifest-db", type=Path, default=None)
    g2.add_argument("--embed-config-json", type=Path, default=None)

    g3 = p.add_argument_group("적재 동작")
    g3.add_argument("--reset", action="store_true",
                    help="기존 LanceDB 테이블을 drop하고 새로 만든다")
    g3.add_argument("--upsert", action="store_true",
                    help="table.add() 대신 항상 merge_insert 사용 (증분 갱신용, 느림)")
    g3.add_argument("--no-resume", action="store_true",
                    help="같은 버전의 기존 행도 skip하지 않는다 (전량 재적재)")
    g3.add_argument("--batch-rows", type=int, default=10_000,
                    help="LanceDB 적재 배치 행수 (기본 10000)")
    g3.add_argument("--shard-cache", type=int, default=3,
                    help="메모리에 유지할 emb 샤드 수 (기본 3, 샤드당 ~100MB)")
    g3.add_argument("--check-vector-shards", type=int, default=3,
                    help="값 검사할 샘플 샤드 수 (0=스키마만, -1=전부)")
    g3.add_argument("--allow-duplicates", action="store_true",
                    help="중복 chunk_id를 치명적 오류로 보지 않음")
    g3.add_argument("--allow-unverified-emb", action="store_true",
                    help="--emb-dir 의 run_manifest.json/_DONE.json 검증 실패·부재를 "
                         "경고로 낮춘다 (가짜 벡터 적재 위험 — 상시 사용 금지)")
    g3.add_argument("--skip-hash-check", action="store_true",
                    help="chunks_*.parquet sha256 재대조 생략 (대용량일 때 시간 절약)")
    g3.add_argument("--skip-hnsw", action="store_true",
                    help="HNSW 인덱스 빌드 생략")
    g3.add_argument("--hnsw-partitions", type=int, default=256)
    g3.add_argument("--bm25-workers", type=int, default=6)
    g3.add_argument("--bm25-force", action="store_true",
                    help="완성된 BM25 sub-index도 다시 빌드")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    repo_root = Path(__file__).resolve().parents[2]
    rag_cfg, vectordb_mod, manifest_mod = load_rag_modules(repo_root)

    lance_dir = args.lance_dir or Path(rag_cfg.LANCE_DIR)
    table = args.table or rag_cfg.TABLE_NAME
    dtype = rag_cfg.VECTOR_DTYPE
    bm25_root = args.bm25_dir or Path(str(rag_cfg.BM25_PKL)).with_suffix("")
    manifest_db = args.manifest_db or Path(rag_cfg.MANIFEST_DB)
    sidecar = args.embed_config_json or (Path(rag_cfg.DATA_DIR) / "embed_config.json")
    version = args.embed_config_version

    log.info("=== ingest_embeddings ===")

    # ── 0. GPU 실행 출처 검증 (가짜 벡터·부분 회수 차단) ──
    prov = load_emb_provenance(args.emb_dir, args.dim, args.allow_unverified_emb)

    # embed_config에는 "실제로 쓰인" 모델·설정을 적는다 (CLI 기본값이 아니라).
    model_name = prov.get("model") or DEFAULT_MODEL_NAME
    if args.model_name and args.model_name != model_name:
        if prov.get("model") and not args.allow_unverified_emb:
            raise IngestAbort(
                f"--model-name {args.model_name!r} 이 GPU 기록 {prov['model']!r} 과 다르다. "
                "embed_config에 거짓 모델명을 기록하지 않도록 중단한다.")
        log.warning("모델명을 CLI 값 %r 로 강제 (GPU 기록=%r)",
                    args.model_name, prov.get("model"))
        model_name = args.model_name
    max_seq_tokens = (prov.get("max_seq_len") or args.max_seq_tokens
                      or DEFAULT_MAX_SEQ_TOKENS)
    if args.max_seq_tokens and args.max_seq_tokens != max_seq_tokens:
        log.warning("--max-seq-tokens %d 무시 — GPU 기록 %d 를 기록한다",
                    args.max_seq_tokens, max_seq_tokens)
    if prov.get("doc_prefix") is not None and prov["doc_prefix"] != args.doc_prefix:
        raise IngestAbort(
            f"문서 측 prefix 불일치: GPU={prov['doc_prefix']!r} / "
            f"--doc-prefix={args.doc_prefix!r}. 질의·문서 표현이 갈라진다.")
    if prov.get("query_prefix") is not None and prov["query_prefix"] != args.query_prefix:
        log.warning("질의 prefix가 GPU 기록(%r)과 다르다 — 서빙 계약으로 %r 를 기록",
                    prov["query_prefix"], args.query_prefix)
    args.model_name = model_name
    args.max_seq_tokens = max_seq_tokens

    log.info("model=%s dim=%d dtype=%s metric=%s max_seq_tokens=%d", model_name,
             args.dim, dtype, DEFAULT_METRIC, max_seq_tokens)
    log.info('prefix: doc=%r query=%r', args.doc_prefix, args.query_prefix)
    log.info("embed_config_version=%s", version)
    log.info("lance=%s table=%s", lance_dir, table)
    log.info("bm25=%s manifest=%s", bm25_root, manifest_db)

    chunk_files = discover(args.chunks_dir, ("chunks_*.parquet", "chunks.parquet"),
                           "chunks")
    emb_files = discover(args.emb_dir, ("emb_*.parquet",), "emb")

    v = validate(chunk_files, emb_files, args.dim, args.check_vector_shards)
    fatal = report_validation(v, args.allow_duplicates)
    check_emb_shape(prov, emb_files, v.emb_rows, args.allow_unverified_emb)
    if args.skip_hash_check:
        log.warning("--skip-hash-check: chunks sha256 재대조 생략")
    else:
        fatal += verify_chunk_hashes(chunk_files, prov)
    if v.source_counts:
        log.info("── source 분포: %s",
                 ", ".join(f"{k}={n}" for k, n in sorted(v.source_counts.items())))
    if fatal:
        log.error("검증 실패 %d건:", len(fatal))
        for f in fatal:
            log.error("  - %s", f)
        return EXIT_VALIDATION
    log.info("✓ 검증 통과: 청크 %d = 벡터 %d, chunk_id 완전 일치",
             len(v.chunk_ids), len(v.emb_shard_of))

    if args.dry_run:
        log.info("── dry-run 계획 (아무것도 쓰지 않음)")
        log.info("   LanceDB %s::%s ← %d행 (dim=%d, %s)",
                 lance_dir, table, len(v.chunk_ids), args.dim, dtype)
        if lance_dir.exists():
            import lancedb
            db = lancedb.connect(str(lance_dir))
            if table in db.table_names():
                t = db.open_table(table)
                log.info("   기존 테이블: %d rows, dim=%s → %s", t.count_rows(),
                         _table_dim(t),
                         "재생성 필요" if _table_dim(t) != args.dim else "그대로 append")
        else:
            log.info("   LanceDB 디렉터리 없음 → 새로 생성 예정")
        log.info("   manifest: %s (embed_config_ver=%s)", manifest_db, version)
        log.info("   BM25: %s (%s)", bm25_root,
                 "skip" if args.skip_bm25 else "빌드")
        log.info("   embed_config 사이드카: %s", sidecar)
        log.info("   GPU 출처: model=%s dim=%s max_seq_len=%s (verified=%s)",
                 prov.get("model"), prov.get("dim"), prov.get("max_seq_len"),
                 prov.get("verified"))
        log.info("dry-run OK")
        _warn_serving_not_wired(repo_root, args.dim)
        return EXIT_OK

    # ── 실제 적재 ──
    vdb, fresh = open_vectordb(vectordb_mod, lance_dir, table, args.dim,
                               dtype, args.reset)

    # manifest는 모듈 전역 EMBED_CONFIG_VERSION을 읽으므로 새 버전으로 고정
    rag_cfg.EMBED_CONFIG_VERSION = version
    manifest = manifest_mod.Manifest(manifest_db)

    # 이어하기 판정: 같은 embed_config_version의 이전 실행이 남긴 행만 skip 대상.
    # 버전이 다르면(=다른 모델/청킹) 같은 chunk_id라도 벡터가 다르므로 skip 금지 →
    # merge_insert로 덮어쓴다.
    skip_ids: set[str] = set()
    use_add = not args.upsert and fresh
    if not fresh and not args.upsert:
        prior = prior_embed_config_version(manifest)
        if prior == version and not args.no_resume:
            log.info("같은 버전(%s)의 기존 행 발견 → 이어하기 위해 chunk_id 조회 중",
                     version)
            skip_ids = existing_chunk_ids(vdb)
            log.info("   기존 %d행 skip 예정", len(skip_ids))
            use_add = True
        else:
            log.warning("기존 행 %s → merge_insert로 덮어쓴다",
                        "전량 재적재(--no-resume)" if prior == version
                        else f"이 다른 버전({prior})으로 적재돼 있음")
    log.info("적재 모드: %s", "table.add()" + (" (resume)" if skip_ids else " (fresh bulk)")
             if use_add else "merge_insert upsert")

    cache = ShardCache(emb_files, args.dim, capacity=args.shard_cache)
    prior_vec = prior_vector_stats(manifest, sidecar, version)
    if prior_vec:
        log.info("이전 실행의 L2 노름 통계 발견(mean=%s) — 이번에 샤드를 안 읽으면 승계",
                 prior_vec.get("l2_norm_mean"))
    sources = sorted(v.source_counts) or ["unknown"]
    run_id = manifest.open_run(sources)
    t0 = time.time()
    n_loaded = n_skipped = 0
    try:
        # embed_config를 먼저 한 번 기록해 둔다 — 중간에 죽어도 다음 실행이
        # "같은 버전의 이전 실행"임을 알아보고 이어할 수 있게.
        write_embed_config(manifest, sidecar, _payload(
            args, version, dtype, lance_dir, table, bm25_root, manifest_db,
            emb_files, chunk_files, v, cache, repo_root, n_chunks=None,
            prov=prov, prior_vectors=prior_vec))

        n_loaded, n_skipped = ingest(
            chunk_files, cache, v.emb_shard_of, vdb, vectordb_mod,
            manifest, args.dim, dtype, args.batch_rows, use_add,
            skip_ids=skip_ids,
            dedupe=bool(v.chunk_dups and args.allow_duplicates))
        log.info("LanceDB row count = %d", vdb.count())

        total_rows = vdb.count()
        write_embed_config(manifest, sidecar, _payload(
            args, version, dtype, lance_dir, table, bm25_root, manifest_db,
            emb_files, chunk_files, v, cache, repo_root, n_chunks=total_rows,
            prov=prov, prior_vectors=prior_vec))

        if not args.skip_hnsw and total_rows:
            _build_hnsw(vdb, args.dim, args.hnsw_partitions, total_rows)

        if not args.skip_bm25:
            log.info("── BM25 빌드 시작 (workers=%d)", args.bm25_workers)
            build_bm25(bm25_root, vdb, version, total_rows,
                       args.bm25_workers, args.bm25_force)
        else:
            log.warning("BM25 생략 — 하이브리드 검색은 vector-only로 degrade한다. "
                        "나중에 이 스크립트를 --skip-bm25 **없이**(적재는 이어하기로 "
                        "즉시 끝난다) 다시 돌리거나, rag_assembly/bm25.py를 직접 실행할 것")

        manifest.close_run(run_id, n_loaded, n_skipped,
                           notes=f"ingest_embeddings {version} "
                                 f"elapsed={time.time() - t0:.0f}s")
    finally:
        manifest.close()

    log.info("=== 완료: 신규 %d행 / skip %d행, %.1f분 ===",
             n_loaded, n_skipped, (time.time() - t0) / 60)
    _warn_serving_not_wired(repo_root, args.dim)
    return EXIT_OK


def _warn_serving_not_wired(repo_root: Path, dim: int) -> None:
    """적재는 끝났지만 서빙 경로는 아직 옛 설정(1536/Gemini)이라는 사실을 알린다."""
    checklist = repo_root / SERVING_CHECKLIST
    bar = "!" * 72
    log.warning("\n%s\n"
                "  이 편집 전에는 RAG 검색이 동작하지 않습니다.\n"
                "  인덱스는 %d차원(arctic-ko)으로 새로 섰지만, 서빙 코드는 아직\n"
                "  rag_assembly/config.py 의 gemini-embedding-001 / 1536 을 봅니다.\n"
                "  질의 벡터가 1536차원으로 만들어져 검색이 실패하거나 무의미해집니다.\n"
                "  → 체크리스트: %s\n"
                "%s", bar, dim, checklist, bar)


def _payload(args, version: str, dtype: str, lance_dir: Path, table: str,
             bm25_root: Path, manifest_db: Path, emb_files: list[Path],
             chunk_files: list[Path], v: Validation, cache: ShardCache,
             repo_root: Path, n_chunks: int | None,
             prov: dict | None = None,
             prior_vectors: dict | None = None) -> dict:
    """embed_config 기록 payload. 서빙 측이 지켜야 할 계약 문서이기도 하다.

    ※ 현재 서빙 코드는 이 기록을 읽지 않는다 — SERVING_CHECKLIST.md 참조.
    """
    if cache.norm_n:
        vectors = {"l2_norm_mean": round(cache.norm_mean, 6),
                   "l2_norm_min": round(cache.norm_min, 6),
                   "l2_norm_max": round(cache.norm_max, 6),
                   "measured_rows": cache.norm_n}
    elif prior_vectors:
        vectors = {**prior_vectors, "measured_rows": prior_vectors.get(
            "measured_rows", 0), "carried_over": True}
    else:
        vectors = {"l2_norm_mean": None, "l2_norm_min": None,
                   "l2_norm_max": None, "measured_rows": 0}
    return {
        "embed_config_version": version,
        "model": {
            "name": args.model_name,
            "dim": args.dim,
            "max_seq_tokens": args.max_seq_tokens,
            "tokenizer": "xlm-roberta (sentencepiece)",
            "precision": "fp16 (inference)",
        },
        # 서빙 측 계약: 질의에만 query prefix를 붙인다 (문서 측은 prefix 없음)
        "prefix": {"document": args.doc_prefix, "query": args.query_prefix},
        "storage": {
            "backend": "lancedb",
            "lance_dir": str(lance_dir),
            "table": table,
            "dtype": dtype,
            "metric": DEFAULT_METRIC,
            "bm25_dir": str(bm25_root),
            "manifest_db": str(manifest_db),
        },
        "counts": {"chunks": n_chunks if n_chunks is not None else len(v.chunk_ids),
                   "emb_shards": len(emb_files),
                   "chunk_files": len(chunk_files),
                   "by_source": v.source_counts},
        "vectors": vectors,
        "source": {"emb_dir": str(args.emb_dir.resolve()),
                   "chunks_dir": str(args.chunks_dir.resolve()),
                   "gpu_run": {k: (prov or {}).get(k) for k in
                               ("model", "revision", "dim", "shard_size",
                                "max_seq_len", "n_shards", "expected_rows",
                                "dtype_compute", "input_sha256_state",
                                "finished_at", "verified")}},
        "git_sha": git_sha(repo_root),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "built_by": "working/embed_bundle/ingest_embeddings.py",
        "status": "complete" if n_chunks is not None else "in_progress",
    }


def _build_hnsw(vdb, dim: int, num_partitions: int, n_rows: int) -> None:
    """HNSW(IVF_HNSW_SQ) 빌드.

    VectorDB.create_hnsw_index()를 쓰지 않는 이유: 그쪽은 num_sub_vectors=96이
    하드코딩돼 있는데 이는 1536차원 전용이다(1536/96=16). 1024차원에서는
    나눠떨어지지 않아 인덱스가 깨지거나 빌드가 실패한다. 여기서는 dim에서
    유도한다 (1024 → 64).
    """
    num_sub_vectors = max(1, dim // 16)
    if dim % num_sub_vectors:
        raise IngestAbort(f"dim={dim}을 num_sub_vectors={num_sub_vectors}로 "
                          "나눌 수 없다")
    # IVF는 파티션당 학습 샘플이 필요하다. 행수가 적으면 파티션을 줄인다.
    num_partitions = max(1, min(num_partitions, n_rows // 64 or 1))
    log.info("── HNSW 인덱스 빌드 (dim=%d, rows=%d, partitions=%d, sub_vectors=%d)",
             dim, n_rows, num_partitions, num_sub_vectors)
    t = time.time()
    vdb.table.create_index(
        metric="cosine",
        vector_column_name="vector",
        index_type="IVF_HNSW_SQ",
        num_partitions=num_partitions,
        num_sub_vectors=num_sub_vectors,
    )
    log.info("   HNSW 완료 %.1f분", (time.time() - t) / 60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IngestAbort as e:
        log.error("중단: %s", e)
        sys.exit(EXIT_VALIDATION)
    except KeyboardInterrupt:
        log.error("중단됨 (KeyboardInterrupt)")
        sys.exit(EXIT_ERROR)
