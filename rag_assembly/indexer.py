"""전체 임베딩·인덱싱 파이프라인.

소스 5개 (bill, bill_meta, document, speech, member)를 raw·analysis DB에서 읽고
chunker로 청킹 후 Vertex AI 임베딩 → ChromaDB 적재.

Idempotent: manifest의 existing_chunk_ids로 이미 임베딩된 청크는 skip.
부분 실행 가능: --source bill / --source document 등.

Usage:
    venv/Scripts/python.exe rag_assembly/indexer.py             # 전체 (기본)
    venv/Scripts/python.exe rag_assembly/indexer.py --source bill speech
    venv/Scripts/python.exe rag_assembly/indexer.py --dry-run   # 청크 수만 출력
    venv/Scripts/python.exe rag_assembly/indexer.py --limit 100 # 디버깅용
"""
import _bootstrap  # noqa: F401

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Iterator

import duckdb
from tqdm import tqdm

import config as cfg
import chunker
from embedder import Embedder
from manifest import Manifest
from vectordb import VectorDB


if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


SOURCES = ["bill", "bill_meta", "document", "speech", "member"]


def open_raw_con():
    return duckdb.connect(str(cfg.RAW_DB_PATH), read_only=True)


# ── 소스별 row generator ──────────────────────────────

def iter_bills(con, limit: int = None) -> Iterator[chunker.Chunk]:
    """bill_text + v_bill 메타 join → bill 청크."""
    sql = """
        SELECT t.bill_id, t.age, t.reason_and_content, t.full_text,
               b.bill_name, b.proposer, b.committee, b.propose_date
        FROM bill_text t
        LEFT JOIN v_bill b USING (bill_id)
        WHERE t.full_text IS NOT NULL OR t.reason_and_content IS NOT NULL
    """
    if limit:
        sql += f" LIMIT {limit}"
    for row in con.execute(sql).fetchall():
        bill_id, age, reason, full, name, proposer, committee, propose_date = row
        yield from chunker.chunks_from_bill(
            bill_id=bill_id, age=age, bill_name=name,
            reason_and_content=reason, full_text=full,
            proposer=proposer, committee=committee,
            propose_date=propose_date,
        )


def iter_bill_metas(con, limit: int = None) -> Iterator[chunker.Chunk]:
    """v_bill 전체 (97K) → bill_meta 청크 (짧은 카드)."""
    sql = """
        SELECT bill_id, age, bill_name, proposer, lead_proposer,
               committee, propose_date, proc_result
        FROM v_bill
        WHERE bill_name IS NOT NULL
    """
    if limit:
        sql += f" LIMIT {limit}"
    for row in con.execute(sql).fetchall():
        bid, age, name, proposer, lead, committee, propose_date, result = row
        yield from chunker.chunks_from_bill_meta(
            bill_id=bid, age=age, bill_name=name,
            proposer=proposer, lead_proposer=lead,
            committee=committee, propose_date=propose_date,
            proc_result=result,
        )


def iter_documents(con, limit: int = None) -> Iterator[chunker.Chunk]:
    """document_text 26K행 청킹 (회의록·보고서)."""
    sql = """
        SELECT doc_id, source, full_text
        FROM document_text
        WHERE full_text IS NOT NULL AND LENGTH(full_text) > 50
    """
    if limit:
        sql += f" LIMIT {limit}"
    for row in con.execute(sql).fetchall():
        doc_id, source, full_text = row
        yield from chunker.chunks_from_document(
            doc_id=doc_id, doc_source=source, full_text=full_text,
        )


def iter_speeches(con, limit: int = None) -> Iterator[chunker.Chunk]:
    """speeches 84K행."""
    sql = """
        SELECT conf_id, source, dae_num, conf_date, speaker, speech_idx, text
        FROM speeches
        WHERE text IS NOT NULL AND LENGTH(text) > 5
    """
    if limit:
        sql += f" LIMIT {limit}"
    for row in con.execute(sql).fetchall():
        conf_id, src, dae_num, conf_date, speaker, sp_idx, text = row
        yield from chunker.chunks_from_speech(
            conf_id=conf_id, speech_idx=sp_idx, speaker=speaker,
            conf_date=conf_date, source=src, dae_num=dae_num, text=text,
        )


def iter_members(con, limit: int = None) -> Iterator[chunker.Chunk]:
    """v_member 295명 (현 22대)."""
    sql = """
        SELECT mona_cd, name, party, district, committee,
               reelect, terms_list, gender
        FROM v_member
    """
    if limit:
        sql += f" LIMIT {limit}"
    for row in con.execute(sql).fetchall():
        mona_cd, name, party, district, committee, reelect, terms, gender = row
        yield from chunker.chunks_from_member(
            mona_cd=mona_cd, name=name, party=party,
            district=district, committee=committee,
            reelect=reelect, terms_list=terms, gender=gender,
        )


SOURCE_FUNCS = {
    "bill": iter_bills,
    "bill_meta": iter_bill_metas,
    "document": iter_documents,
    "speech": iter_speeches,
    "member": iter_members,
}


# ── 인덱싱 메인 ───────────────────────────────────────

def index_source(source: str, manifest: Manifest, vdb: VectorDB,
                 embedder: Embedder, raw_con, limit: int = None,
                 dry_run: bool = False, batch_size: int = None) -> dict:
    """단일 소스를 청킹·임베딩·적재."""
    # indexer flush 단위는 EMBED_BATCH_SIZE * EMBED_CONCURRENCY 만큼 누적
    # → 한 번 flush 시 N개 배치가 병렬 호출됨
    flush_size = batch_size or (cfg.EMBED_BATCH_SIZE * cfg.EMBED_CONCURRENCY)
    fn = SOURCE_FUNCS[source]
    log.info("[%s] iterating rows...", source)

    # manifest에서 이미 임베딩된 chunk_id 로드 (전체 한 번)
    existing = manifest.existing_chunk_ids()
    log.info("[%s] %d existing chunks in manifest (skip if matched)",
             source, len(existing))

    buf_chunks: list[chunker.Chunk] = []
    n_seen = 0
    n_new = 0
    n_skipped = 0
    pbar = tqdm(total=None, desc=f"{source}", unit="chunks")

    def flush():
        nonlocal n_new
        if not buf_chunks:
            return
        texts = [c.text for c in buf_chunks]
        if not dry_run:
            vectors = embedder.embed_documents(texts)
            vdb.upsert(
                chunk_ids=[c.chunk_id for c in buf_chunks],
                texts=texts,
                embeddings=vectors,
                metadatas=[c.metadata for c in buf_chunks],
            )
            manifest.register_chunks([
                dict(chunk_id=c.chunk_id, source=c.metadata.get("source"),
                     ref_id=str(c.metadata.get("bill_id")
                                or c.metadata.get("doc_id")
                                or c.metadata.get("conf_id")
                                or c.metadata.get("mona_cd") or ""),
                     sub_source=c.metadata.get("sub_source")
                                or c.metadata.get("doc_source"),
                     text_len=len(c.text))
                for c in buf_chunks
            ])
        n_new += len(buf_chunks)
        pbar.update(len(buf_chunks))
        buf_chunks.clear()

    for chunk in fn(raw_con, limit=limit):
        n_seen += 1
        if chunk.chunk_id in existing:
            n_skipped += 1
            continue
        existing.add(chunk.chunk_id)  # 같은 run 내 중복 방지
        buf_chunks.append(chunk)
        if len(buf_chunks) >= flush_size:
            flush()
    flush()
    pbar.close()

    log.info("[%s] done: seen=%d, new=%d, skipped=%d",
             source, n_seen, n_new, n_skipped)
    return {"source": source, "seen": n_seen, "new": n_new, "skipped": n_skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", nargs="+", choices=SOURCES + ["all"],
                        default=["all"],
                        help="소스 선택 (기본: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="각 소스에서 최대 N행만 (디버깅용)")
    parser.add_argument("--dry-run", action="store_true",
                        help="청크 수만 출력, 임베딩·DB 적재 생략")
    parser.add_argument("--batch", type=int, default=None,
                        help="임베딩 배치 크기 (기본 config.EMBED_BATCH_SIZE)")
    args = parser.parse_args()

    sources = SOURCES if "all" in args.source else args.source
    log.info("starting indexer | sources=%s | dry_run=%s | limit=%s",
             sources, args.dry_run, args.limit)

    manifest = Manifest()
    vdb = VectorDB()
    embedder = Embedder() if not args.dry_run else None
    raw_con = open_raw_con()

    run_id = manifest.open_run(sources)
    started = time.time()
    totals = []
    try:
        for src in sources:
            result = index_source(
                src, manifest, vdb, embedder, raw_con,
                limit=args.limit, dry_run=args.dry_run,
                batch_size=args.batch,
            )
            totals.append(result)
        elapsed = time.time() - started
        added = sum(r["new"] for r in totals)
        skipped = sum(r["skipped"] for r in totals)
        manifest.close_run(run_id, added, skipped,
                            notes=f"elapsed={elapsed:.1f}s")
        log.info("=== summary ===")
        for r in totals:
            log.info("  %-12s seen=%d new=%d skipped=%d",
                     r["source"], r["seen"], r["new"], r["skipped"])
        log.info("  elapsed: %.1f min", elapsed / 60)
        if not args.dry_run:
            log.info("  vectordb count: %d", vdb.count())
    finally:
        raw_con.close()
        manifest.close()


if __name__ == "__main__":
    main()
