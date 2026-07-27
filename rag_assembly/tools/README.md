# embed_bundle — RAG 임베딩 재구축 (Gemini 1536 → arctic-ko 1024)

GPU가 필요한 구간만 별도 머신(RTX A5000 24GB)에 떼어 보내고 나머지는 로컬에서 도는
**3-스크립트 체계**. 청킹 로직은 `rag_assembly/{chunker,indexer}.py` 정본을 import 해서
재사용하며 여기서 재구현하지 않는다.

```
① 로컬   export_chunks.py       assembly_raw.duckdb  →  chunks_*.parquet + export_manifest.json
② GPU    bundle/embed_remote.py chunks_*.parquet     →  emb_*.parquet + run_manifest.json + _DONE.json
③ 로컬   ingest_embeddings.py   emb_* + chunks_*     →  LanceDB + BM25 + manifest.sqlite
④ 로컬   SERVING_CHECKLIST.md   ← 이걸 해야 검색이 산다 (코드 편집, 자동 아님)
```

| 파일 | 역할 |
|------|------|
| `export_chunks.py` | ① 청크 추출 (DuckDB read-only) |
| `bundle/` | ② GPU 머신으로 통째로 옮기는 폴더. 자세한 절차는 [`bundle/README.md`](bundle/README.md) |
| `ingest_embeddings.py` | ③ 인덱스 재조립 |
| `SERVING_CHECKLIST.md` | ④ 서빙 코드 전환 체크리스트 (**필수**) |
| `_selftest/` | 합성 데이터 배관 검사 (GPU·DB 불필요) |

계약 요약: `chunks_*.parquet`(chunk_id, text, source, metadata_json) →
`emb_XXXX.parquet`(chunk_id, `fixed_size_list<float16,1024>`, 샤드당 50,000행).
**조인 키는 chunk_id** 이므로 샤드 경계가 어긋나도 데이터가 섞이지는 않는다.

---

## ① export (로컬)

```bash
.venv/bin/python working/embed_bundle/export_chunks.py \
    --output-dir working/embed_bundle/chunks_out
```

- 대상 소스: `bill, bill_meta, document, speech, member` (`--sources` 로 선택)
- 예상 산출: 약 **263만 청크 / 14개 파일**(파일당 20만행) / **1~2 GB**(zstd) / 수십 분
- DuckDB는 **read_only** 로만 연다. 다른 프로세스가 write lock을 쥐고 있으면 그대로
  실패하니 쓰기 파이프라인이 끝난 뒤 실행할 것.
- **이어하기 없음.** 중간에 죽으면 `--overwrite` 로 처음부터 — 그 플래그는 완성된
  `chunks_*.parquet` 를 **전부 지운다**. 부분 실행이 필요하면 `--sources` 로 나눠
  서로 다른 `--output-dir` 에 떨군 뒤 파일명을 사전순으로 이어붙일 것
  (파일명 사전순 + 파일 내 행순 = 전역 행 순서).
- 스모크: `--limit 100`(소스별 100 *행*) → 별도 `--output-dir` 에.

산출: `chunks_000.parquet …` + `export_manifest.json`(파일별 sha256·소스별 통계·
청킹 파라미터·chunker/indexer 해시·git SHA).

> 정본 indexer와의 유일한 차이: 공백만 있는 청크를 드롭한다
> (`export_manifest.json::divergence_from_indexer`, `per_source.empty_dropped`).

## ② GPU 임베딩

**보낼 것**: `bundle/` 폴더 전체 + `chunks_*.parquet` + **`export_manifest.json`**
(마지막 것을 빼먹으면 전송 무결성 대조가 통째로 생략된다).

```bash
rsync -avP working/embed_bundle/bundle  user@gpu-box:~/embed_job/
rsync -avP working/embed_bundle/chunks_out/  user@gpu-box:~/embed_job/chunks/
```

이후는 [`bundle/README.md`](bundle/README.md) 그대로. 요약하면:

```bash
# GPU 머신에서
pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
pip install -r bundle/requirements.txt
python bundle/embed_remote.py --input-dir ./chunks --output-dir ./embeddings --max-shards 1  # 스모크
nohup python bundle/embed_remote.py --input-dir ./chunks --output-dir ./embeddings > embed.log 2>&1 &
```

**받아올 것**: `embeddings/` 폴더 **통째로** — `emb_XXXX.parquet` +
`run_manifest.json` + `_DONE.json`. 매니페스트 2개가 없으면 ③이 적재를 거부한다.

```bash
rsync -avP user@gpu-box:~/embed_job/embeddings/ working/embed_bundle/emb_in/
```

### 회수 전 수동 확인 (필수)

```bash
cat working/embed_bundle/emb_in/_DONE.json
```

- `"complete": true`
- `"vectors" == "expected_rows"`
- `"dry_run": false`  ← **가짜 벡터는 차원·L2 노름·chunk_id가 전부 정상이라
  데이터 검증만으로는 절대 구별되지 않는다.** 이 필드가 유일한 근거다.
- `_DRY_RUN` 파일이 폴더에 있으면 그 폴더는 통째로 버릴 것.

## ③ ingest (로컬)

```bash
# 먼저 dry-run 으로 검증만
.venv/bin/python working/embed_bundle/ingest_embeddings.py \
    --emb-dir working/embed_bundle/emb_in \
    --chunks-dir working/embed_bundle/chunks_out \
    --dry-run

# 실제 적재 (LanceDB 1024 테이블 생성 → HNSW → BM25 → manifest)
.venv/bin/python working/embed_bundle/ingest_embeddings.py \
    --emb-dir working/embed_bundle/emb_in \
    --chunks-dir working/embed_bundle/chunks_out
```

검증 순서: GPU 출처(run_manifest/_DONE) → parquet 스키마 → chunk_id 완전 대조 →
청크 sha256 재대조 → 벡터 샘플 값 검사. 하나라도 어긋나면 **exit 2** 로 멈추고
아무것도 쓰지 않는다.

| 플래그 | 언제 |
|--------|------|
| `--reset` | 기존 LanceDB 테이블(행 있음)을 버리고 새로 만들 때 |
| `--skip-bm25` | 벡터만 먼저. 나중에 **`--skip-bm25` 없이** 다시 돌리면 적재는 이어하기로 즉시 끝나고 BM25만 빌드된다 |
| `--skip-hnsw` | 인덱스 없이 brute-force 로 둘 때 |
| `--skip-hash-check` | 청크 sha256 재대조 생략(시간 절약, 무결성 포기) |
| `--allow-unverified-emb` | 매니페스트 없는 폴더를 강행 — **상시 사용 금지** |
| `--check-vector-shards N` | 값 검사할 샘플 샤드 수 (`-1` 전부) |

중단됐다면 같은 명령을 다시: 같은 `embed_config_version` 의 기존 행은 skip 한다.

## ④ 서빙 전환

적재가 끝나도 **검색은 아직 동작하지 않는다.** 서빙 코드가 여전히
`rag_assembly/config.py` 의 `gemini-embedding-001` / 1536 을 보기 때문.
→ [`SERVING_CHECKLIST.md`](SERVING_CHECKLIST.md) 를 끝까지 처리할 것.

---

## 배관 자체를 검사하고 싶을 때 (GPU·DB 불필요)

```bash
cd /tmp && mkdir -p embed_st && cd embed_st
.venv/bin/python .../\_selftest/make_fake_chunks.py ./chunks_fake
.venv/bin/python .../bundle/embed_remote.py --input-dir ./chunks_fake \
        --output-dir /tmp/embed_st/out_dry --dry-run --shard-size 30
.venv/bin/python .../\_selftest/verify_out.py ./chunks_fake /tmp/embed_st/out_dry
```

dry-run 출력 폴더는 **항상 별도로**. 실제 산출물이 있는 폴더에 dry-run 을 걸면
`--force` 로도 뚫리지 않는 에러로 막힌다.
