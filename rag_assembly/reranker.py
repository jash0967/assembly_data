"""Cross-encoder reranker (BAAI/bge-reranker-v2-m3).

쿼리·문서 쌍의 관련도를 직접 스코어링. 한국어·다국어 지원.
sentence-transformers의 CrossEncoder 사용.
첫 호출 시 모델 다운로드 (~1.1 GB).
"""
import _bootstrap  # noqa: F401

import logging
from typing import Iterable

import config as cfg


log = logging.getLogger(__name__)


class Reranker:
    def __init__(self, model_name: str = None, batch_size: int = None):
        self.model_name = model_name or cfg.RERANKER_MODEL
        self.batch_size = batch_size or cfg.RERANK_BATCH
        self._model = None

    def _ensure_loaded(self):
        if self._model is not None:
            return
        log.info("loading reranker: %s", self.model_name)
        from sentence_transformers import CrossEncoder
        # GPU 있으면 자동 사용
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
        self._model = CrossEncoder(self.model_name, device=device,
                                    trust_remote_code=True)
        log.info("reranker loaded on %s", device)

    def rerank(self, query: str, candidates: list[dict],
               top_k: int = None, text_field: str = "text") -> list[dict]:
        """candidates: [{chunk_id, text/document, ...}] → 점수순 top_k 반환.

        각 dict에 'rerank_score' 추가.
        """
        if not candidates:
            return []
        self._ensure_loaded()
        top_k = top_k or cfg.FINAL_TOP_K
        pairs = [(query, c.get(text_field) or c.get("document", ""))
                 for c in candidates]
        scores = self._model.predict(pairs, batch_size=self.batch_size,
                                      show_progress_bar=False)
        out = []
        for c, s in zip(candidates, scores):
            c2 = dict(c)
            c2["rerank_score"] = float(s)
            out.append(c2)
        out.sort(key=lambda x: x["rerank_score"], reverse=True)
        return out[:top_k]
