# Handoff: BERTopic + Kiwi 형태소 분석기 통합 시 hang 문제

작성: 2026-05-25 02:00 KST
저자 세션: Claude Code (Opus 4.7) — 진단 진행 중

---

## 문제 한 줄 요약

`subtopic_bertopic.py`에 **kiwipiepy** 명사 추출 토크나이저를 도입한 후, BGE-M3 SBERT 임베딩 단계에서 작업이 **CPU fallback + hang 패턴**으로 멈춤. Kiwi 도입 전(Phase A)에는 동일한 BGE-M3 임베딩이 정상 GPU 속도(2~3분/2,720건)로 작동했음.

진단 마지막 시도 (`device='cuda'` 명시 + `show_progress_bar=True` 추가) 시점에는 EN 543건 임베딩이 3초만에 정상 완료됨 → KO 결과 대기 중에 사용자가 handoff 요청.

---

## 환경

- **OS**: Windows 11 Pro
- **Python**: venv = `C:\Users\jaeky\Documents\GitHub\assembly_data-1\venv\Scripts\python.exe` (실제 인터프리터 = Microsoft Store Python 3.13)
- **GPU**: NVIDIA RTX 4060 8GB (CUDA 13.2, torch 2.11.0+cu128, sentence-transformers 5.4.1)
- **DuckDB**: news.duckdb 1.18GB at `data/news/news.duckdb`
- **저장소**: `c:\Users\jaeky\Documents\GitHub\assembly_data-1` (main branch)

---

## 핵심 파일

- [analyze/subtopic_bertopic.py](analyze/subtopic_bertopic.py) — 본 문제의 메인 스크립트
- [analyze/news_cleaning.py](analyze/news_cleaning.py) — `STRICT_WHERE` + `CLEANED_CONTENT_SQL` 모듈
- `data/news/news.duckdb` — `news_articles` 157K rows + `news_classifications` 81,888건 분류 완료
- `data/analysis/subtopics_bertopic.json` — Phase A 결과 저장된 위치 (Phase B 검증 미완료)
- 계획서: `C:\Users\jaeky\.claude\plans\agile-petting-snowglobe.md` (Phase B 계획)
- 학습 노트: `BERTOPIC_NOTES.md`, `BERTOPIC_PIPELINE.md` (repo root)

---

## 컨텍스트 (작업 흐름)

### Phase A (2026-05-24~25, 완료)

- `subtopic_bertopic.py`에 한국 뉴스(news.duckdb) 로더 추가
- 임베딩 모델 MiniLM → **BAAI/bge-m3** (1024dim 다국어 SOTA) 교체
- HDBSCAN 파라미터 튜닝: `mcs = max(8, n//200)`, `min_samples=1`, `cluster_selection_method='leaf'`
- `MERGE_THRESHOLD` 0.62 → 0.68 (BGE-M3 재캘리브)
- `news_cleaning.CLEANED_CONTENT_SQL` 적용 — YTN AI 앵커 footer, MBC `(AI학습 포함)` boilerplate 제거
- **Phase A 결과 정상**: 책임/윤리AI 3,263건 → 37토픽, 산업정책 36,975건 → 30토픽, 공익 11,347건 → 39토픽 등 전체 10 attr 완주 (~80분)

### Phase B (2026-05-25 진행 중, hang)

- **kiwipiepy 0.23.1** import + `_kiwi = Kiwi()` 모듈 전역 인스턴스
- 사용자 사전 19개 등록 (`인공지능`, `딥페이크`, `자율주행` 등 — Kiwi 기본 사전이 분리하는 어휘만 데이터 기반 선별)
- `ko_tokenizer(text)` 함수: `NNG`/`NNP`/`SL` 태그 추출 + trailing punctuation 정리
- `CountVectorizer`의 `tokenizer=ko_tokenizer`로 KO/mixed 분기 (EN은 기존 token_pattern 유지)

```python
# 모듈 전역 (subtopic_bertopic.py:38 부근)
_kiwi = Kiwi()
KIWI_USER_WORDS = [
    "인공지능", "딥페이크", "자율주행", "데이터센터", "빅테크",
    "안면인식", "생성형", "머신러닝", "딥러닝",
    "딥페이크 성범죄", "디지털 성범죄", "가짜뉴스",
    "전세사기", "유심해킹", "개인정보",
    "챗GPT", "SK하이닉스", "G7", "정상회담",
]
for _w in KIWI_USER_WORDS:
    _kiwi.add_user_word(_w, "NNP", 10.0)

KO_NOUN_TAGS = {"NNG", "NNP", "SL"}
_PUNCT_TAIL = re.compile(r"[^A-Za-z가-힣\d]+$")

def ko_tokenizer(text):
    out = []
    for t in _kiwi.tokenize(text):
        if t.tag not in KO_NOUN_TAGS:
            continue
        form = _PUNCT_TAIL.sub("", t.form)
        if len(form) >= 2:
            out.append(form)
    return out

# run_bertopic_lang 안 (subtopic_bertopic.py:264 부근)
if lang_tag == "en":
    vectorizer_model = CountVectorizer(
        stop_words=stopwords, min_df=2, ngram_range=(1, 1),  # 진단 중 ngram(1,1)로 임시 변경
        token_pattern=r'(?u)\b[a-zA-Z가-힣]{2,}\b')
else:  # "ko" or "mixed"
    vectorizer_model = CountVectorizer(
        tokenizer=ko_tokenizer, lowercase=False,
        stop_words=stopwords, min_df=2, ngram_range=(1, 1))
```

---

## 증상 패턴

검증 attr: **책임/윤리AI** (KO 2,720건 + EN 543건 = 3,263건)

```
... 진행 ...
device: cuda:0, cuda_available: True
[책임/윤리AI] 총 3263건 — EN:543 / KO:2720
    [en] 543건 임베딩...
    → 토픽 2개, 아웃라이어 112건 (21%)
    [ko] 2720건 임베딩...
    ←─ ★ 여기서 멈춤. 5~7분 동안 진전 없음
```

GPU 활용도 0~10% sparse, VRAM 6GB 유지. CPU 시간은 누적 (PID별 CPU 400~470초). 마치 CPU에서 임베딩하는 것 같은 패턴이지만 단독 테스트에선 GPU 정상 작동.

---

## 진단 시도 이력

### A. PID 3300 zombie 프로세스 (다른 Claude 세션 작업)
- 발견: `Get-CimInstance Win32_Process | ProcessId=3300` = `C:/claude_scratch/20260525_descriptive_stats_collect.py` (다른 세션이 news.duckdb 열어둠)
- 종료 후 재시도 → **여전히 hang**
- DB read-only 연결은 0.02초 성공 (DB 락 자체는 hang 원인 아님)

### B. KeyBERTInspired 제거 (representation_model)
- 가설: KeyBERTInspired가 SBERT 재호출 → 다음 단계에서 stuck
- KeyBERTInspired 주석 처리 후 재시도 → **여전히 같은 패턴 hang**
- 단, KeyBERTInspired 제거하면 키워드 품질 떨어짐 (`to, of, in, is, on` 같은 stopword만 남음 — c-TF-IDF가 좋은 키워드 골라주지 못함). KeyBERTInspired는 hang 원인 아니지만 품질에 필수.

### C. ngram_range (1,2) → (1,1)
- 가설: vocab 71,766 폭증이 후속 단계 부담
- 변경 후 재시도 → **여전히 hang**
- vocab 크기는 hang 원인 아님

### D. Kiwi + CountVectorizer 단독 테스트 (BERTopic 없이)
- 실제 KO 본문 2,720건에 직접 호출
- 결과: ko_tokenizer 33초, CountVectorizer.fit_transform 32초 — **정상**
- 즉 Kiwi 자체 또는 CountVectorizer 자체는 문제 없음

### E. `device='cuda'` 명시 + `show_progress_bar=True`
- `sbert.encode(..., device='cuda', show_progress_bar=True)` 명시 추가
- 진행 신호 추가: 임베딩 시작·완료 print
- **EN 543건 임베딩 3.0초만에 정상 완료** (10 it/s, cuda:0)
- KO 결과 대기 중 → handoff 시점

### F. SentenceTransformer 단독 테스트 (BERTopic 외부)
```python
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("BAAI/bge-m3")  # device 명시 없음
# Result: m.device = cuda:0, 100 docs 임베딩 0.29초, VRAM 2.12GB — 정상
```
즉 SBERT 자체는 자동으로 cuda 잘 인식. **subtopic_bertopic.py 컨텍스트에서만 문제**.

---

## 의심 가설 공간

1. **SBERT.encode가 BERTopic 컨텍스트에서 CPU로 fallback**
   - `device='cuda'` 명시 안 하면 자동 감지 실패하는 케이스
   - 진단 시도 E의 결과로 EN은 해결된 듯 (3초 완료) — 단 KO 결과는 미확인

2. **kiwipiepy import 부작용**
   - `from kiwipiepy import Kiwi` 또는 `_kiwi = Kiwi()` 인스턴스 생성이 다른 라이브러리(torch CUDA) 초기화에 영향?
   - 가설 검증 안 됨

3. **`_kiwi = Kiwi()` 모듈 전역 + multi-thread/multi-process**
   - sklearn CountVectorizer는 single-thread지만, BERTopic 내부에서 어떤 multiprocessing 호출 시 Kiwi 인스턴스가 pickle 불가능 → spawn 실패?
   - kiwipiepy 0.23.1의 Kiwi 객체가 fork-safe인지 미확인

4. **PyTorch CUDA stream 상태**
   - 여러 작업 죽이고 GPU 메모리 1.8GB→6.3GB→1.6GB 반복 → CUDA context 일관성 깨짐 가능

5. **단순히 zombie 프로세스 누적**
   - 시도하다 보면 좀비 python3.13 프로세스가 VRAM을 잡고 있어 새 작업이 GPU 못 쓰고 CPU fallback
   - 실제로 진단 중 zombie 프로세스 2~3차례 발견 (PID 53680, 51476, 27136 등)

---

## 재현 명령어

```powershell
# 1. 진행 중 백그라운드 작업/zombie 정리
Get-Process python3.13 -ErrorAction SilentlyContinue | Where-Object {$_.WorkingSet64 -gt 200MB} | ForEach-Object { Stop-Process -Id $_.Id -Force }
Start-Sleep 2

# 2. GPU 메모리 청소 확인
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader
# 기대: ~1.5GB used (시스템 GUI), ~6GB free

# 3. 단일 attr 검증 실행
venv\Scripts\python.exe analyze\subtopic_bertopic.py --attr "책임/윤리AI" --no-label 2>&1 | tee data\analysis\kiwi_diag.log

# 정상이면 ~7분에 완료, 결과 표 출력
# 비정상이면 [ko] 2720건 임베딩... 에서 5분+ hang
```

`--no-label`은 GPT 라벨링 스킵 (비용 절약). 

전체 10 attr 재실행은 `--attr` 옵션 제거.

---

## 관련 외부 자료

- kiwipiepy GitHub: https://github.com/bab2min/Kiwi
- kiwipiepy 0.23.1 changelog (multi-thread/process 관련 이슈 검색)
- sentence-transformers `encode(device=...)` 동작: https://www.sbert.net/docs/package_reference/SentenceTransformer.html
- BERTopic + Kiwi 통합 사례 (KoBERTopic): https://github.com/ukairia777/KoBERTopic
- BERTopic FAQ — Custom tokenizer: https://maartengr.github.io/BERTopic/faq.html

---

## 권장 다음 액션 (다른 에이전트용)

1. **현재 실행 중인 task `brf7lywr0` 결과 먼저 확인** — `device='cuda'` 명시 + progress_bar 추가 버전. EN은 3초 완료. KO 결과가 정상 시간 안에 끝나는지 본 후 결정.
   - 로그 위치: `C:\Users\jaeky\AppData\Local\Temp\claude\c--Users-jaeky-Documents-GitHub-assembly-data-1\41905c28-9223-4cb8-b694-9cc94fdcfaec\tasks\brf7lywr0.output`
   - 또는 사용자가 직접 실행한 `data/analysis/kiwi_step3_diag.log`

2. **만약 KO도 정상 완료** → 진단 시도 E (`device='cuda'` 명시)가 해결책. 전체 10 attr 재실행 (`--attr` 제거, ~100분).

3. **만약 KO에서 또 hang**:
   - kiwipiepy import를 main() 함수 안으로 이동 (모듈 전역 → 함수 지역) — torch CUDA 초기화 전 import 영향 차단
   - 또는 `multiprocessing.set_start_method('spawn')` 명시
   - 또는 SBERT.encode 호출 시 `convert_to_tensor=True` 추가 시도
   - 또는 fp16 시도: `sbert.half()` 후 encode

4. **최후 수단**: Kiwi 우회. 조사 suffix 정규식 trim 휴리스틱으로 대체 (BERTOPIC_NOTES.md §1.5 참조)
   ```python
   PARTICLES = ['으로서','으로써','에서','에게','한테','부터','까지',
                '으로','이라','라고','이라고','들이','들은','들을','들과',
                '은','는','이','가','을','를','의','에','와','과',
                '로','도','만','랑']
   PAT = re.compile(r'(' + '|'.join(sorted(PARTICLES, key=len, reverse=True)) + r')$')
   def strip_particle(token):
       return PAT.sub('', token) if len(token) > 2 else token
   def quick_tokenizer(text):
       return [strip_particle(w) for w in text.split() if len(w) >= 2]
   ```
   장점: 의존성 0, BERTopic 영향 0. 단점: Kiwi 정확도 손실.

---

## 사용자 작업 스타일 메모

- 빠른 진행 선호 ("쭉쭉")
- 대용량 작업 전 승인 게이트 (1.5h+ 작업은 부분 검증 후 진행)
- 한국어 응답 + 한국어 하이픈(-) 금지 (쉼표·괄호로 대체)
- venv 정본: `venv/Scripts/python.exe` (Microsoft Store global python 금지)
- 임시 파일: `C:\claude_scratch\` 단일 폴더

---

## 본 handoff 파일 위치

`BERTOPIC_KIWI_HANDOFF.md` (repo root). 2026-05-25 commit `9b8d369`에서 `C:\claude_scratch\`에서 이동.

코드 변경은 [analyze/subtopic_bertopic.py](analyze/subtopic_bertopic.py)에 commit `582df98`로 누적되어 있음. 진단용 임시 변경(`device='cuda'`, `show_progress_bar=True`, 임베딩 시작/완료 print)도 포함.
