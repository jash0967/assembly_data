"""DuckDB → chunks_NNN.parquet 내보내기 (로컬 임베딩 번들 1/3).

기존 RAG 인덱서(rag_assembly/indexer.py + chunker.py)의 **청킹·chunk_id 체계를
그대로 재사용**해서, 소스별 청크를 parquet로 떨군다. 임베딩은 하지 않는다.
GPU 머신(별도 장비)이 이 parquet를 받아 emb_XXXX.parquet를 만들고,
회수 후 BM25·manifest·LanceDB 빌드가 chunk_id로 조인한다.
→ 청킹 로직을 절대 재구현하지 말 것. 여기서는 import만 한다.

재사용하는 것 (import, 복사 아님):
  - chunker.Chunk / chunker._chunk_id / char_chunks / speaker_split /
    chunks_from_{bill,bill_meta,document,speech,member}
  - indexer.SOURCES, indexer.SOURCE_FUNCS (iter_bills / iter_bill_metas /
    iter_documents / iter_speeches / iter_members) — 소스별 SQL 정본
  - rag_assembly/config.py (CHUNK_SIZE=800, CHUNK_OVERLAP=200,
    SHORT_DOC_THRESHOLD=1500, RAW_DB_PATH)

배제하는 것: embedder.Embedder(Vertex AI/Gemini 호출), vectordb.VectorDB(LanceDB),
manifest.Manifest(SQLite). indexer.py가 모듈 최상단에서 이 셋을 import하므로
import 전에 sys.modules에 stub을 심어 Gemini/LanceDB 의존성을 차단한다
(§_install_stubs). 스텁 심볼을 실제로 호출하면 즉시 RuntimeError.

출력 계약 (3-스크립트 공유):
  chunks_NNN.parquet : chunk_id(str), text(str), source(str), metadata_json(str)
                       zstd 압축, 파일당 --rows-per-file 행, 원자적 쓰기(.tmp→rename)
  export_manifest.json : 파일 목록·sha256·소스별 통계·청킹 파라미터·git SHA
                       ※ chunks_*.parquet 와 함께 GPU 머신으로 보낼 것 —
                         embed_remote.py 가 sha256을 대조하고 그 값을
                         run_manifest.json 에 실어 ingest가 재대조한다.

정본 indexer와의 의도적 차이 (딱 하나): 공백만 있는 청크(text.strip()=='')를
드롭한다. 정본 indexer는 그것도 임베딩한다. 빈도가 극히 낮고 검색에 무의미하므로
유지하되, manifest의 divergence_from_indexer / per_source.empty_dropped 에 남긴다.

이어하기 없음: 중간에 죽으면 --overwrite 로 처음부터 다시 돌려야 하고, 그 플래그는
완성된 chunks_*.parquet 를 전부 지운다. 263만 청크 전량 export는 대략 수십 분
규모이므로 감수한다 (소스별 분할 실행이 필요하면 --sources 로 나눠 서로 다른
--output-dir 에 떨군 뒤 파일명을 이어붙일 것 — 파일명 사전순이 곧 전역 행 순서다).

Usage:
    .venv/bin/python working/embed_bundle/export_chunks.py --output-dir OUT
    .venv/bin/python working/embed_bundle/export_chunks.py --sources bill member
    .venv/bin/python working/embed_bundle/export_chunks.py --limit 100   # 스모크
    .venv/bin/python working/embed_bundle/export_chunks.py --db /path/fake.duckdb
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

# ── rag_assembly 모듈 재사용 준비 ──────────────────────────
# rag_assembly/*.py 는 flat import(`import chunker`, `import config`)를 쓰고,
# rag_assembly/config.py 와 repo root config.py 가 이름이 겹친다.
# rag_assembly 디렉터리를 sys.path[0]에 넣어야 chunker가 rag 쪽 config
# (CHUNK_SIZE 등)을 집는다. 순서를 바꾸면 조용히 repo root config를 잡고
# AttributeError가 난다. (_bootstrap.py는 rag_assembly 안에 config.py가
# 있어 사실상 no-op이다.)
_THIS = Path(__file__).resolve()
REPO_ROOT = _THIS.parents[2]
RAG_DIR = Path(os.environ.get("RAG_ASSEMBLY_DIR", REPO_ROOT / "rag_assembly")).resolve()
if not (RAG_DIR / "chunker.py").exists():
    raise SystemExit(f"rag_assembly not found at {RAG_DIR} (set RAG_ASSEMBLY_DIR)")
sys.path.insert(0, str(RAG_DIR))


def _install_stubs() -> None:
    """indexer.py가 import하는 임베딩·저장 계층을 무해한 stub으로 대체.

    export는 청킹·메타만 필요하다. 실물 embedder는 import만으로
    google-genai를 끌어오고 GOOGLE_* 환경변수를 세팅하며, vectordb는
    lancedb를 로드한다. 둘 다 이 스크립트에 불필요하고, GPU 머신 이관
    번들의 의존성을 오염시킨다.
    """
    for mod_name, symbols in (
        ("embedder", ("Embedder",)),
        ("manifest", ("Manifest",)),
        ("vectordb", ("VectorDB",)),
    ):
        if mod_name in sys.modules:
            continue
        stub = types.ModuleType(mod_name)
        stub.__doc__ = "stub installed by export_chunks.py (embedding path excluded)"
        stub.__EXPORT_CHUNKS_STUB__ = True
        for sym in symbols:
            def _blocked(*_a, _who=f"{mod_name}.{sym}", **_kw):
                raise RuntimeError(
                    f"{_who} is stubbed out in export_chunks.py — this script "
                    "only chunks; embedding runs on the GPU machine."
                )
            setattr(stub, sym, _blocked)
        sys.modules[mod_name] = stub


_install_stubs()

import duckdb                      # noqa: E402
import pyarrow as pa               # noqa: E402
import pyarrow.parquet as pq       # noqa: E402

import config as rag_cfg           # noqa: E402  (rag_assembly/config.py)
import chunker                     # noqa: E402
import indexer                     # noqa: E402

# 잘못된 config를 잡았으면 조용히 틀린 청크를 만들지 말고 즉시 죽는다.
_resolved_cfg = Path(chunker.cfg.__file__).resolve()
if _resolved_cfg != (RAG_DIR / "config.py").resolve():
    raise SystemExit(
        f"chunker bound the wrong config module: {_resolved_cfg}\n"
        f"expected {(RAG_DIR / 'config.py').resolve()} — sys.path order is broken."
    )

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("export_chunks")

SOURCES: list[str] = list(indexer.SOURCES)   # bill, bill_meta, document, speech, member

PARQUET_SCHEMA = pa.schema([
    pa.field("chunk_id", pa.string(), nullable=False),
    pa.field("text", pa.string(), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("metadata_json", pa.string(), nullable=False),
])

MANIFEST_NAME = "export_manifest.json"
FILE_PREFIX = "chunks"
EXPORT_FORMAT_VERSION = "chunks-parquet-1"


# ── DuckDB 스트리밍 어댑터 ─────────────────────────────────

class _StreamingResult:
    """indexer의 `con.execute(sql).fetchall()` 관용구를 스트리밍으로 바꾼다.

    indexer.iter_*는 fetchall()로 전체 행을 파이썬 메모리에 올린다.
    document_text(회의록 26.5K행, 행당 수백 KB)에서는 수 GB를 먹는다.
    fetchall()이 리스트 대신 제너레이터를 돌려줘도 호출부(`for row in ...`)는
    그대로 동작하므로, SQL은 정본을 그대로 쓰면서 메모리만 상수로 묶는다.
    """

    def __init__(self, con, batch: int, counter: dict):
        self._con = con
        self._batch = batch
        self._counter = counter

    def fetchall(self):
        while True:
            rows = self._con.fetchmany(self._batch)
            if not rows:
                return
            self._counter["rows"] += len(rows)
            yield from rows


class StreamingCon:
    """DuckDB 연결 proxy. execute()만 노출하고 결과를 스트리밍한다."""

    def __init__(self, con, batch: int = 256):
        self._con = con
        self._batch = batch
        self.counter = {"rows": 0}

    def execute(self, sql, *args, **kwargs):
        self._con.execute(sql, *args, **kwargs)
        return _StreamingResult(self._con, self._batch, self.counter)


# ── 통계 ──────────────────────────────────────────────────

_LEN_BUCKETS = (200, 400, 600, 800, 1000, 1200, 1400, 1600, 2000)


class LenStats:
    """청크 문자 길이 분포 (전량 보관 없이 히스토그램만)."""

    def __init__(self):
        self.n = 0
        self.total = 0
        self.max = 0
        self.hist = [0] * (len(_LEN_BUCKETS) + 1)

    def add(self, length: int) -> None:
        self.n += 1
        self.total += length
        if length > self.max:
            self.max = length
        for i, edge in enumerate(_LEN_BUCKETS):
            if length <= edge:
                self.hist[i] += 1
                return
        self.hist[-1] += 1

    def as_dict(self) -> dict:
        labels = []
        prev = 0
        for edge in _LEN_BUCKETS:
            labels.append(f"{prev + 1}-{edge}")
            prev = edge
        labels.append(f"{prev + 1}+")
        return {
            "chunks": self.n,
            "chars_total": self.total,
            "chars_mean": round(self.total / self.n, 1) if self.n else 0.0,
            "chars_max": self.max,
            "hist": dict(zip(labels, self.hist)),
        }


# ── parquet shard writer ──────────────────────────────────

def _sha256_file(path: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(buf), b""):
            h.update(block)
    return h.hexdigest()


class ParquetShardWriter:
    """chunks_NNN.parquet 롤오버 writer.

    - row_group_size 행마다 row group flush (메모리 상한)
    - rows_per_file 행마다 파일 롤오버
    - 파일은 `.tmp`에 쓰고 완성 시 os.replace → 부분 파일이 최종 이름을
      가지지 않는다 (GPU 머신 전송·이어하기 판정의 전제)
    """

    def __init__(self, out_dir: Path, rows_per_file: int, row_group_size: int,
                 compression: str = "zstd", compression_level: int = 3,
                 hash_files: bool = True, prefix: str = FILE_PREFIX):
        self.out_dir = out_dir
        self.rows_per_file = rows_per_file
        self.row_group_size = row_group_size
        self.compression = compression
        self.compression_level = compression_level
        self.hash_files = hash_files
        self.prefix = prefix

        self.files: list[dict] = []
        self._writer: pq.ParquetWriter | None = None
        self._tmp_path: Path | None = None
        self._final_path: Path | None = None
        self._rows_in_file = 0
        self._file_idx = 0
        self._buf: dict[str, list] = {c: [] for c in PARQUET_SCHEMA.names}
        self._buf_n = 0
        self.total_rows = 0

    # 내부 -------------------------------------------------
    def _open_file(self) -> None:
        self._final_path = self.out_dir / f"{self.prefix}_{self._file_idx:03d}.parquet"
        self._tmp_path = self._final_path.with_suffix(".parquet.tmp")
        self._writer = pq.ParquetWriter(
            self._tmp_path, PARQUET_SCHEMA,
            compression=self.compression,
            compression_level=self.compression_level,
        )
        self._rows_in_file = 0
        log.info("writing %s", self._final_path.name)

    def _flush_buf(self) -> None:
        if not self._buf_n:
            return
        if self._writer is None:
            self._open_file()
        table = pa.Table.from_pydict(self._buf, schema=PARQUET_SCHEMA)
        self._writer.write_table(table, row_group_size=self.row_group_size)
        self._rows_in_file += self._buf_n
        self._buf = {c: [] for c in PARQUET_SCHEMA.names}
        self._buf_n = 0

    def _close_file(self) -> None:
        if self._writer is None:
            return
        self._writer.close()
        self._writer = None
        entry = {
            "file": self._final_path.name,
            "rows": self._rows_in_file,
            "bytes": self._tmp_path.stat().st_size,
        }
        if self.hash_files:
            entry["sha256"] = _sha256_file(self._tmp_path)
        os.replace(self._tmp_path, self._final_path)   # 원자적 확정
        self.files.append(entry)
        log.info("  finished %s (%d rows, %.1f MB)",
                 entry["file"], entry["rows"], entry["bytes"] / 1e6)
        self._file_idx += 1
        self._tmp_path = None
        self._final_path = None
        self._rows_in_file = 0

    # 공개 -------------------------------------------------
    def add(self, chunk_id: str, text: str, source: str, metadata_json: str) -> None:
        self._buf["chunk_id"].append(chunk_id)
        self._buf["text"].append(text)
        self._buf["source"].append(source)
        self._buf["metadata_json"].append(metadata_json)
        self._buf_n += 1
        self.total_rows += 1
        # row group 상한이거나 파일 상한에 정확히 닿으면 flush
        if (self._buf_n >= self.row_group_size
                or self._rows_in_file + self._buf_n >= self.rows_per_file):
            self._flush_buf()
        if self._rows_in_file >= self.rows_per_file:
            self._close_file()

    def close(self) -> list[dict]:
        self._flush_buf()
        self._close_file()
        return self.files

    def abort(self) -> None:
        """예외 발생 시 미완성 .tmp 제거 (최종 이름은 절대 남기지 않는다)."""
        try:
            if self._writer is not None:
                self._writer.close()
        except Exception:
            pass
        self._writer = None
        if self._tmp_path and self._tmp_path.exists():
            self._tmp_path.unlink()


# ── 메타 헬퍼 ─────────────────────────────────────────────

def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _sha256_text(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _dump_meta(meta: dict) -> str:
    return json.dumps(meta, ensure_ascii=False, separators=(",", ":"), default=str)


def _write_manifest(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


# ── 메인 export ───────────────────────────────────────────

def export(out_dir: Path, sources: list[str], db_path: Path,
           limit: int | None = None, rows_per_file: int = 200_000,
           row_group_size: int = 20_000, fetch_batch: int = 256,
           compression: str = "zstd", compression_level: int = 3,
           hash_files: bool = True, dedup: bool = True,
           progress: bool = True) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from tqdm import tqdm
    except ImportError:                                   # pragma: no cover
        tqdm = None

    try:
        con = duckdb.connect(str(db_path), read_only=True)    # 읽기 전용 고정
    except duckdb.IOException as e:
        raise SystemExit(
            f"cannot open {db_path} read-only: {e}\n"
            "DuckDB는 다른 프로세스가 write lock을 쥐고 있으면 read_only 연결도 막는다. "
            "쓰기 중인 파이프라인이 끝난 뒤 다시 실행할 것."
        ) from e
    writer = ParquetShardWriter(
        out_dir, rows_per_file=rows_per_file, row_group_size=row_group_size,
        compression=compression, compression_level=compression_level,
        hash_files=hash_files,
    )

    seen: set[int] = set()          # chunk_id(hex16) → int, 메모리 절약
    per_source: dict[str, dict] = {}
    manifest_path = out_dir / MANIFEST_NAME
    started = time.time()

    base_manifest = {
        "format": EXPORT_FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "db_path": str(db_path),
        "sources_requested": sources,
        "limit": limit,
        "dedup": dedup,
        "chunking": {
            "chunk_size": rag_cfg.CHUNK_SIZE,
            "chunk_overlap": rag_cfg.CHUNK_OVERLAP,
            "short_doc_threshold": rag_cfg.SHORT_DOC_THRESHOLD,
            "chunker_sha256_16": _sha256_text(RAG_DIR / "chunker.py"),
            "indexer_sha256_16": _sha256_text(RAG_DIR / "indexer.py"),
            # 정본은 rag_assembly/chunker.py::_chunk_id — 아래는 설명용 사본.
            # 소스마다 seed 형태가 다르다(bill_meta·member는 인덱스가 없음).
            "chunk_id_scheme": {
                "hash": "sha256(seed)[:16]",
                "bill": "bill:{bill_id}:{sub_source}:{i}:{text[:80]}",
                "bill_meta": "bill_meta:{bill_id}:{text[:80]}",
                "document": "doc:{doc_id}:{doc_source}:{i}:{text[:80]}",
                "speech": "sp:{conf_id}:{speech_idx}:{i}:{text[:80]}",
                "member": "member:{mona_cd}:{text[:80]}",
            },
        },
        # 정본 indexer와의 유일한 의도적 차이 (V1). 정본은 공백만 있는 청크도
        # 임베딩하지만 여기서는 드롭한다 — 소스별 empty_dropped 카운트 참조.
        "divergence_from_indexer": [
            "text.strip()=='' 인 청크는 export에서 드롭 (정본 indexer는 임베딩함). "
            "빈도 극소, 검색 품질에 영향 없음."
        ],
        "resume": False,   # 중단 시 이어하기 없음 — --overwrite 로 처음부터 (F7)
        "parquet": {
            "columns": list(PARQUET_SCHEMA.names),
            "compression": compression,
            "compression_level": compression_level,
            "rows_per_file": rows_per_file,
            "row_group_size": row_group_size,
        },
    }

    try:
        for src in sources:
            fn = indexer.SOURCE_FUNCS[src]
            scon = StreamingCon(con, batch=fetch_batch)
            stats = LenStats()
            n_dup = 0
            n_empty = 0
            t0 = time.time()
            bar = tqdm(total=None, desc=src, unit="chunk") if (tqdm and progress) else None

            for chunk in fn(scon, limit=limit):
                text = chunk.text
                if not text or not text.strip():
                    n_empty += 1
                    continue
                if dedup:
                    key = int(chunk.chunk_id, 16)
                    if key in seen:
                        n_dup += 1
                        continue
                    seen.add(key)
                meta = chunk.metadata or {}
                writer.add(
                    chunk_id=chunk.chunk_id,
                    text=text,
                    source=str(meta.get("source") or src),
                    metadata_json=_dump_meta(meta),
                )
                stats.add(len(text))
                if bar is not None:
                    bar.update(1)
            if bar is not None:
                bar.close()

            per_source[src] = {
                "rows_scanned": scon.counter["rows"],
                "dup_dropped": n_dup,
                "empty_dropped": n_empty,
                "elapsed_sec": round(time.time() - t0, 1),
                **stats.as_dict(),
            }
            log.info("[%s] rows=%d chunks=%d dup=%d empty=%d (%.1fs)",
                     src, scon.counter["rows"], stats.n, n_dup, n_empty,
                     per_source[src]["elapsed_sec"])

            # 소스 하나 끝날 때마다 중간 manifest 저장 (중단 대비)
            _write_manifest(manifest_path, {
                **base_manifest,
                "status": "in_progress",
                "per_source": per_source,
                "files": writer.files,
                "total_rows": writer.total_rows,
            })

        writer.close()
    except BaseException:
        writer.abort()
        raise
    finally:
        con.close()

    payload = {
        **base_manifest,
        "status": "complete",
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_sec": round(time.time() - started, 1),
        "per_source": per_source,
        "files": writer.files,
        "total_rows": writer.total_rows,
    }
    _write_manifest(manifest_path, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--output-dir", default=str(_THIS.parent / "chunks_out"),
                    help="parquet 출력 디렉터리 (기본: working/embed_bundle/chunks_out)")
    ap.add_argument("--sources", "--source", dest="sources", nargs="+",
                    choices=SOURCES + ["all"], default=["all"],
                    help=f"내보낼 소스 (기본 all = {SOURCES})")
    ap.add_argument("--limit", type=int, default=None,
                    help="소스별 최대 N '행' (청크 아님, indexer와 동일 의미). 스모크용")
    ap.add_argument("--db", default=str(rag_cfg.RAW_DB_PATH),
                    help="원본 DuckDB (기본: rag config RAW_DB_PATH). 항상 read_only")
    ap.add_argument("--rows-per-file", type=int, default=200_000,
                    help="parquet 파일당 행 수 (기본 200000 = 임베딩 샤드 50000의 4배수)")
    ap.add_argument("--row-group-size", type=int, default=20_000,
                    help="parquet row group 크기 (메모리 상한)")
    ap.add_argument("--fetch-batch", type=int, default=256,
                    help="DuckDB fetchmany 배치 (회의록처럼 큰 행이면 낮출 것)")
    ap.add_argument("--compression-level", type=int, default=3,
                    help="zstd 레벨 (기본 3)")
    ap.add_argument("--no-hash", action="store_true",
                    help="parquet sha256 계산 생략 (전송 검증 포기)")
    ap.add_argument("--no-dedup", action="store_true",
                    help="chunk_id 중복 제거 생략 (기본은 제거)")
    ap.add_argument("--no-progress", action="store_true", help="tqdm 끄기")
    ap.add_argument("--overwrite", action="store_true",
                    help="기존 chunks_*.parquet / manifest를 **전부 삭제**하고 처음부터 "
                         "재생성 (이어하기 없음)")
    args = ap.parse_args(argv)

    sources = SOURCES if "all" in args.sources else list(dict.fromkeys(args.sources))
    out_dir = Path(args.output_dir).resolve()
    db_path = Path(args.db).resolve()

    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob(f"{FILE_PREFIX}_*.parquet")) + \
        sorted(out_dir.glob(f"{FILE_PREFIX}_*.parquet.tmp"))
    if existing:
        if not args.overwrite:
            log.error("output dir already has %d chunk file(s): %s ... "
                      "(--overwrite to replace)", len(existing), existing[0].name)
            return 2
        for p in existing:
            p.unlink()
        mp = out_dir / MANIFEST_NAME
        if mp.exists():
            mp.unlink()
        log.warning("removed %d pre-existing file(s) (--overwrite)", len(existing))

    log.info("export | db=%s | sources=%s | limit=%s | out=%s",
             db_path, sources, args.limit, out_dir)
    log.info("chunking | size=%d overlap=%d short_threshold=%d",
             rag_cfg.CHUNK_SIZE, rag_cfg.CHUNK_OVERLAP, rag_cfg.SHORT_DOC_THRESHOLD)

    payload = export(
        out_dir=out_dir, sources=sources, db_path=db_path, limit=args.limit,
        rows_per_file=args.rows_per_file, row_group_size=args.row_group_size,
        fetch_batch=args.fetch_batch, compression="zstd",
        compression_level=args.compression_level,
        hash_files=not args.no_hash, dedup=not args.no_dedup,
        progress=not args.no_progress,
    )

    log.info("=== summary ===")
    for src, st in payload["per_source"].items():
        log.info("  %-10s rows=%-8d chunks=%-9d mean=%-6.0f max=%d",
                 src, st["rows_scanned"], st["chunks"],
                 st["chars_mean"], st["chars_max"])
    total_bytes = sum(f["bytes"] for f in payload["files"])
    log.info("  files=%d  rows=%d  size=%.1f MB  elapsed=%.1f min",
             len(payload["files"]), payload["total_rows"],
             total_bytes / 1e6, payload["elapsed_sec"] / 60)
    log.info("  manifest: %s", out_dir / MANIFEST_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
