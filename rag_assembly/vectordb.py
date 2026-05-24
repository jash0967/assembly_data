"""LanceDB 벡터 저장소.

ChromaDB 대체. 동일 인터페이스(upsert/query/count/delete) 유지로
search.py·indexer.py·api.py 변경 최소화.

특징:
  - float16 저장 (메모리 절반, 정확도 손실 < 0.5%)
  - lazy load (mmap, 컬렉션 열기 시 메타만 로드)
  - 명시적 SQL where 필터 지원
  - HNSW 인덱스는 bulk insert 후 한 번 빌드 (incremental insert도 지원)
"""
import _bootstrap  # noqa: F401

import logging
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import pyarrow as pa

import config as cfg


log = logging.getLogger(__name__)


# Arrow 스키마: 메타필드를 명시적으로 두면 SQL where 필터·정렬이 빠름
def _arrow_schema(dim: int, dtype: str) -> pa.Schema:
    vec_type = pa.float16() if dtype == "float16" else pa.float32()
    return pa.schema([
        pa.field("chunk_id", pa.string(), nullable=False),
        pa.field("text", pa.string(), nullable=False),
        # 소스 정보
        pa.field("source", pa.string(), nullable=False),
        pa.field("sub_source", pa.string()),
        # 식별자 (소스별로 채워짐)
        pa.field("bill_id", pa.string()),
        pa.field("doc_id", pa.string()),
        pa.field("conf_id", pa.string()),
        pa.field("mona_cd", pa.string()),
        # 검색 필터용
        pa.field("speaker", pa.string()),
        pa.field("age", pa.int32()),
        pa.field("propose_date", pa.string()),
        pa.field("conf_date", pa.string()),
        pa.field("committee", pa.string()),
        pa.field("dae_num", pa.string()),
        pa.field("doc_source", pa.string()),
        pa.field("bill_name", pa.string()),
        pa.field("party", pa.string()),
        pa.field("name", pa.string()),
        pa.field("district", pa.string()),
        pa.field("chunk_idx", pa.int32()),
        # 벡터 (고정 길이 list)
        pa.field("vector", pa.list_(vec_type, dim), nullable=False),
    ])


_META_KEYS = (
    "source", "sub_source", "bill_id", "doc_id", "conf_id", "mona_cd",
    "speaker", "age", "propose_date", "conf_date", "committee", "dae_num",
    "doc_source", "bill_name", "party", "name", "district", "chunk_idx",
)


def _meta_to_record(meta: dict) -> dict:
    """ChromaDB-style metadata dict → arrow record fields."""
    out = {}
    for k in _META_KEYS:
        v = meta.get(k)
        if v is None:
            out[k] = None
        elif k in ("age", "chunk_idx"):
            try:
                out[k] = int(v) if v != "" else None
            except (TypeError, ValueError):
                out[k] = None
        else:
            out[k] = str(v)
    return out


class VectorDB:
    def __init__(self, path: Path | str = None,
                 table_name: str = None,
                 dim: int = None,
                 dtype: str = None):
        self.path = Path(path or cfg.LANCE_DIR)
        self.path.mkdir(parents=True, exist_ok=True)
        self.dim = dim or cfg.EMBED_DIM
        self.dtype = dtype or cfg.VECTOR_DTYPE
        self.np_dtype = np.float16 if self.dtype == "float16" else np.float32
        self.table_name = table_name or cfg.TABLE_NAME

        self.db = lancedb.connect(str(self.path))
        schema = _arrow_schema(self.dim, self.dtype)

        if self.table_name in self.db.table_names():
            self.table = self.db.open_table(self.table_name)
        else:
            self.table = self.db.create_table(self.table_name, schema=schema)
            log.info("created lance table %s/%s (dim=%d, dtype=%s)",
                     self.path, self.table_name, self.dim, self.dtype)

    # ── ChromaDB 호환 인터페이스 ─────────────────────────

    def upsert(self, chunk_ids: list[str], texts: list[str],
               embeddings: list[list[float]], metadatas: list[dict]) -> None:
        """N개 청크 upsert. embeddings는 float32 리스트로 들어와도 float16으로 변환."""
        # pyarrow Table로 명시 변환 (dict + numpy로는 merge_insert가 vector를 null 처리)
        n = len(chunk_ids)
        # 메타필드 추출
        meta_cols: dict[str, list] = {k: [None] * n for k in _META_KEYS}
        for i, meta in enumerate(metadatas):
            rec = _meta_to_record(meta or {})
            for k in _META_KEYS:
                meta_cols[k][i] = rec[k]

        # 벡터: numpy array of shape (N, dim) — pyarrow가 FixedSizeList로 변환
        vec_array = np.asarray(embeddings, dtype=self.np_dtype)

        # pyarrow Table 구성
        cols = {
            "chunk_id": pa.array(chunk_ids, type=pa.string()),
            "text": pa.array(texts, type=pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(
                pa.array(vec_array.flatten(),
                         type=pa.float16() if self.dtype == "float16" else pa.float32()),
                self.dim,
            ),
        }
        for k in _META_KEYS:
            if k in ("age", "chunk_idx"):
                cols[k] = pa.array(meta_cols[k], type=pa.int32())
            else:
                cols[k] = pa.array(meta_cols[k], type=pa.string())
        tbl = pa.Table.from_pydict(cols, schema=_arrow_schema(self.dim, self.dtype))

        self.table.merge_insert("chunk_id") \
            .when_matched_update_all() \
            .when_not_matched_insert_all() \
            .execute(tbl)

    def count(self) -> int:
        return self.table.count_rows()

    def query(self, query_embedding: list[float], top_k: int = 30,
              where: dict | None = None) -> dict:
        """벡터 KNN 쿼리. ChromaDB 응답 포맷으로 변환해 반환."""
        q = np.asarray(query_embedding, dtype=self.np_dtype)
        # cosine 명시 (default는 L2)
        search = self.table.search(q).metric("cosine").limit(top_k)
        if where:
            cond = self._where_to_sql(where)
            if cond:
                search = search.where(cond)
        rows = search.to_list()
        # ChromaDB-style 응답 (search.py 호환)
        ids = [r["chunk_id"] for r in rows]
        docs = [r["text"] for r in rows]
        metas = [{k: r.get(k) for k in _META_KEYS if r.get(k) is not None}
                 for r in rows]
        # _distance: cosine distance (0=동일, 2=반대)
        dists = [r.get("_distance", 0.0) for r in rows]
        return {
            "ids": [ids],
            "documents": [docs],
            "metadatas": [metas],
            "distances": [dists],
        }

    def delete(self, chunk_ids: list[str] | None = None,
               where: dict | None = None) -> None:
        if chunk_ids:
            ids_list = ",".join(f"'{c}'" for c in chunk_ids)
            self.table.delete(f"chunk_id IN ({ids_list})")
        elif where:
            cond = self._where_to_sql(where)
            if cond:
                self.table.delete(cond)

    def reset(self) -> None:
        self.db.drop_table(self.table_name)
        schema = _arrow_schema(self.dim, self.dtype)
        self.table = self.db.create_table(self.table_name, schema=schema)

    # ── 추가: LanceDB-native 기능 ─────────────────────

    def create_hnsw_index(self, m: int = 16,
                          ef_construction: int = 128) -> None:
        """HNSW 인덱스 빌드 (대량 insert 후 1회 권장)."""
        log.info("building HNSW index (m=%d, ef_construction=%d)...",
                 m, ef_construction)
        self.table.create_index(
            metric="cosine",
            vector_column_name="vector",
            index_type="IVF_HNSW_SQ",
            num_partitions=256,
            num_sub_vectors=96,
        )

    def get_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        """chunk_id 리스트로 행 조회 (search.py에서 BM25 후보 보강용)."""
        if not chunk_ids:
            return []
        ids_list = ",".join(f"'{c}'" for c in chunk_ids)
        df = self.table.search().where(
            f"chunk_id IN ({ids_list})"
        ).limit(len(chunk_ids)).to_list()
        return df

    # ── helper ────────────────────────────────────────

    def _where_to_sql(self, where: dict) -> str:
        """ChromaDB-style {"source": "bill", "age": 22} → SQL string."""
        parts = []
        for k, v in where.items():
            if isinstance(v, dict):
                if "$eq" in v:
                    parts.append(self._eq(k, v["$eq"]))
                elif "$in" in v:
                    items = ",".join(self._lit(x) for x in v["$in"])
                    parts.append(f"{k} IN ({items})")
            else:
                parts.append(self._eq(k, v))
        return " AND ".join(parts)

    @staticmethod
    def _eq(col: str, val) -> str:
        return f"{col} = {VectorDB._lit(val)}"

    @staticmethod
    def _lit(val) -> str:
        if isinstance(val, str):
            escaped = val.replace("'", "''")
            return f"'{escaped}'"
        return str(val)

    # ── ChromaDB 호환: collection 객체 노출 ─────────────

    @property
    def collection(self):
        """search.py가 self.vdb.collection.get(...) 호출하는 경우 대응."""
        return _CollectionFacade(self)


class _CollectionFacade:
    """search.py·bm25.py에서 chromadb collection.get(...) 사용 시 호환 wrapper."""
    def __init__(self, vdb: VectorDB):
        self._vdb = vdb

    def get(self, ids: list[str] = None, where: dict = None,
            limit: int = None, offset: int = 0,
            include: list[str] = None) -> dict:
        """ChromaDB-style get. include는 무시 (LanceDB는 항상 모든 컬럼 반환)."""
        if ids:
            rows = self._vdb.get_by_ids(ids)
        else:
            search = self._vdb.table.search()
            if where:
                cond = self._vdb._where_to_sql(where)
                if cond:
                    search = search.where(cond)
            if limit:
                search = search.limit(limit + offset)
            rows = search.to_list()
            if offset:
                rows = rows[offset:]
        return {
            "ids": [r["chunk_id"] for r in rows],
            "documents": [r["text"] for r in rows],
            "metadatas": [{k: r.get(k) for k in _META_KEYS
                            if r.get(k) is not None} for r in rows],
        }
