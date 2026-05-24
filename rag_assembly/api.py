"""rag_assembly Public API.

분석 스크립트·MCP 서버·Jupyter 노트북에서 import해서 사용:

    from rag_assembly.api import search, search_bills, search_speeches

    results = search("AI 안전성 관련 법안 발의자", top_k=5)
    for r in results:
        print(r["text"][:200], r["metadata"])

전역 싱글턴 (`_engine`)으로 ChromaDB·BM25·embedder·reranker를 한 번만 로드.
"""
import _bootstrap  # noqa: F401

from typing import Optional

import config as cfg
from search import AssemblySearch


_engine: AssemblySearch | None = None


def _get_engine() -> AssemblySearch:
    global _engine
    if _engine is None:
        _engine = AssemblySearch()
    return _engine


# ── 일반 검색 ────────────────────────────────────────

def search(query: str, top_k: int = 10,
           source: str | None = None,
           where: dict | None = None,
           rerank: bool = True) -> list[dict]:
    """하이브리드 검색 (벡터 + BM25 + RRF + rerank).

    Args:
        query: 자연어 쿼리
        top_k: 반환 개수 (기본 10)
        source: 필터링 (bill / document / speech / member / bill_meta)
        where: 추가 ChromaDB 필터 (예: {"age": 22})
        rerank: cross-encoder reranking 적용 여부 (기본 True)

    Returns:
        [{chunk_id, text, metadata, vector_score, bm25_score, rrf_score, rerank_score}, ...]
    """
    eng = _get_engine()
    final_where = dict(where or {})
    if source:
        final_where["source"] = source
    return eng.hybrid(query, top_k=top_k,
                       where=final_where or None,
                       rerank=rerank)


# ── 소스별 헬퍼 ──────────────────────────────────────

def search_bills(query: str, top_k: int = 10,
                 age: int | None = None,
                 committee: str | None = None) -> list[dict]:
    """법안 본문(reason+full) 검색."""
    where = {}
    if age is not None:
        where["age"] = int(age)
    return search(query, top_k=top_k, source="bill",
                  where=where or None)


def search_bill_metas(query: str, top_k: int = 10,
                      age: int | None = None) -> list[dict]:
    """법안 메타데이터 카드 검색 (제목·발의자·위원회)."""
    where = {}
    if age is not None:
        where["age"] = int(age)
    return search(query, top_k=top_k, source="bill_meta",
                  where=where or None)


def search_speeches(query: str, top_k: int = 10,
                    dae_num: str | None = None,
                    speaker: str | None = None) -> list[dict]:
    """회의록 발언 검색."""
    where = {}
    if dae_num:
        where["dae_num"] = dae_num
    if speaker:
        where["speaker"] = speaker
    return search(query, top_k=top_k, source="speech",
                  where=where or None)


def search_documents(query: str, top_k: int = 10,
                     doc_source: str | None = None) -> list[dict]:
    """회의록·연구단체보고서·정책여론조사 본문 검색.

    doc_source: minutes_committee / minutes_plenary / minutes_subcommittee /
                minutes_committee_of_whole / report / research
    """
    where = {}
    if doc_source:
        where["doc_source"] = doc_source
    return search(query, top_k=top_k, source="document",
                  where=where or None)


def search_members(query: str, top_k: int = 10) -> list[dict]:
    """의원 프로필 검색 (이름·정당·지역구·위원회)."""
    return search(query, top_k=top_k, source="member")


# ── 정확 일치 lookup (fallback) ─────────────────────

def lookup_member_by_name(name: str) -> list[dict]:
    """의원명으로 정확 일치 검색 (fallback to fuzzy if 0 hits)."""
    eng = _get_engine()
    results = eng.vector_search(name, top_k=5,
                                 where={"source": "member", "name": name})
    if not results:
        # fuzzy
        results = search_members(name, top_k=5)
    return results


def lookup_bill_by_id(bill_id: str) -> list[dict]:
    """법안 ID로 정확 일치 검색."""
    vdb = _get_engine().vdb
    res = vdb.collection.get(
        where={"$and": [{"source": "bill_meta"}, {"bill_id": bill_id}]},
        include=["documents", "metadatas"],
    )
    out = []
    for cid, doc, meta in zip(res["ids"], res["documents"], res["metadatas"]):
        out.append({"chunk_id": cid, "text": doc, "metadata": meta})
    return out


# ── 통계·관리 ───────────────────────────────────────

def stats() -> dict:
    """ChromaDB 청크 수·소스별 분포."""
    from manifest import Manifest
    eng = _get_engine()
    m = Manifest()
    try:
        counts = m.chunk_counts_by_source()
    finally:
        m.close()
    return {
        "vectordb_count": eng.vdb.count(),
        "chunks_by_source": counts,
        "embed_config_version": cfg.EMBED_CONFIG_VERSION,
    }
