# Windows 서빙 셋업 — `assembly-db` MCP 서버

WSL에서 만든 RAG 인덱스·DuckDB를 **같은 물리 머신의 Windows**에 복사해, Windows쪽
Claude Code가 `assembly-db` MCP 서버를 stdio로 띄워 쓰게 만드는 절차.

> **Windows는 서빙(읽기)만 한다.** 수집·인덱스 빌드·분류는 계속 WSL에서. → §9
>
> 소요: 복사 30~90분(33GB, 9p 전송) + 설치 15~30분. 대부분 기다리는 시간이다.

전제: WSL 정본이 `/home/jays0967/assembly_data`, Windows에 D: 드라이브가 있고,
`\\wsl.localhost\Ubuntu\...` 로 WSL 파일에 접근 가능(= 기본 상태).

---

## 0. 무엇을 옮기는가

| 대상 | 크기 | WSL 위치 | Windows 역할 |
|---|---|---|---|
| 코드 | ~0.1 MB | `duckdb_mcp_server.py`, `config.py`, `rag_assembly/*.py` | 서버 본체 |
| RAG 인덱스 | **24 GB** | `rag_assembly/data/` (LanceDB 21G + BM25 3G + manifest.sqlite 754M) | `rag_search*` |
| DuckDB ×4 | **~9 GB** | `data/bills_kr/*.duckdb`, `data/news/*.duckdb` | `query`, `list_tables` … |
| 임베딩 모델 | 2.2 GB | HF 캐시 `models--dragonkue--snowflake-arctic-embed-l-v2.0-ko` | 질의 임베딩 |

코드 쪽에서 **반드시 같이 가야 하는 것**: 저장소 루트의 `config.py`.
`rag_assembly/config.py` 가 import 시점에 루트 `config.py` 를 exec 하기 때문이다
(그래서 `python-dotenv` 도 서빙 의존성에 들어간다). 동봉된 sync 스크립트는 이걸 이미 챙긴다.

---

## 1. 리소스 요건 (실측)

| 항목 | 값 | 근거 |
|---|---|---|
| 디스크 | **45 GB 이상 여유** | 데이터 33GB + venv 약 6GB(대부분 torch CUDA 휠) + 모델 2.2GB |
| RAM | **16 GB 권장** (최소 12) | BM25 13개 sub-index 전량 상주 시 peak RSS **5.3 GB** 실측 + torch/모델 |
| VRAM | 약 1.2 GB | arctic-embed-l fp16. RTX 4060 8GB면 충분 |
| 첫 기동 | initialize 2.1초 / BM25 백그라운드 로드 31.5초(콜드), 7.7초(웜) | WSL 실측. Windows 첫 실행은 파일 캐시가 비어 더 걸릴 수 있다 |

BM25 로드는 **백그라운드 데몬 스레드**라 handshake를 막지 않는다. 로드 중에
`rag_search*` 를 부르면 "로딩 중" 안내가 오고, `rag_stats` 와 DuckDB 도구 4종은
그 동안에도 정상 응답한다.

---

## 2. 배치 (디렉터리 레이아웃)

sync 스크립트가 만드는 표준 배치. 이 문서와 `mcp.windows.json` 이 이걸 전제한다.

```
D:\assembly_serving\
├── repo\                      코드 (duckdb_mcp_server.py, config.py, rag_assembly\*.py)
├── data\                      ← ASSEMBLY_DATA_DIR
│   ├── bills_kr\assembly_raw.duckdb, assembly_analysis.duckdb
│   └── news\news.duckdb, news_analysis.duckdb
├── rag_data\                  ← RAG_DATA_DIR   (= WSL rag_assembly/data 의 내용)
│   ├── lance_db\chunks.lance\
│   ├── bm25\  (13개 sub-index + manifest.json)
│   ├── manifest.sqlite
│   └── embed_config.json
├── hf_cache\                  ← HF_HOME
│   └── hub\models--dragonkue--snowflake-arctic-embed-l-v2.0-ko\
└── venv\                      Windows 전용 venv (§4에서 직접 만든다)
```

**드라이브는 자유.** 위 경로가 그대로일 필요는 없다 — §7의 env 3개만 실제 위치와
맞으면 된다. 다만 `data\` 아래의 `bills_kr\`·`news\` 하위 구조는 유지해야 한다
(`ASSEMBLY_DATA_DIR` 는 그 두 폴더의 부모를 가리킨다). 개별 DB를 따로 흩어 놓아야
하면 §12의 파일 단위 env 4개를 쓴다.

> **env를 아예 안 쓰는 배치도 가능하다.** 코드를 `X\repo\`, 데이터를 `X\repo\data\`,
> 인덱스를 `X\repo\rag_assembly\data\` 에 두면 저장소 기본 경로와 같아져 env가
> 필요 없다. 하지만 WSL 정본과 헷갈리기 쉬워 권장하지 않는다.

---

## 3. 1단계 — 복사 (WSL → Windows)

### 3.1 권장: sync 스크립트 (WSL에서 실행)

```bash
cd /home/jays0967/assembly_data

# 먼저 무엇이 복사될지만 확인 (아무것도 쓰지 않는다)
bash rag_assembly/tools/windows/sync_to_windows.sh --all --dry-run /mnt/d/assembly_serving

# 실제 복사 (코드 + DuckDB + RAG 인덱스 + 모델 캐시)
bash rag_assembly/tools/windows/sync_to_windows.sh --all /mnt/d/assembly_serving
```

- 중간에 끊겨도 **같은 명령을 다시** 실행하면 이어받는다(`rsync --partial-dir`).
- 끝나면 (1) rsync 재대조로 전 파일 크기·시각 확인, (2) 표본 sha256 바이트 대조를
  자동으로 돌리고, 마지막에 **`.mcp.json` 에 넣을 env 블록을 Windows 경로로 출력**한다.
  그 출력을 §7에 그대로 쓰면 된다.
- 그룹만 따로: `--code` / `--duckdb` / `--rag` / `--model`. 코드만 고쳤을 땐 `--code` 가 몇 초.
- 옵션 전체는 `bash ... sync_to_windows.sh --help`.

### 3.2 수동 대안 A — WSL에서 `/mnt/d` 로 직접 복사

스크립트를 쓰지 않겠다면 최소한 이 형태로. (rsync 여야 이어받기가 된다)

```bash
D=/mnt/d/assembly_serving
mkdir -p "$D"/{repo/rag_assembly,data/bills_kr,data/news,rag_data}

RS="rsync -rlt --no-perms --no-owner --no-group --modify-window=2 \
    --partial-dir=.rsync-partial --info=progress2 -h"

# 코드
$RS duckdb_mcp_server.py config.py prompts.py bill_loaders.py "$D/repo/"
$RS --include='*.py' --exclude='*' rag_assembly/ "$D/repo/rag_assembly/"
$RS rag_assembly/tools/windows/ "$D/repo/rag_assembly/tools/windows/"

# 데이터
$RS data/bills_kr/assembly_raw.duckdb data/bills_kr/assembly_analysis.duckdb "$D/data/bills_kr/"
$RS data/news/news.duckdb data/news/news_analysis.duckdb "$D/data/news/"
$RS rag_assembly/data/ "$D/rag_data/"

# 모델 캐시 (blobs 제외 + 심볼릭 링크 실체화 — NTFS에는 링크를 못 만든다)
$RS -L --exclude='blobs/' \
   ~/.cache/huggingface/hub/models--dragonkue--snowflake-arctic-embed-l-v2.0-ko/ \
   "$D/hf_cache/hub/models--dragonkue--snowflake-arctic-embed-l-v2.0-ko/"
```

`cp -r` 도 되지만 진행률·이어받기·검증이 없다. 33GB에는 권하지 않는다.

### 3.3 수동 대안 B — Windows에서 끌어오기

탐색기 주소창 또는 PowerShell에서:

```
\\wsl.localhost\Ubuntu\home\jays0967\assembly_data
```

(구형 표기 `\\wsl$\Ubuntu\...` 도 같은 곳이다.) robocopy 예:

```powershell
robocopy "\\wsl.localhost\Ubuntu\home\jays0967\assembly_data\rag_assembly\data" `
         "D:\assembly_serving\rag_data" /E /Z /R:2 /W:5 /MT:8 /NP
```

`/Z`(재시작 가능), `/E`(빈 폴더 포함). robocopy는 WSL 심볼릭 링크를 만나면 실패하므로
**모델 캐시에는 쓰지 말 것** (인덱스·DuckDB에는 심볼릭 링크가 없다 — 실측 0개).

### 3.4 왜 로컬 드라이브에 복사하나

`\\wsl.localhost\` 는 9p 프로토콜을 타는 파일 공유다. 상시 서빙 경로로 쓰면
LanceDB의 수백 개 조각 파일과 BM25 npy를 매번 그 위로 읽어 눈에 띄게 느리고,
WSL 배포판이 꺼져 있으면 아예 접근이 안 된다. **한 번 복사해서 로컬 NTFS에 두는 것**이
정답이다. 같은 이유로 복사 자체도 9p를 타니 33GB에 30~90분을 잡아 두자.

---

## 4. 2단계 — Python · venv · 의존성 (Windows)

### 4.1 Python

**3.12 권장** (WSL 정본 3.12.3과 동일). 3.11/3.13도 가능. **3.14는 쓰지 말 것** —
고정한 `numpy 2.2.6` 에 cp314 Windows 휠이 없어 소스 빌드로 떨어진다.

이미 있는 3.11이 다른 앱의 venv 안이라면 그건 쓰지 말고 새로 설치한다.

```powershell
winget install Python.Python.3.12
py -0p          # 설치 확인 — 3.12 경로가 보여야 한다
```

### 4.2 venv

```powershell
py -3.12 -m venv D:\assembly_serving\venv
```

이후 모든 명령은 **activate 없이 전체 경로로** 부른다(ExecutionPolicy 문제 회피):

```powershell
D:\assembly_serving\venv\Scripts\python.exe -V
D:\assembly_serving\venv\Scripts\python.exe -m pip install --upgrade pip
```

> `pythonw.exe` 를 쓰면 안 된다. 서버가 진단을 `sys.stderr` 로 쓰는데 pythonw에서는
> `sys.stderr is None` 이라 그 지점마다 죽는다. 반드시 `python.exe`.

### 4.3 torch 먼저 (순서 중요)

```powershell
D:\assembly_serving\venv\Scripts\python.exe -m pip install torch==2.12.0 `
    --index-url https://download.pytorch.org/whl/cu130
```

CUDA를 안 쓸 거면 `--index-url https://download.pytorch.org/whl/cpu`.

**순서를 지켜야 하는 이유**: 다음 단계의 `sentence-transformers` 가 torch를 요구하는데,
그때 torch가 없으면 PyPI의 **CPU 전용** 휠을 끌어와 GPU를 못 쓰게 된다.

### 4.4 나머지 의존성

```powershell
D:\assembly_serving\venv\Scripts\python.exe -m pip install `
    -r D:\assembly_serving\repo\rag_assembly\tools\windows\requirements-serving.txt
```

확인:

```powershell
D:\assembly_serving\venv\Scripts\python.exe -c "import torch;print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
# 기대: 2.12.0+cu130 13.0 True     ← cuda 가 None 이면 CPU 휠이 깔린 것 (§11)
D:\assembly_serving\venv\Scripts\python.exe -c "import mcp,duckdb,lancedb,bm25s,kiwipiepy,sentence_transformers;print('imports ok')"
```

### 4.5 사내 SSL 검사(TLS inspection) 환경이라면

이 환경에서 실제로 겪은 이슈다. 기업 방화벽이 TLS를 가로채면 PyPI/HuggingFace가
`SSLCertVerificationError` 로 죽는다. **Windows 인증서 저장소를 쓰게 하는 게 정답**이다.

```powershell
# pip
D:\assembly_serving\venv\Scripts\python.exe -m pip install truststore
D:\assembly_serving\venv\Scripts\python.exe -m pip install --use-feature=truststore `
    -r D:\assembly_serving\repo\rag_assembly\tools\windows\requirements-serving.txt
```

(pip 24.2+ 는 truststore가 이미 기본일 수 있다. 그래도 `--use-feature=truststore` 는 무해.)

HuggingFace 다운로드에는 프로세스 안에서 주입한다 — §5의 스크립트에 이미 들어 있다.
회사 루트 CA의 PEM 파일이 있다면 대안:

```powershell
$env:SSL_CERT_FILE="C:\certs\corp-root.pem"
$env:REQUESTS_CA_BUNDLE=$env:SSL_CERT_FILE
```

**인증서 검증을 끄지 말 것.** 그럴 필요 자체가 없다 — §5의 "캐시 복사" 경로를 쓰면
Windows에서 네트워크를 한 번도 안 탄다.

---

## 5. 3단계 — 임베딩 모델 캐시

서버는 `dragonkue/snowflake-arctic-embed-l-v2.0-ko` 를 **revision 고정**으로 로드한다
(`55ec6e9358a56d56af759bc8372e970caf8c305f` — 문서 임베딩을 만든 바로 그 가중치).

### 방법 A (권장) — WSL 캐시를 복사

`sync_to_windows.sh --all` 또는 `--model` 이 이미 해 준다. 네트워크·SSL을 전혀 안 탄다.
`blobs/` 를 빼고 `snapshots/` 의 링크를 실체화해 넣으므로 2.2GB로 끝나고,
huggingface_hub은 이 형태를 정상 캐시로 인식한다(개발자 모드가 꺼진 Windows에서
HF 자신이 만드는 형태와 동일).

### 방법 B — Windows에서 직접 내려받기

```powershell
$env:HF_HOME="D:\assembly_serving\hf_cache"
D:\assembly_serving\venv\Scripts\python.exe -c @'
import truststore; truststore.inject_into_ssl()   # SSL 검사 환경이 아니면 이 줄 삭제
from huggingface_hub import snapshot_download
p = snapshot_download("dragonkue/snowflake-arctic-embed-l-v2.0-ko",
                      revision="55ec6e9358a56d56af759bc8372e970caf8c305f")
print(p)
'@
```

### 확인

```powershell
dir D:\assembly_serving\hf_cache\hub\models--dragonkue--snowflake-arctic-embed-l-v2.0-ko\snapshots\55ec6e9358a56d56af759bc8372e970caf8c305f
```

`model.safetensors`, `tokenizer.json`, `config.json`, `modules.json`, `1_Pooling\` 이 보이면 된다.
이 스냅샷이 캐시에 있으면 서버는 `local_files_only=True` 로 로드해 **HTTP 왕복을 아예 하지
않는다**(`embedder.py::pinned_snapshot_is_cached`). 없으면 기동 시 다운로드를 시도한다.

> reranker(`BAAI/bge-reranker-v2-m3`, 2.2GB)는 **복사할 필요 없다.** MCP 경로는
> rerank 없이 dense+BM25+RRF만 쓴다(FastMCP 컨텍스트에서 reranker 로드가 hang하던
> 이력 때문에 의도적으로 비활성).

---

## 6. 4단계 — 경로 점검

MCP에 등록하기 **전에** 서버를 띄우지 않고 경로만 확인한다.

```powershell
$env:ASSEMBLY_DATA_DIR="D:\assembly_serving\data"
$env:RAG_DATA_DIR="D:\assembly_serving\rag_data"
$env:HF_HOME="D:\assembly_serving\hf_cache"
$env:PYTHONUTF8="1"
D:\assembly_serving\venv\Scripts\python.exe D:\assembly_serving\repo\duckdb_mcp_server.py --paths
```

기대 출력 — 7줄 전부 `OK`:

```
[mcp] path analysis_db: OK   D:\assembly_serving\data\bills_kr\assembly_analysis.duckdb
[mcp] path raw_db: OK   D:\assembly_serving\data\bills_kr\assembly_raw.duckdb
[mcp] path news_analysis_db: OK   D:\assembly_serving\data\news\news_analysis.duckdb
[mcp] path news_raw_db: OK   D:\assembly_serving\data\news\news.duckdb
[mcp] path rag_data_dir: OK   D:\assembly_serving\rag_data
[mcp] path rag_bm25_manifest: OK   D:\assembly_serving\rag_data\bm25\manifest.json
[mcp] path rag_lance_table: OK   D:\assembly_serving\rag_data\lance_db\chunks.lance
```

`없음` 이 하나라도 있으면 그 줄이 **고칠 환경변수 이름을 직접 알려준다**. 고치고 다시.
(같은 목록이 두 번 보일 수 있다 — 하나는 import 시점 stderr 진단, 하나는 `--paths` 의
stdout 출력이다. 정상이다.)

---

## 7. 5단계 — MCP 등록

`mcp.windows.json` 을 Windows쪽 Claude Code 프로젝트 루트에 `.mcp.json` 으로 복사하고
경로를 실제 값으로 고친다. §3.1 스크립트가 마지막에 출력한 블록을 그대로 붙여 넣으면 된다.

```powershell
copy D:\assembly_serving\repo\rag_assembly\tools\windows\mcp.windows.json .mcp.json
notepad .mcp.json
```

```json
{
  "mcpServers": {
    "assembly-db": {
      "command": "D:\\assembly_serving\\venv\\Scripts\\python.exe",
      "args": ["D:\\assembly_serving\\repo\\duckdb_mcp_server.py"],
      "env": {
        "ASSEMBLY_DATA_DIR": "D:\\assembly_serving\\data",
        "RAG_DATA_DIR": "D:\\assembly_serving\\rag_data",
        "HF_HOME": "D:\\assembly_serving\\hf_cache",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

주의점:

- JSON에는 주석을 못 쓴다. 경로 설명은 이 문서가 정본이다.
- **백슬래시는 `\\` 로 두 번.** 슬래시(`D:/assembly_serving/...`)로 써도 Python은 잘 받는다.
- 서버는 모든 경로를 절대경로로 조립하므로 Claude Code의 작업 디렉터리는 무관하다.
- `PYTHONUTF8=1` 은 권장 사항이다. 서버 자신은 stderr를, 임베딩 subprocess는 stdin/stdout을
  UTF-8로 고정하지만, 서드파티가 여는 파일까지는 보장할 수 없다(cp949 기본값 사고 예방).
  JSON-RPC 채널(stdout)은 mcp SDK가 직접 UTF-8로 감싸므로 로케일과 무관하다.
- 다른 env는 §12. 기본값이 이미 옳으니 굳이 넣지 않는다.

---

## 8. 6단계 — 동작 확인

Claude Code(Windows)를 열고 `/mcp` 로 `assembly-db` 가 connected 인지 본 뒤, 순서대로.

### (1) `rag_stats` — 인덱스가 제대로 보이는지

기대 출력 (WSL 정본과 **완전히 같아야 한다**):

```
=== rag_assembly 인덱스 현황 ===
LanceDB total chunks: 3,264,019
embed_config_version: v3_arctic_ko_1024_fp16_20260726
embed model: dragonkue/snowflake-arctic-embed-l-v2.0-ko (dim=1024, query_prefix='query: ', device=cuda, warmup=ready)

소스별 분포:
  bill            797,776
  bill_meta        99,158
  document      2,254,799
  member              299
  speech          111,987
```

판정 기준:

- `3,264,019` — 이 숫자가 다르면 인덱스 복사가 덜 됐다.
- **`소스별 분포`가 비어 있으면** manifest.sqlite 를 못 읽거나 `embed_config_version`
  이 어긋난 것이다(카운트는 이 버전으로 필터한다).
- `device=cuda` — `cpu` 로 나오면 CPU torch가 깔렸거나 CUDA 초기화 실패. 검색은 되지만 느리다.
- `warmup=ready` — `loading` 이면 몇 초 더 기다린다. `failed` 면 §11.
- `⚠ embed_config 계약 불일치` 줄이 뜨면 인덱스와 서빙 상수가 어긋난 것 — 복사가
  섞였을 가능성이 크다.

### (2) `rag_search` — 한국어 질의가 실제로 맞는 문서를 집는지

질의 예: `인공지능 기본법 규제 샌드박스`

BM25 로딩 중이면 안내 메시지가 온다. 30초쯤 뒤 다시. 정상이면 결과마다
`source=... ref=...` 헤더와 본문 조각이 붙는다. **한글이 깨져 보이면 안 된다** —
깨진다면 인코딩 문제(§11)이고, 조용히 엉뚱한 결과가 나오는 상태일 수 있다.

### (3) `rag_search_bills` — 소스 필터가 먹는지

같은 질의로 호출했을 때 **모든 결과에 `source=bill`** 이 붙어야 한다. 예:

```
결과: 2건

[1] source=bill ref=PRC_F2D5D0B9A1B9W1X0V3W8U4V3T5B4C0
    bill_name=인공지능산업 발전 특별법안 | age=22 | committee=산업통상자원중소벤처기업위원회 | propose_date=2025-09-22
    ...
```

`source=document` 나 `source=speech` 가 섞여 나오면 메타필터가 안 먹은 것이다.

### (4) DuckDB 도구 — ATTACH 3개가 다 붙었는지

```sql
SELECT (SELECT COUNT(*) FROM raw.v_bill)                  AS vbill,
       (SELECT COUNT(*) FROM news_analysis.news_articles) AS news_analysis,
       (SELECT COUNT(*) FROM news_raw.news_articles)      AS news_raw;
```

WSL 정본과 같은 값이 나오면 된다. 2026-07-27 실측: `99158 / 76645 / 157886`.
세 카탈로그가 다 답하면 read-only ATTACH가 정상이다. 하나만 실패하면 그 DB 파일만
빠진 것이고(서버는 나머지를 살려 둔다), stderr에 어느 env로 고치라는 경고가 1회 찍힌다.

---

## 9. 서빙 전용 제약 — Windows에서 **하면 안 되는 것**

Windows 사본은 **읽기 전용 스냅샷**이다. 다음은 WSL에서만.

| 금지 | 왜 |
|---|---|
| RAG 인덱스 빌드 (`bm25.py::build`, `ingest_embeddings.py`, `export_chunks.py`) | BM25 빌드가 `multiprocessing.Pool` = **fork 전제**다. Windows는 spawn이라 동작 자체가 미검증이며, 반쯤 만들어진 인덱스는 조용히 틀린 검색 결과를 낸다. 서빙 경로(`load`/`search`)는 프로세스를 안 띄우므로 **무관** — 그래서 서빙만은 Windows에서 안전하다 |
| 임베딩 대량 생성 (`embed_remote.py`) | GPU 배치 작업 + 디렉터리 fsync 등 POSIX 전제. 8GB VRAM으로 326만 청크는 애초에 무리 |
| 수집 (`collect/*`) | WSL 경로·`venv/Scripts/hwp5txt` 전제이고 `assembly_raw.duckdb` 에 **쓴다**. Windows 사본에 쓰면 WSL 정본과 갈라진다 |
| 분류 (`analyze/classify_*`) | `assembly_analysis.duckdb` / `news_analysis.duckdb` 에 쓴다. 위와 같은 이유 |
| 그림·리포트 생성 (`analyze/make_figures.py` 등) | 의존성이 requirements-serving.txt 에 아예 없다 |

서버가 여는 DuckDB는 **전부 `read_only=True`** 라 DB 쪽 쓰기는 발생하지 않는다.

**단 하나의 예외**: `rag_stats` 가 `manifest.sqlite` 를 sqlite3 기본(읽기·쓰기) 모드로
연다(기존 설계). 데이터는 바뀌지 않지만 **파일에 읽기 전용 속성을 걸면 안 되고**,
쓰기 가능한 위치에 있어야 한다(SQLite가 옆에 journal 파일을 만들 수 있다).
읽기 전용 네트워크 공유에 두지 말 것.

---

## 10. 데이터 갱신 절차

WSL에서 재수집·재분류·재빌드한 뒤 Windows에 반영하는 순서.

1. **WSL에서** 갱신을 끝낸다(수집 → 분류 → 필요 시 인덱스 재빌드).
2. **Windows Claude Code를 종료한다.** MCP 서버가 살아 있으면 DuckDB·LanceDB·
   manifest.sqlite 파일이 잠겨 있어 덮어쓰기가 `Permission denied` 로 실패한다.
3. WSL에서 sync 재실행 — 변경된 파일만 간다.

   ```bash
   # DB만 갱신된 경우
   bash rag_assembly/tools/windows/sync_to_windows.sh --duckdb /mnt/d/assembly_serving

   # 인덱스를 재빌드한 경우 → --delete 필수
   bash rag_assembly/tools/windows/sync_to_windows.sh --rag --delete /mnt/d/assembly_serving

   # 코드만 고친 경우 (몇 초)
   bash rag_assembly/tools/windows/sync_to_windows.sh --code /mnt/d/assembly_serving
   ```

   `--delete` 를 빼면 LanceDB 조각 파일명이 통째로 바뀌는 재빌드 후 **옛 21GB가 대상에
   그대로 남는다.** 검색 결과가 틀리진 않지만(매니페스트가 가리키는 것만 읽는다)
   디스크를 먹고 다음 진단을 헷갈리게 한다.
4. `--paths` 로 7줄 OK 확인(§6).
5. Claude Code를 다시 켜고 `rag_stats` 의 청크 수가 WSL과 같은지 대조(§8-1).

인덱스를 재빌드했다면 `embed_config_version` 이 바뀌었을 수 있다. 그러면 **코드도 같이
동기화**해야 한다(`--code`) — 서빙 상수와 인덱스가 어긋나면 `rag_stats` 의 소스별 분포가
빈 채로 나온다.

---

## 11. 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| MCP가 connected 안 됨 | 먼저 `--paths`(§6)를 CLI로 돌려 본다. 여기서 죽으면 MCP 문제가 아니라 환경 문제다 |
| `analysis DB 파일이 없습니다` | `ASSEMBLY_ANALYSIS_DB_PATH` 또는 `ASSEMBLY_DATA_DIR` 오타. 메시지가 이름을 알려 준다 |
| `RAG 인덱스 미구축` | `RAG_DATA_DIR` 이 틀렸거나 인덱스가 덜 복사됨. `rag_data\bm25\manifest.json` 과 `rag_data\lance_db\chunks.lance\data\` 존재 확인 |
| `rag_search` 가 계속 "로딩 중" | BM25 첫 로드(콜드 30초+). 그 뒤에도 그러면 stderr 로그에서 로더 실패 확인. RAM 부족(5.3GB 필요)일 수 있다 |
| `rag_stats` 는 되는데 소스별 분포가 빔 | `manifest.sqlite` 누락, 또는 코드의 `EMBED_CONFIG_VERSION` 과 인덱스 버전 불일치 → 코드도 동기화(§10) |
| 결과 한글이 깨짐 | `PYTHONUTF8=1` 을 env에 넣고 재시작. **결과가 조용히 엉뚱해지는 유형의 사고**라 반드시 처리 |
| 서버가 조용히 죽음 / 로그 없음 | `pythonw.exe` 로 띄웠을 가능성. `python.exe` 로 바꾼다 |
| `torch.version.cuda` 가 `None` | CPU 휠이 깔림. `pip uninstall torch` 후 §4.3 재실행 |
| CUDA OOM / 드라이버 오류 | 코드가 자동으로 CPU 폴백한다(느려질 뿐). 고정하려면 env `RAG_EMBED_DEVICE=cpu` |
| `SSLCertVerificationError` | §4.5 truststore. 또는 §5 방법 A로 네트워크 자체를 회피 |
| 재동기화가 `Permission denied` | Windows쪽 MCP 서버가 파일을 잡고 있다. Claude Code 종료 후 재시도(§10-2) |
| `sqlite3.OperationalError: readonly database` | `manifest.sqlite` 에 읽기 전용 속성이 걸림 → `attrib -R D:\assembly_serving\rag_data\manifest.sqlite` |
| 디스크 부족 | §1. sync 스크립트가 복사 전에 여유 공간을 확인하고 막는다 |

stderr 로그는 Claude Code의 MCP 로그에서 본다. 서버는 기동 시 경로 진단 7줄,
`[mcp] RAG preimport done`, `[mcp] embed load guard registered`, BM25/warm-up 상태를
전부 stderr로 남기므로 어디까지 갔는지 바로 보인다.

---

## 12. 부록 A — 환경변수

**정본은 `duckdb_mcp_server.py` 모듈 docstring(§경로 환경변수)** 이다. 아래는 요약.

### 경로 (하나도 안 주면 저장소 기본 경로 = WSL과 완전히 동일한 동작)

| 변수 | 뜻 |
|---|---|
| `ASSEMBLY_DATA_DIR` | `data/` 루트. 아래 4개 DB의 기본 부모. `bills_kr/`·`news/` 하위 구조는 유지 |
| `ASSEMBLY_RAW_DB_PATH` | `assembly_raw.duckdb` (파일 단위, `ASSEMBLY_DATA_DIR` 보다 우선) |
| `ASSEMBLY_ANALYSIS_DB_PATH` | `assembly_analysis.duckdb` |
| `ASSEMBLY_NEWS_RAW_DB_PATH` | `news.duckdb` |
| `ASSEMBLY_NEWS_ANALYSIS_DB_PATH` | `news_analysis.duckdb` |
| `RAG_DATA_DIR` | RAG 인덱스 루트. `duckdb_mcp_server.py` 와 `rag_assembly/config.py` 가 **같은 변수를 같은 규칙으로** 읽는다(진단 경로와 실제 검색 경로가 갈라질 수 없다) |

값은 `~` 와 `$VAR`/`%VAR%` 확장 후 절대경로화된다. 상대경로는 프로세스 CWD 기준이라 쓰지 말 것.

### 동작

| 변수 | 기본 | 뜻 |
|---|---|---|
| `RAG_EMBED_DEVICE` | (자동) | `cuda` / `cpu` 강제. 미설정이면 CUDA 가용성으로 결정 |
| `MCP_EMBED_WARMUP` | `1` | 기동 시 임베딩 모델 선로드. `0` 이면 첫 검색이 몇 초 느려진다 |
| `MCP_BM25_LOAD_DELAY` | `2.0` | BM25 로더 시작 유예(초). 상한 60초로 clamp |
| `MCP_EMBED_SUBPROC` | `0` | `1` 이면 질의마다 격리 subprocess로 임베딩(느림). in-proc 실패 시 자동 폴백이 이미 있어 평소엔 불필요 |
| `PYTHONUTF8` | — | `1` 권장 (§7) |
| `HF_HOME` | `%USERPROFILE%\.cache\huggingface` | 모델 캐시 위치 |

## 부록 B — 이 폴더의 파일

| 파일 | 역할 |
|---|---|
| `SETUP_WINDOWS.md` | 이 문서 |
| `requirements-serving.txt` | 서빙 전용 의존성. 산정 근거·제외 목록·torch 설치법 포함 |
| `mcp.windows.json` | Windows Claude Code `.mcp.json` 템플릿 |
| `sync_to_windows.sh` | WSL에서 실행하는 복사·검증 스크립트 (`--help`) |

---

*작성 2026-07-27. 인덱스 기준: `v3_arctic_ko_1024_fp16_20260726`, 3,264,019 청크.*
