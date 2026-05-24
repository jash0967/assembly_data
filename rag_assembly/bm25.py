"""BM25 인덱스 (kiwipiepy + bm25s, 소스별 분할).

대용량(3.2M 청크)을 단일 인덱스로 빌드하면 메모리 25~30GB 피크에 도달.
이를 회피하기 위해 소스별·doc_source별로 분할 빌드한다.

분할 구성 (13개 sub-index):
  - 기본 source: bill, bill_meta, speech, member
  - document → doc_source별 5개 (plenary/report/research/sub/committee_of_whole)
  - minutes_committee(1.9M) → chunk_id hex prefix로 4분할
각 sub-index 피크 메모리 ~3~5GB.

검색 시 모든 sub-index 점수 합쳐서 top-N 반환.
"""
import _bootstrap  # noqa: F401

import gc
import json
import logging
import pickle
import re
import time
from pathlib import Path

import bm25s
import pyarrow as pa
import pyarrow.compute as pc
from kiwipiepy import Kiwi

import config as cfg
from vectordb import VectorDB


log = logging.getLogger(__name__)


_POS_KEEP = {"NNG", "NNP", "VV", "VA", "SL", "SN", "MM"}
_NON_KOREAN = re.compile(r"[A-Za-z0-9_/.\-]+")


def _tokenize_text(kiwi: Kiwi, text: str) -> list[str]:
    if not text:
        return []
    tokens = []
    for tok in kiwi.tokenize(text):
        if tok.tag in _POS_KEEP and len(tok.form) >= 1:
            tokens.append(tok.form.lower())
    tokens.extend(m.group(0).lower() for m in _NON_KOREAN.finditer(text))
    return tokens


_kiwi_worker: Kiwi | None = None


def _init_worker():
    global _kiwi_worker
    _kiwi_worker = Kiwi()


def _tokenize_one(text: str) -> list[str]:
    return _tokenize_text(_kiwi_worker, text)


class KoreanTokenizer:
    """단일 프로세스 토크나이저 (search 시 query용)."""
    def __init__(self):
        self.kiwi = Kiwi()

    def tokenize(self, text: str) -> list[str]:
        return _tokenize_text(self.kiwi, text)


_META_COLS = (
    "source", "sub_source", "bill_id", "doc_id", "conf_id", "mona_cd",
    "speaker", "age", "propose_date", "conf_date", "committee", "dae_num",
    "doc_source", "bill_name", "party", "name", "district", "chunk_idx",
)

# vector·text 외 모든 메타 + chunk_id + text. vector 제외로 메모리 절약.
_LOAD_COLS = ("chunk_id", "text") + _META_COLS


# sub-corpus 정의: (sub_id, filter_func(arr) -> arr)
def _filter_eq(col: str, val):
    return lambda arr: arr.filter(pc.equal(arr[col], val))


def _filter_and(*filters):
    def fn(arr):
        for f in filters:
            arr = f(arr)
        return arr
    return fn


def _filter_committee_shard(shard_idx: int, n_shards: int = 4):
    """chunk_id hex의 첫 글자 기준 modulo로 minutes_committee를 N분할."""
    # hex 0~9, a~f → 16개 균일 분포. n_shards=4면 4개씩 묶음
    groups = [list("0123456789abcdef")[i::n_shards] for i in range(n_shards)]
    target = groups[shard_idx]
    def fn(arr):
        first = pc.utf8_slice_codeunits(arr["chunk_id"], 0, 1)
        mask = pc.is_in(first, value_set=pa.array(target))
        return arr.filter(mask)
    return fn


def _get_sub_corpus_specs():
    """빌드할 sub-corpus 명세. (sub_id, filter_fn) 튜플 리스트."""
    return [
        # 기본 source
        ("bill",       _filter_eq("source", "bill")),
        ("bill_meta",  _filter_eq("source", "bill_meta")),
        ("speech",     _filter_eq("source", "speech")),
        ("member",     _filter_eq("source", "member")),
        # document → doc_source별
        ("doc_minutes_plenary",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "minutes_plenary"))),
        ("doc_report",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "report"))),
        ("doc_research",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "research"))),
        ("doc_minutes_subcommittee",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "minutes_subcommittee"))),
        ("doc_minutes_committee_of_whole",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "minutes_committee_of_whole"))),
        # minutes_committee(1.9M) → 4 shards
        ("doc_minutes_committee_0",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "minutes_committee"),
                        _filter_committee_shard(0))),
        ("doc_minutes_committee_1",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "minutes_committee"),
                        _filter_committee_shard(1))),
        ("doc_minutes_committee_2",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "minutes_committee"),
                        _filter_committee_shard(2))),
        ("doc_minutes_committee_3",
            _filter_and(_filter_eq("source", "document"),
                        _filter_eq("doc_source", "minutes_committee"),
                        _filter_committee_shard(3))),
    ]


def _build_sub_index(sub_id: str, sub_arr, root_dir: Path,
                     n_workers: int, force: bool = False) -> int:
    """단일 sub-corpus BM25 빌드. 끝나면 디스크 저장.

    이미 빌드되어 있으면 (chunk_meta.pkl 존재) skip — resume 지원.

    Returns: 빌드된 (또는 기존) 청크 수.
    """
    import multiprocessing as mp

    n = len(sub_arr)
    if n == 0:
        log.warning("[%s] empty, skipping", sub_id)
        return 0

    sub_dir = root_dir / sub_id
    # resume: 이미 완성된 sub-index는 skip
    meta_pkl = sub_dir / "chunk_meta.pkl"
    if not force and meta_pkl.exists():
        with open(meta_pkl, "rb") as f:
            existing = pickle.load(f)
        n_existing = len(existing["chunk_ids"])
        log.info("[%s] already built (%d chunks) — skip", sub_id, n_existing)
        return n_existing
    sub_dir.mkdir(parents=True, exist_ok=True)

    chunk_ids = sub_arr.column("chunk_id").to_pylist()
    docs = sub_arr.column("text").to_pylist()
    meta_cols = {c: sub_arr.column(c).to_pylist() for c in _META_COLS
                 if c in sub_arr.column_names}
    del sub_arr
    gc.collect()

    log.info("[%s] tokenizing %d chunks (workers=%d)...", sub_id, n, n_workers)
    t0 = time.time()
    with mp.Pool(processes=n_workers, initializer=_init_worker) as pool:
        tokenized = pool.map(_tokenize_one, docs, chunksize=500)
    elapsed = time.time() - t0
    log.info("[%s]   tokenized %.1fs (%.0f docs/sec)",
             sub_id, elapsed, n / max(elapsed, 0.1))
    del docs
    gc.collect()

    log.info("[%s] indexing with bm25s...", sub_id)
    t0 = time.time()
    bm = bm25s.BM25()
    bm.index(tokenized, show_progress=False)
    log.info("[%s]   indexed in %.1fs", sub_id, time.time() - t0)
    del tokenized
    gc.collect()

    log.info("[%s] saving to %s", sub_id, sub_dir)
    bm.save(str(sub_dir))
    # 메타데이터는 행 단위 dict로 저장 (검색 결과용)
    metadatas = [
        {c: meta_cols[c][i] for c in meta_cols if meta_cols[c][i] is not None}
        for i in range(n)
    ]
    with open(sub_dir / "chunk_meta.pkl", "wb") as f:
        pickle.dump({"chunk_ids": chunk_ids, "metadatas": metadatas},
                    f, protocol=pickle.HIGHEST_PROTOCOL)
    log.info("[%s] done (%d chunks)\n", sub_id, n)

    # 강제 해제
    del bm, chunk_ids, metadatas, meta_cols
    gc.collect()
    return n


class _SubIndex:
    """런타임 검색용 sub-index wrapper."""
    def __init__(self, sub_id: str, sub_dir: Path):
        self.sub_id = sub_id
        self.dir = sub_dir
        self.bm25 = bm25s.BM25.load(str(sub_dir))
        with open(sub_dir / "chunk_meta.pkl", "rb") as f:
            data = pickle.load(f)
        self.chunk_ids: list[str] = data["chunk_ids"]
        self.metadatas: list[dict] = data["metadatas"]


class BM25Index:
    """통합 BM25 검색기: 13개 sub-index 합쳐서 검색."""
    def __init__(self, path: Path | str = None):
        p = Path(path or cfg.BM25_PKL)
        if p.suffix == ".pkl":
            p = p.with_suffix("")
        self.root = p
        self.sub_indices: list[_SubIndex] = []
        self.tokenizer = KoreanTokenizer()
        self._loaded = False

    def build(self, vdb: VectorDB, n_workers: int = 6,
              force: bool = False) -> None:
        """모든 sub-corpus 빌드. 메모리 안전 (한 번에 하나씩 + GC).

        Resume 지원: 이미 chunk_meta.pkl 있는 sub-index는 skip.
        force=True면 모두 재빌드.
        """
        self.root.mkdir(parents=True, exist_ok=True)

        specs = _get_sub_corpus_specs()

        # 모두 이미 빌드돼있으면 LanceDB 로드 자체를 건너뜀
        pending = [(sid, fn) for sid, fn in specs
                   if force or not (self.root / sid / "chunk_meta.pkl").exists()]
        if not pending:
            log.info("all sub-indices already built — nothing to do")
            self._write_manifest_summary(specs)
            return

        log.info("loading non-vector columns from LanceDB...")
        t0 = time.time()
        # vector 컬럼 제외 → 메모리 절약 (vector는 1536*2 bytes/row × 3.24M = ~10GB 제거)
        full_arr = vdb.table.to_arrow().select(list(_LOAD_COLS))
        log.info("  loaded %d rows in %.1fs", len(full_arr), time.time() - t0)

        for sub_id, filter_fn in specs:
            # resume check (filter도 skip 가능)
            if not force and (self.root / sub_id / "chunk_meta.pkl").exists():
                log.info("[%s] already built — skip", sub_id)
                continue
            log.info("[%s] filtering...", sub_id)
            t0 = time.time()
            sub_arr = filter_fn(full_arr)
            n_filtered = len(sub_arr)
            log.info("[%s]   %d chunks (filter %.1fs)",
                     sub_id, n_filtered, time.time() - t0)
            _build_sub_index(sub_id, sub_arr, self.root, n_workers, force=force)
            # 매 sub-index 완료 시 manifest 갱신 (중단 대비)
            self._write_manifest_summary(specs)

        self._write_manifest_summary(specs)
        log.info("=== ALL SUB-INDICES BUILT ===")

    def _write_manifest_summary(self, specs) -> None:
        """디스크에 존재하는 sub-index들로 manifest.json 갱신."""
        manifest = []
        total = 0
        for sub_id, _ in specs:
            meta_pkl = self.root / sub_id / "chunk_meta.pkl"
            if meta_pkl.exists():
                with open(meta_pkl, "rb") as f:
                    d = pickle.load(f)
                n = len(d["chunk_ids"])
                manifest.append({"sub_id": sub_id, "n_chunks": n})
                total += n
        with open(self.root / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        log.info("manifest: %d sub-indices, total %d chunks",
                 len(manifest), total)

    def load(self) -> None:
        if self._loaded:
            return
        manifest_path = self.root / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"BM25 manifest missing: {manifest_path}")
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        self.sub_indices = []
        for m in manifest:
            sub_dir = self.root / m["sub_id"]
            if not sub_dir.exists():
                log.warning("sub-index missing: %s", sub_dir)
                continue
            self.sub_indices.append(_SubIndex(m["sub_id"], sub_dir))
        self._loaded = True
        log.info("loaded %d sub-indices from %s",
                 len(self.sub_indices), self.root)

    def search(self, query: str, top_k: int = 30) -> list[dict]:
        if not self._loaded:
            self.load()
        tokens = self.tokenizer.tokenize(query)
        if not tokens:
            return []
        all_results = []
        for si in self.sub_indices:
            try:
                results, scores = si.bm25.retrieve([tokens], k=top_k,
                                                    show_progress=False)
            except Exception as e:
                log.warning("retrieve failed [%s]: %s", si.sub_id, e)
                continue
            for idx, score in zip(results[0], scores[0]):
                if score <= 0:
                    continue
                i = int(idx)
                all_results.append({
                    "chunk_id": si.chunk_ids[i],
                    "score": float(score),
                    "metadata": si.metadatas[i] if i < len(si.metadatas) else None,
                })
        all_results.sort(key=lambda r: -r["score"])
        return all_results[:top_k]


def build_cli():
    """CLI: 임베딩·HNSW 완료 후 호출."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(cfg.BM25_PKL))
    parser.add_argument("--workers", type=int, default=6,
                        help="kiwipiepy 병렬 토크나이징 워커 수")
    parser.add_argument("--force", action="store_true",
                        help="이미 빌드된 sub-index도 다시 빌드")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    vdb = VectorDB()
    bm = BM25Index(args.out)
    bm.build(vdb, n_workers=args.workers, force=args.force)


if __name__ == "__main__":
    build_cli()
