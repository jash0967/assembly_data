#!/usr/bin/env python3
"""GPU 머신(예: RTX A5000 24GB)에서 단독 실행되는 임베딩 워커.

chunks*.parquet  →  sentence-transformers(arctic-ko, fp16)  →  emb_XXXX.parquet

이 스크립트는 assembly_data 저장소에 의존하지 않는다(standalone).
번들 폴더(embed_remote.py + requirements.txt + README.md)와 chunks*.parquet만
GPU 머신에 옮기면 그대로 돌아간다.

━━ 입출력 계약 (export/import 스크립트와 공유) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
입력  chunks*.parquet : chunk_id(str), text(str), source(str), metadata_json(str)
                        여러 파일 허용(chunks_000.parquet, chunks_001.parquet, ...)
                        파일명 사전순 + 파일 내 행순 = 전역 행 순서(샤드 경계 결정자)
      export_manifest.json (같은 폴더, 있으면) : 파일별 sha256 → 전송 무결성 대조
출력  emb_XXXX.parquet : chunk_id(str), vector(fixed_size_list<float16, 1024>)
                        샤드당 SHARD_SIZE(50,000)행, zstd 압축
                        임시파일 → os.replace 원자적 rename (부분 파일 = 존재하지 않음)
      run_manifest.json : 입력 인벤토리 지문 + 실행 설정 (이어하기 정합성 검증용)
      _DONE.json        : 전 샤드 완료 시 기록 (회수 전 확인용)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠ 문서 측 prefix 없음 — dragonkue/snowflake-arctic-embed-l-v2.0-ko(= Snowflake
arctic-embed v2.0 계열)는 **질의(query)에만** "query: " 접두사를 붙이고 **문서
(passage)에는 어떤 접두사도 붙이지 않는** 것이 모델 카드가 규정한 올바른 사용법이다.
따라서 이 스크립트는 chunk text를 가공 없이 그대로 인코딩한다. 검색 시점 질의 임베딩
쪽에서만 QUERY_PREFIX를 붙일 것(아래 상수는 회수 측 코드가 참조하도록 남겨둔 문서용).

사용:
    python embed_remote.py --input-dir ./chunks --output-dir ./embeddings
    python embed_remote.py --input-dir ./chunks --output-dir ./embeddings --max-shards 1  # GPU 스모크
    python embed_remote.py --input-dir ./chunks --output-dir ./out_dry --dry-run          # 모델 없이 배관 검사
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# ── 계약 상수 (import 스크립트와 반드시 일치) ───────────────────────────
SHARD_SIZE = 50_000
VECTOR_DIM = 1024
VECTOR_ARROW_TYPE = pa.float16()          # fixed_size_list<float16, 1024>
DEFAULT_MODEL = "dragonkue/snowflake-arctic-embed-l-v2.0-ko"
# 문서 측(GPU)과 질의 측(로컬 서빙)이 같은 가중치를 쓰도록 리비전을 고정한다.
DEFAULT_REVISION = "55ec6e9358a56d56af759bc8372e970caf8c305f"
QUERY_PREFIX = "query: "                  # 질의 측 전용. 문서 측에는 절대 붙이지 않음.
DOC_PREFIX = ""                           # 명시적으로 비움 (모델 카드 규약)
# 모델 자체의 한계는 8192(sentence_bert_config.json). 512로 낮추면 800자 청크는
# 안전하지만 1,200~1,500자 청크(단일 청크 경로·speaker_split 버퍼)가 28~34% 잘린다.
# → 1024로 둔다. 늘어난 연산량은 긴 청크에만 붙고, OOM은 배치 자동 반감이 흡수한다.
MAX_SEQ_LEN = 1024                        # XLM-R 토크나이저 기준 (모델 한계는 8192)
INPUT_GLOB = "chunks*.parquet"
EXPORT_MANIFEST_NAME = "export_manifest.json"
SHARD_FMT = "emb_{:04d}.parquet"
TMP_FMT = ".emb_{:04d}.parquet.tmp"
MANIFEST_NAME = "run_manifest.json"
DONE_NAME = "_DONE.json"
DRY_MARKER = "_DRY_RUN"


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def fmt_eta(seconds: float) -> str:
    if seconds <= 0 or not np.isfinite(seconds):
        return "--:--:--"
    seconds = int(seconds)
    return f"{seconds // 3600:d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


# ── 입력 인벤토리 ──────────────────────────────────────────────────────

@dataclass
class InputFile:
    path: Path
    num_rows: int


def discover_inputs(input_dir: Path) -> list[InputFile]:
    """chunks*.parquet 를 파일명 사전순으로 수집. 행 수는 footer 메타만 읽는다."""
    paths = sorted(input_dir.glob(INPUT_GLOB), key=lambda p: p.name)
    if not paths:
        raise SystemExit(f"[FATAL] 입력 파케이 없음: {input_dir}/{INPUT_GLOB}")
    files: list[InputFile] = []
    for p in paths:
        pf = pq.ParquetFile(p)
        cols = set(pf.schema_arrow.names)
        missing = {"chunk_id", "text"} - cols
        if missing:
            raise SystemExit(f"[FATAL] {p.name}: 필수 컬럼 누락 {sorted(missing)} "
                             f"(있는 컬럼: {sorted(cols)})")
        files.append(InputFile(p, pf.metadata.num_rows))
    return files


def sha256_file(path: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(buf), b""):
            h.update(block)
    return h.hexdigest()


def load_export_hashes(in_dir: Path) -> tuple[dict[str, str], dict | None]:
    """export_manifest.json(있으면) → {파일명: sha256}, 원본 매니페스트 요약."""
    p = in_dir / EXPORT_MANIFEST_NAME
    if not p.exists():
        return {}, None
    try:
        man = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log(f"[WARN] {EXPORT_MANIFEST_NAME} 파싱 실패({e}) — 해시 검증 생략")
        return {}, None
    hashes = {f["file"]: f["sha256"] for f in man.get("files", [])
              if f.get("file") and f.get("sha256")}
    summary = {"format": man.get("format"), "git_sha": man.get("git_sha"),
               "created_at": man.get("created_at"),
               "total_rows": man.get("total_rows"),
               "status": man.get("status")}
    if man.get("status") not in (None, "complete"):
        raise SystemExit(
            f"[FATAL] {EXPORT_MANIFEST_NAME}: status={man.get('status')!r} — "
            f"export가 완주하지 않았습니다. 로컬에서 export를 마친 뒤 다시 보내세요.")
    return hashes, summary


def verify_input_hashes(files: list[InputFile], hashes: dict[str, str]) -> dict[str, str]:
    """전송된 chunks*.parquet의 sha256을 export_manifest 기록과 대조."""
    verified: dict[str, str] = {}
    bad: list[str] = []
    for f in files:
        want = hashes.get(f.path.name)
        got = sha256_file(f.path)
        verified[f.path.name] = got
        if want is None:
            log(f"[WARN] {f.path.name}: export_manifest에 sha256 기록 없음 — 대조 생략")
            continue
        if want != got:
            bad.append(f"{f.path.name}: 기대 {want[:16]}… / 실제 {got[:16]}…")
        else:
            log(f"  sha256 OK  {f.path.name} ({got[:16]}…)")
    if bad:
        raise SystemExit("[FATAL] 전송 중 청크 파일이 손상됐습니다 (sha256 불일치):\n  "
                         + "\n  ".join(bad)
                         + "\n  다시 전송한 뒤 실행하세요.")
    missing = sorted(set(hashes) - {f.path.name for f in files})
    if missing:
        raise SystemExit(f"[FATAL] export_manifest에 있는 청크 파일이 전송되지 않았습니다: "
                         f"{missing[:10]}{' ...' if len(missing) > 10 else ''}")
    return verified


def inventory_fingerprint(files: list[InputFile], shard_size: int) -> str:
    """입력 구성이 바뀌면 샤드 경계가 어긋난다 → 이어하기 전 지문 대조."""
    payload = json.dumps(
        {"shard_size": shard_size,
         "files": [{"name": f.path.name, "rows": f.num_rows} for f in files]},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def read_range(files: list[InputFile], start: int, length: int
               ) -> tuple[list[str], list[str]]:
    """전역 행 구간 [start, start+length) 를 읽는다.

    row group 메타로 통째 건너뛰므로 앞쪽 샤드를 재실행 없이 스킵해도 비용이 거의 없다.
    """
    ids: list[str] = []
    texts: list[str] = []
    want = length
    offset = 0
    for f in files:
        if want <= 0:
            break
        if offset + f.num_rows <= start:
            offset += f.num_rows
            continue
        pf = pq.ParquetFile(f.path)
        local_skip = max(0, start - offset)
        rg_off = 0
        for rg in range(pf.num_row_groups):
            if want <= 0:
                break
            rg_rows = pf.metadata.row_group(rg).num_rows
            if rg_off + rg_rows <= local_skip:
                rg_off += rg_rows
                continue
            tbl = pf.read_row_group(rg, columns=["chunk_id", "text"])
            if rg_off < local_skip:
                tbl = tbl.slice(local_skip - rg_off)
            if tbl.num_rows > want:
                tbl = tbl.slice(0, want)
            ids.extend(tbl.column("chunk_id").to_pylist())
            texts.extend(tbl.column("text").to_pylist())
            want -= tbl.num_rows
            rg_off += rg_rows
        offset += f.num_rows
    return ids, texts


# ── 출력 (원자적 쓰기) ─────────────────────────────────────────────────

def emb_schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("chunk_id", pa.string(), nullable=False),
        pa.field("vector", pa.list_(VECTOR_ARROW_TYPE, dim), nullable=False),
    ])


def write_shard_atomic(out_dir: Path, idx: int, ids: list[str],
                       vecs: np.ndarray, dim: int) -> Path:
    """임시파일 쓰기 → fsync → os.replace. 중간에 죽어도 emb_XXXX.parquet은 항상 완전본."""
    assert vecs.shape == (len(ids), dim), f"shape mismatch {vecs.shape} vs {(len(ids), dim)}"
    flat = np.ascontiguousarray(vecs, dtype=np.float16).reshape(-1)
    arr = pa.FixedSizeListArray.from_arrays(
        pa.array(flat, type=VECTOR_ARROW_TYPE), dim)
    tbl = pa.Table.from_arrays(
        [pa.array(ids, type=pa.string()), arr], schema=emb_schema(dim))

    tmp = out_dir / TMP_FMT.format(idx)
    final = out_dir / SHARD_FMT.format(idx)
    with open(tmp, "wb") as fh:
        pq.write_table(tbl, fh, compression="zstd")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    # 디렉터리 fsync는 POSIX 전용 — Windows는 디렉터리를 O_RDONLY로 못 열어
    # PermissionError로 죽는다. 파일 fsync + os.replace 원자성은 이미 보장되므로
    # 여기 실패는 무시해도 중단 안전성이 유지된다. (2026-07-27 Windows 실행에서 발견)
    try:
        dfd = os.open(out_dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except (PermissionError, OSError):
        pass
    return final


def completed_shards(out_dir: Path, n_shards: int, dim: int) -> set[int]:
    """이미 완성된 샤드 인덱스. footer를 실제로 열어 확인(내용 검증까지는 안 함)."""
    done: set[int] = set()
    for i in range(n_shards):
        p = out_dir / SHARD_FMT.format(i)
        if not p.exists():
            continue
        try:
            pf = pq.ParquetFile(p)
            names = set(pf.schema_arrow.names)
            if {"chunk_id", "vector"} - names:
                log(f"[WARN] {p.name}: 스키마 불일치 → 재생성 대상")
                continue
            vt = pf.schema_arrow.field("vector").type
            if not pa.types.is_fixed_size_list(vt) or vt.list_size != dim:
                log(f"[WARN] {p.name}: vector 타입 {vt} ≠ "
                    f"fixed_size_list<float16, {dim}> → 재생성 대상")
                continue
            if pf.metadata.num_rows == 0:
                log(f"[WARN] {p.name}: 0행 → 재생성 대상")
                continue
            done.add(i)
        except Exception as e:  # 손상 파일(원자적 쓰기라면 발생 안 함)
            log(f"[WARN] {p.name} 열기 실패({e}) → 재생성 대상")
    return done


def cleanup_stale_tmp(out_dir: Path) -> int:
    n = 0
    for p in out_dir.glob(".emb_*.parquet.tmp"):
        p.unlink()
        n += 1
    if n:
        log(f"이전 실행의 임시파일 {n}개 제거")
    return n


# ── 인코딩 ─────────────────────────────────────────────────────────────

class Encoder:
    """sentence-transformers 래퍼. OOM 시 배치 자동 반감."""

    def __init__(self, model_name: str, device: str, dtype: str,
                 batch_size: int, dim: int, trust_remote_code: bool,
                 max_seq_len: int, revision: str | None = None):
        import torch
        from sentence_transformers import SentenceTransformer

        if device.startswith("cuda") and not torch.cuda.is_available():
            raise SystemExit(
                "[FATAL] CUDA를 못 찾음(torch.cuda.is_available()=False). "
                "CPU 휠이 설치됐을 가능성이 큽니다 — README 2절의 CUDA 휠 설치를 확인하거나 "
                "정말 CPU로 돌리려면 --device cpu (매우 느림).")
        self.torch = torch
        self.batch_size = batch_size
        self.dim = dim
        self.min_batch = 1

        torch_dtype = {"float16": torch.float16,
                       "bfloat16": torch.bfloat16,
                       "float32": torch.float32}[dtype]
        kwargs = dict(device=device, model_kwargs={"torch_dtype": torch_dtype})
        if trust_remote_code:
            kwargs["trust_remote_code"] = True
        if revision:
            kwargs["revision"] = revision
        log(f"모델 로드: {model_name} (device={device}, dtype={dtype}"
            f"{f', revision={revision}' if revision else ''})")
        try:
            self.model = SentenceTransformer(model_name, **kwargs)
        except Exception as e:
            if not trust_remote_code and "trust_remote_code" in str(e):
                log("trust_remote_code 필요 → 재시도")
                kwargs["trust_remote_code"] = True
                self.model = SentenceTransformer(model_name, **kwargs)
            else:
                raise
        self.model.max_seq_length = max_seq_len
        self.model.eval()
        log(f"모델 준비 완료 (max_seq_length={self.model.max_seq_length})")

    def _is_oom(self, e: BaseException) -> bool:
        oom_cls = getattr(self.torch.cuda, "OutOfMemoryError", None)
        if oom_cls is not None and isinstance(e, oom_cls):
            return True
        return "out of memory" in str(e).lower()

    def encode(self, texts: list[str]) -> np.ndarray:
        """정규화된 fp16 벡터 (N, dim). OOM이면 배치 반감 후 재시도."""
        while True:
            try:
                # 문서 측: prefix 없이 원문 그대로 (모델 카드 규약)
                vecs = self.model.encode(
                    texts,
                    batch_size=self.batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
                return np.asarray(vecs, dtype=np.float16)
            except Exception as e:
                if not self._is_oom(e) or self.batch_size <= self.min_batch:
                    raise
                new_bs = max(self.min_batch, self.batch_size // 2)
                log(f"[OOM] batch_size {self.batch_size} → {new_bs} 로 낮추고 재시도")
                self.batch_size = new_bs
                self.torch.cuda.empty_cache()


class DryEncoder:
    """모델 로드 없이 파이프라인·이어하기·원자적 쓰기만 검사하는 가짜 인코더."""

    def __init__(self, dim: int, batch_size: int):
        self.dim = dim
        self.batch_size = batch_size

    def encode(self, texts: list[str]) -> np.ndarray:
        rng = np.random.default_rng(len(texts))
        v = rng.standard_normal((len(texts), self.dim), dtype=np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True) + 1e-12
        return v.astype(np.float16)


# ── 메인 ───────────────────────────────────────────────────────────────

def guard_dry_run_output(out_dir: Path) -> None:
    """--dry-run 이 실제 산출물 폴더를 덮는 사고를 막는다 (--force 로도 못 뚫는다).

    가짜 벡터는 L2 norm·차원·chunk_id가 모두 정상이라 회수 측 검증을 그대로
    통과한다. 게다가 dry-run 이 남기는 매니페스트(dry_run=true)·_DRY_RUN 마커는
    이후 정상 재실행까지 막는다. 실수 한 번의 대가가 너무 크므로 무조건 거부.
    """
    shards = sorted(out_dir.glob("emb_*.parquet"))
    man_path = out_dir / MANIFEST_NAME
    prior_dry: bool | None = None      # None = 판단 불가
    if man_path.exists():
        try:
            prior_dry = bool(json.loads(
                man_path.read_text(encoding="utf-8")).get("dry_run", False))
        except json.JSONDecodeError:
            prior_dry = None           # 읽을 수 없으면 실제 실행으로 간주(보수적)
    elif (out_dir / DRY_MARKER).exists():
        prior_dry = True
    if prior_dry:                      # 이미 dry-run 전용 폴더 → 이어하기 허용
        return
    if not shards and prior_dry is None and not man_path.exists():
        return                         # 빈 폴더 (또는 새 폴더)
    why = []
    if shards:
        why.append(f"emb_*.parquet {len(shards)}개 (예: {shards[0].name})")
    if prior_dry is False:
        why.append(f"{MANIFEST_NAME}(dry_run=false)")
    elif man_path.exists():
        why.append(f"{MANIFEST_NAME}(읽을 수 없음 → 실제 실행으로 간주)")
    raise SystemExit(
        f"[FATAL] --dry-run 을 실제 산출물 폴더에 쓰려 했습니다: {out_dir}\n"
        f"  발견: {', '.join(why)}\n"
        f"  dry-run은 가짜 벡터를 쓰며, 그 벡터는 회수 측 검증을 그대로 통과합니다.\n"
        f"  반드시 별도 폴더를 쓰세요 (예: --output-dir /tmp/out_dry). "
        f"이 검사는 --force 로도 우회되지 않습니다."
    )


def load_or_create_manifest(out_dir: Path, meta: dict, force: bool) -> dict:
    path = out_dir / MANIFEST_NAME
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        drift = [k for k in ("fingerprint", "dim", "shard_size", "model",
                             "revision", "max_seq_len")
                 if old.get(k) != meta.get(k)]
        if drift and not force:
            old_vals = {k: old.get(k) for k in drift}
            new_vals = {k: meta.get(k) for k in drift}
            raise SystemExit(
                f"[FATAL] 출력 폴더의 기존 실행과 설정이 다름: {drift}\n"
                f"  기존: {old_vals}\n"
                f"  현재: {new_vals}\n"
                f"  샤드 경계가 어긋나므로 이어하기 불가. 다른 --output-dir 를 쓰거나 "
                f"기존 폴더를 지우고 다시 실행하세요(정말 덮어쓰려면 --force)."
            )
        if old.get("dry_run") and not meta.get("dry_run") and not force:
            raise SystemExit(
                f"[FATAL] {out_dir} 는 --dry-run 결과물(가짜 벡터)입니다. "
                f"실제 실행은 다른 폴더에 하거나 이 폴더를 삭제하세요."
            )
        meta["created_at"] = old.get("created_at", meta["created_at"])
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(
        description="chunks*.parquet → arctic-ko 임베딩 → emb_XXXX.parquet",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--input-dir", type=Path, default=Path("./chunks"),
                    help="chunks*.parquet 이 있는 폴더")
    ap.add_argument("--output-dir", type=Path, default=Path("./embeddings"),
                    help="emb_XXXX.parquet 을 쓸 폴더")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="HF 모델 ID 또는 로컬 경로(오프라인 사전 다운로드 시)")
    ap.add_argument("--revision", default=DEFAULT_REVISION,
                    help="HF 리비전(커밋 SHA) 고정. 로컬 경로 모델이면 무시됨")
    ap.add_argument("--batch-size", type=int, default=64,
                    help="GPU 인코딩 배치. OOM 시 자동 반감")
    ap.add_argument("--group-size", type=int, default=4096,
                    help="한 번의 encode() 호출에 넘길 텍스트 수(진행률 갱신 단위)")
    ap.add_argument("--shard-size", type=int, default=SHARD_SIZE,
                    help="샤드당 청크 수 (계약값 50000 — 변경 금지 권장)")
    ap.add_argument("--dim", type=int, default=VECTOR_DIM, help="임베딩 차원")
    ap.add_argument("--device", default="cuda", help="cuda | cuda:0 | cpu")
    ap.add_argument("--dtype", default="float16",
                    choices=["float16", "bfloat16", "float32"],
                    help="모델 연산 dtype (저장은 항상 float16)")
    ap.add_argument("--max-seq-len", type=int, default=MAX_SEQ_LEN)
    ap.add_argument("--trust-remote-code", action="store_true",
                    help="모델이 커스텀 코드 요구 시 (실패하면 자동 재시도도 함)")
    ap.add_argument("--max-shards", type=int, default=None,
                    help="이번 실행에서 처리할 최대 샤드 수(스모크 테스트용)")
    ap.add_argument("--resume", dest="resume", action="store_true", default=True,
                    help="완성된 emb_XXXX.parquet 은 건너뜀 (기본 on)")
    ap.add_argument("--no-resume", dest="resume", action="store_false",
                    help="전 샤드 재생성")
    ap.add_argument("--dry-run", action="store_true",
                    help="모델 로드 없이 파이프라인·이어하기·원자적 쓰기만 검사 "
                         "(실제 산출물이 있는 폴더에는 쓸 수 없음)")
    ap.add_argument("--no-verify-hashes", dest="verify_hashes",
                    action="store_false", default=True,
                    help="export_manifest.json 의 sha256 대조 생략 (전송 무결성 포기)")
    ap.add_argument("--force", action="store_true",
                    help="출력 폴더의 기존 run_manifest 와 설정이 달라도 강행")
    args = ap.parse_args()

    in_dir: Path = args.input_dir.expanduser().resolve()
    out_dir: Path = args.output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        guard_dry_run_output(out_dir)

    # 로컬 경로 모델에는 리비전 개념이 없다 (HF 허브 전용).
    if args.revision and Path(args.model).expanduser().exists():
        log(f"로컬 경로 모델({args.model}) — --revision 무시")
        args.revision = None

    files = discover_inputs(in_dir)
    total_rows = sum(f.num_rows for f in files)
    shard_size = args.shard_size
    n_shards = (total_rows + shard_size - 1) // shard_size
    fp = inventory_fingerprint(files, shard_size)

    log(f"입력 {len(files)}개 파일 / {total_rows:,}행 → 샤드 {n_shards}개 "
        f"(샤드당 {shard_size:,}) | fingerprint={fp}")
    for f in files:
        log(f"  - {f.path.name}: {f.num_rows:,}행")

    # 전송 무결성: export_manifest.json 의 sha256 과 대조하고, 그 값을
    # run_manifest.json 에 실어 회수 측(ingest)이 로컬 원본과 재대조하게 한다.
    export_hashes, export_summary = load_export_hashes(in_dir)
    input_hashes: dict[str, str] = {}
    hash_state = "skipped"
    if not export_hashes:
        log(f"[WARN] {in_dir/EXPORT_MANIFEST_NAME} 없음 — 전송 무결성 대조 불가")
        hash_state = "no_export_manifest"
    elif not args.verify_hashes:
        input_hashes = dict(export_hashes)
        hash_state = "copied_unverified"
        log("[WARN] --no-verify-hashes: sha256 대조 생략, 기록만 승계")
    else:
        log(f"sha256 대조 중 ({len(files)}개 파일)…")
        input_hashes = verify_input_hashes(files, export_hashes)
        hash_state = "verified"

    meta = {
        "fingerprint": fp,
        "model": args.model,
        "revision": args.revision,
        "dim": args.dim,
        "dtype_compute": args.dtype,
        "dtype_stored": "float16",
        "vector_arrow_type": f"fixed_size_list<float16, {args.dim}>",
        "shard_size": shard_size,
        "total_rows": total_rows,
        "n_shards": n_shards,
        "doc_prefix": DOC_PREFIX,
        "query_prefix": QUERY_PREFIX,
        "max_seq_len": args.max_seq_len,
        "inputs": [{"name": f.path.name, "rows": f.num_rows,
                    "sha256": input_hashes.get(f.path.name)} for f in files],
        "input_sha256_state": hash_state,   # verified | copied_unverified | no_export_manifest
        "export_manifest": export_summary,
        "dry_run": bool(args.dry_run),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    load_or_create_manifest(out_dir, meta, args.force)
    if args.dry_run:
        (out_dir / DRY_MARKER).write_text(
            "이 폴더는 --dry-run 산출물(가짜 벡터)입니다. 회수하지 마세요.\n",
            encoding="utf-8")

    cleanup_stale_tmp(out_dir)
    done = completed_shards(out_dir, n_shards, args.dim) if args.resume else set()
    todo = [i for i in range(n_shards) if i not in done]
    if args.max_shards is not None:
        todo = todo[:args.max_shards]
    if done:
        log(f"이어하기: 완료 {len(done)}/{n_shards} 샤드 스킵")
    if not todo:
        log("할 일 없음 — 모든 샤드 완료 상태")
        write_done(out_dir, n_shards, total_rows, meta)
        return 0
    rows_todo = sum(min(shard_size, total_rows - i * shard_size) for i in todo)
    log(f"이번 실행 대상: 샤드 {len(todo)}개 / {rows_todo:,}행")

    if args.dry_run:
        enc = DryEncoder(args.dim, args.batch_size)
        log("[DRY-RUN] 모델 로드 생략, 가짜 정규화 벡터 사용")
    else:
        enc = Encoder(args.model, args.device, args.dtype, args.batch_size,
                      args.dim, args.trust_remote_code, args.max_seq_len,
                      revision=args.revision)

    t0 = time.time()
    rows_done = 0
    for n_i, idx in enumerate(todo, 1):
        start = idx * shard_size
        length = min(shard_size, total_rows - start)
        s_t0 = time.time()
        ids, texts = read_range(files, start, length)
        if len(ids) != length:
            raise SystemExit(f"[FATAL] 샤드 {idx}: {length}행 기대, {len(ids)}행 읽음")
        dup = len(ids) - len(set(ids))
        if dup:
            log(f"[WARN] 샤드 {idx}: 샤드 내 중복 chunk_id {dup}건 (그대로 기록)")
        texts = [t if isinstance(t, str) and t.strip() else " " for t in texts]

        parts: list[np.ndarray] = []
        for g0 in range(0, len(texts), args.group_size):
            group = texts[g0:g0 + args.group_size]
            v = enc.encode(group)
            if v.shape[1] != args.dim:  # 첫 그룹에서 즉시 발각 (샤드 전체 낭비 방지)
                raise SystemExit(
                    f"[FATAL] 임베딩 차원 {v.shape[1]} ≠ 기대 {args.dim}. "
                    f"모델({args.model})이 맞는지 확인하세요.")
            parts.append(v)
            gdone = rows_done + g0 + len(group)
            el = time.time() - t0
            rate = gdone / el if el > 0 else 0.0
            eta = (rows_todo - gdone) / rate if rate > 0 else float("inf")
            log(f"  shard {idx:04d} [{n_i}/{len(todo)}] "
                f"{g0 + len(group):,}/{len(texts):,} | "
                f"전체 {gdone:,}/{rows_todo:,} ({100 * gdone / rows_todo:.1f}%) | "
                f"{rate:.0f} chunk/s | ETA {fmt_eta(eta)} | bs={getattr(enc, 'batch_size', '-')}")

        vecs = np.vstack(parts) if len(parts) > 1 else parts[0]
        if vecs.shape[1] != args.dim:
            raise SystemExit(
                f"[FATAL] 임베딩 차원 {vecs.shape[1]} ≠ 기대 {args.dim}. "
                f"--dim 을 맞추거나 모델을 확인하세요.")
        path = write_shard_atomic(out_dir, idx, ids, vecs, args.dim)
        rows_done += len(ids)
        s_el = time.time() - s_t0
        log(f"  → {path.name} 저장 ({len(ids):,}행, {s_el / 60:.1f}분, "
            f"{path.stat().st_size / 1e6:.1f} MB)")

    el = time.time() - t0
    log(f"완료: {rows_done:,}행 / {el / 60:.1f}분 "
        f"({rows_done / el if el else 0:.0f} chunk/s)")

    final_done = completed_shards(out_dir, n_shards, args.dim)
    if len(final_done) == n_shards:
        write_done(out_dir, n_shards, total_rows, meta)
        log(f"전 샤드({n_shards}) 완료 → {DONE_NAME} 기록. "
            f"이 폴더를 통째로 회수하세요.")
    else:
        missing = sorted(set(range(n_shards)) - final_done)
        log(f"남은 샤드 {len(missing)}개: {missing[:10]}{' ...' if len(missing) > 10 else ''} "
            f"— 같은 명령으로 재실행하면 이어서 진행합니다.")
    return 0


def write_done(out_dir: Path, n_shards: int, total_rows: int, meta: dict) -> None:
    vec_rows = 0
    for i in range(n_shards):
        p = out_dir / SHARD_FMT.format(i)
        if p.exists():
            vec_rows += pq.ParquetFile(p).metadata.num_rows
    payload = {
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "n_shards": n_shards,
        "vectors": vec_rows,
        "expected_rows": total_rows,
        "complete": vec_rows == total_rows,
        "model": meta["model"],
        "revision": meta.get("revision"),
        "dim": meta["dim"],
        "shard_size": meta["shard_size"],
        "max_seq_len": meta["max_seq_len"],
        "doc_prefix": meta["doc_prefix"],
        "query_prefix": meta["query_prefix"],
        "vector_arrow_type": meta["vector_arrow_type"],
        "fingerprint": meta["fingerprint"],
        "input_sha256_state": meta.get("input_sha256_state"),
        "dry_run": meta["dry_run"],
    }
    (out_dir / DONE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
