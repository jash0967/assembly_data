# embed_remote — GPU 머신용 임베딩 번들

`chunks*.parquet` 를 받아 **dragonkue/snowflake-arctic-embed-l-v2.0-ko** (568M, 1024차원)
로 임베딩해 `emb_XXXX.parquet` 샤드로 떨구는 단독 실행 스크립트입니다.
저장소(assembly_data)에 대한 의존이 없습니다 — 이 폴더 + 청크 파케이만 GPU 머신에 옮기면 됩니다.

- 대상 하드웨어: RTX A5000 24GB (CUDA 12.x), 인터넷 있음
- 이번 배치: **청크 3,264,019개 / parquet 17개 / 1.57 GB**. 정본 수치는 동봉된
  `chunks/export_manifest.json` 이며, 샤드 50,000행 기준 **66샤드**가 나온다.
- 예상 소요: **6~15시간**. RTX 4060(데스크톱과 GPU 공유)에서 **48~59 chunk/s 실측**,
  A5000 24GB + 배치 상향이면 100~150 chunk/s 기대 → 326만 기준 6.0시간(150 c/s)~
  15시간(60 c/s). **`--max-shards 1` 실측치로 일정을 확정할 것** (아래 3절).
- 디스크: 임베딩 산출물 약 **6.4 GB** (실측 1,957 B/vector × 326만), 모델 캐시 약 2.3 GB.
  입력 청크 1.6 GB 까지 합쳐 **최소 10 GB 여유**를 확보할 것.

---

## 0. 옮길 것 / 받아올 것

```
GPU 머신으로 →   bundle/            (embed_remote.py, requirements.txt, README.md)
                 chunks/            (chunks_000.parquet, ... + export_manifest.json)
                                    ← export_manifest.json 을 빼먹지 말 것.
                                      sha256 대조(전송 무결성)의 유일한 근거다.

GPU 머신에서 ←   embeddings/        (emb_0000.parquet ... , run_manifest.json, _DONE.json)
                                    ← 폴더를 통째로. 매니페스트 2개가 없으면
                                      회수 측 ingest 가 적재를 거부한다.
```

이 문서는 GPU 구간만 다룬다. 앞뒤 단계(로컬 export → GPU → 로컬 ingest)는
청크를 내보낸 로컬 저장소의 `working/embed_bundle/README.md` 에 있다.

## 1. 압축 풀고 venv 만들기

```bash
tar xf embed_bundle_arctic_ko_20260727.tar -C ~/            # → ~/embed_bundle_arctic_ko_20260727/
cd ~/embed_bundle_arctic_ko_20260727                        # 이하 모든 명령은 이 폴더에서
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
```

## 2. 설치

torch는 **CUDA 휠을 먼저** 넣어야 합니다 (기본 PyPI 휠이 CPU판일 수 있음).

```bash
pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
pip install -r bundle/requirements.txt

# CUDA 확인 — False 면 여기서 멈추고 휠부터 고칠 것
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

## 3. 실행 (한 줄)

```bash
python bundle/embed_remote.py --input-dir ./chunks --output-dir ./embeddings
```

장시간 작업이므로 세션이 끊겨도 살아남게:

```bash
nohup python bundle/embed_remote.py --input-dir ./chunks --output-dir ./embeddings \
      > embed.log 2>&1 &
tail -f embed.log
```

진행 로그는 샤드/전체 진행률, chunk/s, ETA, 현재 batch_size를 한 줄씩 출력합니다.

```
14:02:11   shard 0003 [4/66] 12,288/50,000 | 전체 162,288/3,264,019 (5.0%) | 168 chunk/s | ETA 5:07:33 | bs=64
14:07:44   → emb_0003.parquet 저장 (50,000행, 5.0분, 104.3 MB)
```

### 먼저 1샤드만 돌려보기 (GPU 스모크 테스트, 5분)

```bash
python bundle/embed_remote.py --input-dir ./chunks --output-dir ./embeddings --max-shards 1
```

성공하면 그대로 전체 실행을 이어서 하면 됩니다 (완성된 샤드는 자동 스킵).

이 1샤드(50,000청크)의 `chunk/s` 를 반드시 기록해 두세요. 전체 소요 =
`3,264,019 / (측정 chunk/s)` 입니다. 상단의 6~15시간은 4060 실측을 A5000으로 환산한
추정이고, 이 실측치가 유일하게 믿을 수 있는 숫자입니다.

`--max-shards 1` 로 끝낸 폴더에는 `_DONE.json` 이 생기지 않습니다(전 샤드 완료 시에만 기록).
정상 동작이며, 회수 측 ingest 는 `_DONE.json` 없는 폴더를 거부하므로 **스모크 폴더째로
회수하지 말고 같은 폴더에 전체 실행을 이어서** 끝내세요.

### 주요 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--input-dir` | `./chunks` | `chunks*.parquet` 위치 |
| `--output-dir` | `./embeddings` | 샤드 출력 |
| `--model` | `dragonkue/snowflake-arctic-embed-l-v2.0-ko` | HF ID 또는 로컬 경로 |
| `--batch-size` | 64 | GPU 배치. **OOM 나면 자동으로 절반씩 낮춰 재시도** |
| `--group-size` | 4096 | encode() 1회 분량 = 진행률 출력 주기 |
| `--dtype` | `float16` | 모델 연산 dtype (저장은 항상 float16) |
| `--resume` / `--no-resume` | resume on | 완성된 `emb_XXXX.parquet` 스킵 |
| `--max-shards N` | 없음 | 이번 실행에서 N개 샤드만 |
| `--revision` | `55ec6e93…` (고정) | HF 리비전. 문서 측·질의 측 가중치 동일성 보장 |
| `--max-seq-len` | 1024 | 토큰 절단 기준. **낮추지 말 것** (아래 8절) |
| `--no-verify-hashes` | off | `export_manifest.json` sha256 대조 생략 |
| `--dry-run` | off | 모델 없이 배관만 검사 (가짜 벡터). 실산출 폴더에는 거부됨 |

## 4. 오프라인 대비 — 모델 사전 다운로드

GPU 머신이 HF에 못 붙거나 중간에 네트워크가 죽는 상황을 피하려면 미리 받아둡니다.

모델 리비전은 스크립트가 `55ec6e9358a56d56af759bc8372e970caf8c305f` 로 **고정**합니다
(`--revision`). 문서 측(GPU)과 질의 측(로컬 서빙)이 반드시 같은 가중치를 써야 하기 때문입니다.
로컬 경로(`--model ./models/...`)로 로드할 때는 리비전 개념이 없어 자동으로 무시됩니다.

```bash
# huggingface_hub >= 0.34 이면 `hf download`, 그 이전이면 `huggingface-cli download`
hf download dragonkue/snowflake-arctic-embed-l-v2.0-ko \
   --revision 55ec6e9358a56d56af759bc8372e970caf8c305f --local-dir ./models/arctic-ko
# 또는
huggingface-cli download dragonkue/snowflake-arctic-embed-l-v2.0-ko \
   --revision 55ec6e9358a56d56af759bc8372e970caf8c305f --local-dir ./models/arctic-ko

# 다운로드 확인 (config.json, model.safetensors, tokenizer.json, 1_Pooling/ 등)
ls ./models/arctic-ko
```

이후 로컬 경로로 실행 + 네트워크 완전 차단:

```bash
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
python bundle/embed_remote.py --input-dir ./chunks --output-dir ./embeddings \
      --model ./models/arctic-ko
```

(캐시 위치를 옮기고 싶으면 `export HF_HOME=/data/hf_cache` 를 pip 설치 직후에 잡아두세요.)

**더 빠른 길 — 로컬 머신에 이미 완전한 스냅샷이 있습니다** (2.2 GB, 같은 리비전).
다운로드도 아끼고 가중치 동일성도 보장됩니다.

```bash
rsync -avP ~/.cache/huggingface/hub/models--dragonkue--snowflake-arctic-embed-l-v2.0-ko \
      user@gpu-box:~/.cache/huggingface/hub/
```

## 5. 중단·재시작

`emb_XXXX.parquet` 은 **임시파일 → rename** 으로만 만들어지므로, 존재하는 샤드 파일은 항상 완전본입니다.
Ctrl-C, OOM, 정전 무엇으로 끊기든 **같은 명령을 다시 실행**하면 완성된 샤드는 건너뛰고 이어서 진행합니다.

- 죽다 만 `.emb_XXXX.parquet.tmp` 는 다음 실행 시작 시 자동 삭제됩니다.
- 출력 폴더의 `run_manifest.json` 이 입력 인벤토리 지문을 들고 있어, **청크 파일이 바뀐 채로 이어하기**를
  시도하면(샤드 경계가 어긋남) 에러로 막습니다. 청크를 다시 만들었다면 출력 폴더를 비우고 처음부터 돌리세요.

## 6. 결과 회수

전 샤드가 끝나면 `_DONE.json` 이 생깁니다. 이걸 먼저 확인하세요.

```bash
cat embeddings/_DONE.json      # complete: true, vectors == expected_rows 인지 확인
ls embeddings/ | wc -l
```

회수 (로컬 → GPU 머신 방향 pull 예시):

```bash
rsync -avP user@gpu-box:~/embed_bundle_arctic_ko_20260727/embeddings/ \
      /path/to/assembly_data/working/embed_bundle/embeddings/
# 또는
scp -r user@gpu-box:~/embed_bundle_arctic_ko_20260727/embeddings .
```

`run_manifest.json` / `_DONE.json` 을 **반드시** 같이 가져오세요. 회수 측
`ingest_embeddings.py` 는 이 두 파일을 읽어 `dry_run=false`, `complete=true`,
model·dim·shard_size·샤드 수·총 행수, 그리고 청크 파일 sha256 을 대조하며,
파일이 없으면 적재를 거부합니다(exit 2).

`_DONE.json` 확인은 **수동 필수 단계**입니다 — `complete: true` 와
`vectors == expected_rows` 를 눈으로 확인한 뒤 회수하세요.

## 7. 출력 스키마 (계약)

```
emb_XXXX.parquet   (zstd)
  chunk_id : string
  vector   : fixed_size_list<float16, 1024>   ← L2 정규화 완료
```

- 샤드당 50,000행 (마지막 샤드만 그보다 작을 수 있음). 파일명 인덱스 = 전역 행 순서 / 50,000.
- 전역 행 순서 = `chunks*.parquet` **파일명 사전순 + 파일 내 행순**. 입력을 재정렬하면 안 됩니다.
- 벡터는 정규화되어 있으므로 코사인 = 내적. `float16` 저장은 기존 LanceDB 스키마
  (`pa.list_(pa.float16(), dim)`)와 동일한 표현입니다.

## 8. 이 모델의 올바른 사용법 (중요)

- **문서(passage) 측에는 어떤 접두사도 붙이지 않습니다.** 이 스크립트는 청크 텍스트를 가공 없이 인코딩합니다.
- **질의(query) 측에만 `"query: "` 접두사**를 붙입니다. 검색 시점 코드(회수 측 `embedder`/`search`)에서
  처리해야 하며, 이 번들의 책임이 아닙니다. (스크립트 상단 `QUERY_PREFIX` 상수는 문서화용으로 남겨둠)
- **최대 시퀀스 길이는 이 스크립트가 1024로 잡습니다** (`--max-seq-len`, 기본 1024).
  모델 자체의 한계는 8192(`sentence_bert_config.json`)이고, 512는 모델 한계가 아닙니다.
  512로 낮추면 1,200~1,500자 청크(단일 청크 경로·`speaker_split` 버퍼)가 실측 28~34% 잘립니다
  (800자 청크는 400~415토큰이라 512에서도 안전). 회수 측 `ingest_embeddings.py`의
  `DEFAULT_MAX_SEQ_TOKENS`도 같은 값이며, 실제 기록은 `run_manifest.json::max_seq_len`에서 읽습니다.
  **이 값을 바꾸면 embed_config 기록도 따라 바뀝니다 — 임의로 낮추지 마세요.**

## 9. 트러블슈팅

| 증상 | 조치 |
|------|------|
| `CUDA out of memory` | 자동으로 batch를 반감해 재시도합니다. 계속 나면 `--batch-size 16` 으로 시작 |
| 속도가 10 chunk/s 수준 | CPU 휠로 돌고 있을 확률. `torch.cuda.is_available()` 확인 |
| `trust_remote_code` 관련 에러 | 자동 재시도하지만, 안 되면 `--trust-remote-code` 명시 |
| 임베딩 차원 불일치 에러 | 모델이 잘못됐거나 다른 체크포인트. `--dim` 을 바꾸지 말고 모델을 확인할 것 |
| 다른 GPU를 쓰고 싶다 | `--device cuda:1` |
| `전송 중 청크 파일이 손상됐습니다` | 해당 `chunks_*.parquet` 를 다시 전송. 정말 급하면 `--no-verify-hashes` 지만 회수 측에서 다시 걸린다 |
| `export_manifest.json 없음` 경고 | 청크와 같은 폴더에 `export_manifest.json` 을 넣고 재실행. 없이 돌리면 전송 무결성 검사가 끝까지 생략된다 |

## 10. 개발자용 — dry-run

모델·GPU 없이 파이프라인(입력 스캔 → 샤딩 → 원자적 쓰기 → 이어하기)만 검사합니다.
가짜 벡터가 나오므로 **반드시 별도 출력 폴더**에 쓰세요. 가짜 벡터는 차원·L2 노름·chunk_id가
전부 정상이라 회수 측 데이터 검증을 그대로 통과합니다 — 구별 근거는 매니페스트뿐입니다.

- dry-run 은 `_DRY_RUN` 마커와 `run_manifest.json{dry_run:true}` 를 남깁니다.
- 그 폴더에 **실제 실행**을 시도하면 거부합니다.
- 반대로 **실제 산출물(`emb_*.parquet` 또는 `dry_run:false` 매니페스트)이 있는 폴더에
  dry-run 을 시도해도 거부**합니다. 이쪽은 `--force` 로도 뚫리지 않습니다.

```bash
python bundle/embed_remote.py --input-dir ./chunks_fake --output-dir /tmp/out_dry --dry-run
```
