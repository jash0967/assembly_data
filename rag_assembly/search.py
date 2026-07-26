"""하이브리드 검색 (벡터 + BM25 + RRF 결합 + cross-encoder rerank).

Phase 5 Python API의 핵심.

검색 플로우:
  1. query → embedder (로컬 arctic-ko, "query: " prefix는 embedder 내부에서 주입)
  2. ChromaDB top 30 (벡터)
  3. BM25 top 30 (키워드)
  4. RRF로 결합 → 후보 60개 (중복 제거)
  5. reranker로 top 10 재정렬

메타필터: source/age/dae_num/committee/attribute 등 ChromaDB where 절에 전달.
"""
import _bootstrap  # noqa: F401

import logging
from typing import Optional

import config as cfg
from bm25 import BM25Index
from embedder import Embedder
from reranker import Reranker
from vectordb import VectorDB


log = logging.getLogger(__name__)


class AssemblySearch:
    """하이브리드 검색 진입점."""
    def __init__(self):
        self.vdb = VectorDB()
        self.bm25 = BM25Index()
        self._embedder: Embedder | None = None
        self._reranker: Reranker | None = None
        self._bm25_loaded = False

    @property
    def embedder(self) -> Embedder:
        """lazy — BM25 전용·메타필터 전용 경로에서 임베딩 모델을 로드하지 않는다.

        (구 Vertex Embedder는 생성만으로 ADC 인증 + region별 클라이언트 8개를
        만들었고, 로컬 모델은 생성만으로 568M 가중치를 GPU에 올린다.
        어느 쪽이든 검색과 무관한 경로에서 낼 비용이 아니다.)
        """
        if self._embedder is None:
            self._embedder = Embedder()
        return self._embedder

    def _ensure_bm25(self):
        if not self._bm25_loaded:
            self.bm25.load()
            self._bm25_loaded = True

    def _get_reranker(self) -> Reranker:
        if self._reranker is None:
            self._reranker = Reranker()
        return self._reranker

    def vector_search(self, query: str, top_k: int = None,
                      where: dict | None = None) -> list[dict]:
        top_k = top_k or cfg.VECTOR_TOP_K
        qv = self.embedder.embed_query(query)
        res = self.vdb.query(qv, top_k=top_k, where=where)
        # ChromaDB 응답 → 정형
        out = []
        ids = res["ids"][0] if res["ids"] else []
        docs = res["documents"][0] if res["documents"] else []
        metas = res["metadatas"][0] if res["metadatas"] else []
        dists = res["distances"][0] if res["distances"] else []
        for i, cid in enumerate(ids):
            out.append({
                "chunk_id": cid,
                "text": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                "vector_score": 1.0 - dists[i] if i < len(dists) else 0.0,
                "vector_rank": i,
            })
        return out

    def bm25_search(self, query: str, top_k: int = None) -> list[dict]:
        top_k = top_k or cfg.BM25_TOP_K
        self._ensure_bm25()
        return self.bm25.search(query, top_k=top_k)

    def hybrid(self, query: str, top_k: int = None,
               where: dict | None = None,
               rerank: bool = True) -> list[dict]:
        """벡터 + BM25 + RRF + (옵션) rerank."""
        top_k = top_k or cfg.FINAL_TOP_K

        v_results = self.vector_search(query, top_k=cfg.VECTOR_TOP_K,
                                        where=where)
        try:
            b_results = self.bm25_search(query, top_k=cfg.BM25_TOP_K)
        except FileNotFoundError:
            log.warning("BM25 index not built; using vector only")
            b_results = []

        # 메타필터를 BM25 결과에도 적용 (BM25는 native filter 없음)
        if where:
            b_results = [r for r in b_results
                         if _matches_where(r.get("metadata") or {}, where)]

        # RRF
        rrf_k = cfg.RRF_K
        rrf_scores: dict[str, float] = {}
        details: dict[str, dict] = {}
        for rank, r in enumerate(v_results):
            cid = r["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0) + 1 / (rrf_k + rank)
            details.setdefault(cid, {}).update(r)
            details[cid]["vector_rank"] = rank
        for rank, r in enumerate(b_results):
            cid = r["chunk_id"]
            rrf_scores[cid] = rrf_scores.get(cid, 0) + 1 / (rrf_k + rank)
            details.setdefault(cid, {})
            if "text" not in details[cid] and r.get("metadata"):
                details[cid]["metadata"] = r["metadata"]
            details[cid]["bm25_rank"] = rank
            details[cid]["bm25_score"] = r.get("score", 0)

        # 결합 후보 (rerank 입력용)
        candidates = []
        for cid, rrf in sorted(rrf_scores.items(), key=lambda x: -x[1]):
            d = details[cid]
            d["chunk_id"] = cid
            d["rrf_score"] = rrf
            # rerank를 위해 text가 있어야 함 (없으면 vdb에서 재조회)
            if "text" not in d:
                fetched = self.vdb.collection.get(ids=[cid],
                                                    include=["documents", "metadatas"])
                if fetched["documents"]:
                    d["text"] = fetched["documents"][0]
                    d["metadata"] = (fetched["metadatas"][0]
                                     if fetched["metadatas"] else {})
                else:
                    d["text"] = ""
            candidates.append(d)

        if rerank and candidates:
            return self._get_reranker().rerank(query, candidates, top_k=top_k)
        return candidates[:top_k]


def _matches_where(meta: dict, where: dict) -> bool:
    """ChromaDB-style 단순 where 매칭 (키별 동등 비교만)."""
    for k, v in where.items():
        if isinstance(v, dict):
            # {'$eq': X}, {'$in': [...]} 등 일부만 처리
            if "$eq" in v and meta.get(k) != v["$eq"]:
                return False
            if "$in" in v and meta.get(k) not in v["$in"]:
                return False
        else:
            if meta.get(k) != v:
                return False
    return True


# ── CLI 검증용 ──────────────────────────────────────

def _cli():
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--top_k", type=int, default=5)
    parser.add_argument("--source", default=None,
                        help="필터링: bill / document / speech / member / bill_meta")
    parser.add_argument("--no-rerank", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    s = AssemblySearch()
    where = {"source": args.source} if args.source else None
    results = s.hybrid(args.query, top_k=args.top_k, where=where,
                       rerank=not args.no_rerank)
    for i, r in enumerate(results):
        print(f"\n[{i+1}] rerank={r.get('rerank_score', 0):.3f} "
              f"rrf={r.get('rrf_score', 0):.4f} "
              f"vec_rank={r.get('vector_rank', '-')} "
              f"bm25_rank={r.get('bm25_rank', '-')}")
        print(f"  source: {r.get('metadata', {}).get('source')}")
        print(f"  ref: {r.get('chunk_id')}")
        print(f"  text: {(r.get('text') or '')[:300]}...")


if __name__ == "__main__":
    _cli()
