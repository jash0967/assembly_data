# 적재 재개 안내 (WSL 메모리 상향 후)

작성: 2026-07-27. 세션이 끊겨도 이 파일만 보면 이어서 진행할 수 있다.

## 현재까지 완료된 것 (디스크에 영구 저장됨 — 다시 안 해도 됨)

- **전송·무결성 검증 통과**: 66/66 샤드, 3,264,019 벡터, chunks↔emb chunk_id 완전 대조
  (벡터없음 0, 고아벡터 0), chunks 17개 sha256이 GPU 기록과 일치.
- **LanceDB 적재 완료**: `rag_assembly/data/lance_db` — 3,264,019행, 4.6분.
- **HNSW 인덱스 완료**: partitions=256, sub_vectors=64, 4.0분.
- **embed_config 기록 완료**: `rag_assembly/data/embed_config.json` + `manifest.sqlite::embed_config`.
- 서빙 측(질의 임베딩 로컬 arctic-ko) 코드는 커밋 `733ef34`에 반영됨.

## 미완: BM25 인덱스만

13개 서브인덱스 중 첫 소스(`bill`, 797,776청크) 토큰화 중 메모리 부족으로 중단.
`rag_assembly/data/bm25/build_state.json`에 진행 상태가 남아 있다.

## 재개 명령

```bash
cd /home/jays0967/assembly_data
nohup .venv/bin/python working/embed_bundle/ingest_embeddings.py \
  --emb-dir embed_arctic_ko_20260727/embeddings \
  --chunks-dir working/embed_bundle/chunks_out \
  --bm25-workers 6 \
  > /tmp/ingest2.out 2> /tmp/ingest2.err &
```

- LanceDB는 이미 적재돼 있어 `--upsert`/`--no-resume` 없이 돌리면 기존 행을 skip한다.
- BM25만 남았으므로 `build_state.json` 기준으로 이어서 빌드된다.
- 메모리가 64GB로 올라갔다면 `--bm25-workers`를 6~10으로. 여전히 빠듯하면 2~3으로 낮출 것
  (부모가 본문 전체를 메모리에 올린 뒤 fork하는 구조라 워커 수가 메모리에 직결).

## 재개 후 확인

```bash
# 1) BM25 서브인덱스 13개 생성 확인
ls rag_assembly/data/bm25/

# 2) 계약 대조 (사이드카 ↔ 서빙 상수)
cd rag_assembly && ../.venv/bin/python embedder.py --contract

# 3) MCP 통해 실검색
#    rag_stats → 3,264,019 확인
#    rag_search("인공지능 규제") → 결과 반환 확인
#    rag_search_bills(...) → source=bill 만 나오는지 (소스 필터 회귀 확인)
```

## 메모리 설정 (Windows 측, `%UserProfile%\.wslconfig`)

```ini
[wsl2]
memory=64GB
swap=16GB
```

호스트 96GB 기준. 적용하려면 Windows에서 `wsl --shutdown` (WSL 내부 프로세스 전부 종료됨).
