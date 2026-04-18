# BERT WSI 분석 브리핑: "윤리" 사용 맥락 분석

## 배경 및 목적

### 연구 맥락
현재 AI 정책 뉴스 기사(Guardian, NYT, Naver) 23,181건을 Carvão et al. (2025)의 10개 정책 속성 프레임워크로 분류한 뒤, BERTopic으로 소주제를 추출하는 연구를 진행 중이다.

10개 정책 속성 중 하나인 **"책임/윤리AI"** 카테고리(2,092건)를 분석하는 과정에서 다음 문제가 제기되었다:

> "윤리(ethics/倫理)라는 단어가 굉장히 모호하게 쓰이고 광범위한 맥락에서 사용되는데, 앞뒤 맥락을 이용해서 이 단어가 어떤 맥락과 결부되는지 분석할 수 있는가?"

BERTopic 결과에서 가장 큰 클러스터(237~331건)가 `future, we need, machine learning, society` 같은 범용 키워드로 채워진 **"AI 윤리 일반 담론"** 클러스터로 나타났으며, 이는 "윤리"라는 단어 자체의 다의성·광범위성 때문으로 판단되었다.

### 이 분석의 목적
"윤리"(영어: ethics, ethical)라는 단어가 뉴스 맥락에서 **몇 가지 의미 사용역(sense)으로 쓰이는지**, 각 사용역이 **어떤 맥락·주제와 결부되는지**를 BERT Word Sense Induction(WSI)으로 귀납적으로 도출한다.

이 분석은 현재의 BERTopic 소주제 추출 파이프라인과는 **독립적인 별개 분석**이다.

---

## 가용 데이터

### 원본 기사 파일
모두 `c:\Users\jaeky\Documents\GitHub\assembly_data-1\data\` 에 위치한다.

| 파일명 | 내용 | 주요 필드 |
|--------|------|-----------|
| `guardian_articles_raw.json` | Guardian 기사 원본 | `id`, `title`, `webTitle`, `trail_text`, `fields.trailText`, `url` |
| `nyt_articles_raw.json` | NYT 기사 원본 | `url`, `title`, `headline.main`, `abstract`, `lead_paragraph` |
| `naver_articles_raw.json` | Naver 기사 원본 | `link`, `originallink`, `title`, `description` |

### 분류 결과 파일
| 파일명 | 내용 | 주요 필드 |
|--------|------|-----------|
| `news_guardian_classified.json` | Guardian 기사 정책 속성 분류 결과 | `article_id`, `primary`, `secondary` |
| `news_nyt_classified.json` | NYT 기사 정책 속성 분류 결과 | `article_id`, `primary`, `secondary` |
| `news_naver_classified.json` | Naver 기사 정책 속성 분류 결과 | `article_id`, `primary`, `secondary` |

### 분류 속성값
Guardian/NYT의 `primary` 값 (영어):
- `Responsible and ethical AI`, `Safety`, `Industrial policy`, `Public interest`, `National security`, `Elections`, `Market efficiency and power concentration (antitrust)`, `Labor`, `Copyright`, `International collaboration`

Naver의 `primary` 값 (한국어):
- `책임/윤리AI`, `AI안전`, `산업정책`, `공익/소비자보호`, `국가안보`, `선거/민주주의`, `시장경쟁/독과점`, `노동/고용`, `저작권/지식재산`, `국제협력`

### 기사 텍스트 구성 방법
```python
import html as html_mod
import re

def clean_text(t):
    t = html_mod.unescape(t)
    t = re.sub(r'<[^>]+>', '', t)
    return re.sub(r'\s+', ' ', t).strip()

# Guardian: title + trail_text
text = clean_text(f"{title}. {trail_text}")

# NYT: title + abstract
text = clean_text(f"{title}. {abstract}")

# Naver: title + description
text = clean_text(f"{title}. {description}")
```

### ID 매핑 주의사항
- Guardian: `article_id`는 경로형(`technology/2023/jan/01/article`) 또는 전체 URL(`https://www.theguardian.com/...`) 두 가지 형태가 혼재함. 원본 raw JSON의 `id` 필드(경로형)와 전체 URL 둘 다 매핑해야 함.
- NYT: `article_id`는 `url`, `web_url`, `_id`, `uri` 중 하나. raw JSON에서 여러 키를 시도해야 함.
- Naver: `article_id`는 `link` 또는 `originallink`.

### 규모
- 전체: 23,181건 (Guardian 7,877 + NYT 2,762 + Naver 12,542)
- "책임/윤리AI" 카테고리: 2,092건 (Guardian 957 + NYT 360 + Naver 775)
- 전체 데이터에서 "윤리/ethics/ethical" 포함 기사 수는 별도 집계 필요

---

## 방법론: BERT Word Sense Induction (WSI)

### 핵심 원리
BERT 계열 모델은 **동일한 단어라도 문장 맥락에 따라 다른 벡터를 생성**한다.

```
"AI 면접에서 윤리 문제가 불거지고 있다"   → 윤리 벡터: [0.23, -0.41, 0.87, ...]
"윤리교육을 강화해야 한다"                 → 윤리 벡터: [0.71,  0.12, 0.34, ...]
"기업 윤리경영을 선포했다"                 → 윤리 벡터: [0.45,  0.33, 0.21, ...]
```

이 벡터들을 클러스터링하면 "윤리"의 의미 사용역이 몇 가지인지, 각 사용역의 특징이 무엇인지 귀납적으로 도출된다.

### 구현 단계

#### 1단계: 대상 단어 등장 문장 추출
```python
# 한국어
target_words_ko = ['윤리', '윤리적', '윤리성']

# 영어
target_words_en = ['ethics', 'ethical', 'ethically', 'unethical']
```

기사 텍스트를 문장 단위로 분리한 뒤 대상 단어가 포함된 문장만 추출한다. 문장 분리는 한국어의 경우 `kss` 또는 `.` 기반 간단 분리, 영어는 `nltk.sent_tokenize` 사용.

#### 2단계: BERT 컨텍스트 벡터 추출
```python
from transformers import AutoTokenizer, AutoModel
import torch

# 한국어/영어 혼합이면 다국어 모델 권장
model_name = "klue/bert-base"          # 한국어 전용 (Naver에 적합)
# 또는
model_name = "bert-base-multilingual-cased"   # 다국어 (전체 코퍼스에 적합)
# 또는
model_name = "snunlp/KR-ELECTRA-discriminator"  # 한국어 성능 우수

tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoModel.from_pretrained(model_name)

def get_target_word_embedding(sentence, target_word, tokenizer, model):
    """문장에서 target_word 토큰의 컨텍스트 벡터 반환."""
    inputs = tokenizer(sentence, return_tensors="pt", truncation=True, max_length=512)
    
    # 대상 단어의 토큰 위치 찾기
    tokens = tokenizer.tokenize(sentence)
    target_positions = [i for i, t in enumerate(tokens) 
                        if target_word in tokenizer.convert_tokens_to_string([t])]
    
    with torch.no_grad():
        outputs = model(**inputs)
    
    hidden_states = outputs.last_hidden_state[0]  # (seq_len, hidden_dim)
    
    if not target_positions:
        return None
    
    # 대상 단어에 해당하는 토큰들의 평균 벡터 (subword 처리)
    target_vec = hidden_states[target_positions].mean(dim=0)
    return target_vec.numpy()
```

#### 3단계: 벡터 차원 축소 + 클러스터링
```python
from umap import UMAP
from hdbscan import HDBSCAN
import numpy as np

embeddings = np.stack(all_target_embeddings)  # (n_occurrences, hidden_dim)

# UMAP으로 2D 축소 (시각화용) + 5D 축소 (클러스터링용)
umap_2d = UMAP(n_neighbors=15, n_components=2, metric='cosine', random_state=42)
umap_5d = UMAP(n_neighbors=15, n_components=5, metric='cosine', random_state=42)

coords_2d   = umap_2d.fit_transform(embeddings)
coords_clus = umap_5d.fit_transform(embeddings)

# HDBSCAN 클러스터링
clusterer = HDBSCAN(
    min_cluster_size=20,   # 최소 클러스터 크기 (데이터 규모에 따라 조정)
    min_samples=5,
    metric='euclidean',
)
labels = clusterer.fit_predict(coords_clus)

n_senses = len(set(labels)) - (1 if -1 in labels else 0)
print(f"발견된 의미 사용역: {n_senses}개")
```

#### 4단계: 각 클러스터 해석
클러스터별로:
- 대표 문장 추출 (센트로이드 최근접 문장 10개)
- 주변 단어 빈도 분석 (KWIC ±3 창)
- 출처 분포 (Guardian/NYT/Naver 비율)
- 정책 속성 분포 (어떤 카테고리 기사들이 많은지)

```python
for cluster_id in range(n_senses):
    cluster_sentences = [sentences[i] for i, l in enumerate(labels) if l == cluster_id]
    cluster_sources   = [sources[i]   for i, l in enumerate(labels) if l == cluster_id]
    cluster_attrs     = [attrs[i]     for i, l in enumerate(labels) if l == cluster_id]
    
    print(f"\n=== 클러스터 {cluster_id} ({len(cluster_sentences)}개 문장) ===")
    print("대표 문장:")
    for s in cluster_sentences[:10]:
        print(f"  {s[:120]}")
    print(f"출처: {Counter(cluster_sources)}")
    print(f"정책 속성: {Counter(cluster_attrs).most_common(3)}")
```

#### 5단계: GPT 라벨링
클러스터별 대표 문장 10개를 GPT에 전달해서 이 사용역의 이름을 붙인다.

```python
prompt = f"""다음은 뉴스 기사에서 "윤리" 라는 단어가 등장하는 문장들입니다.
이 문장들에서 "윤리"가 어떤 의미·맥락으로 사용되었는지 3-5 단어로 레이블을 붙여주세요.

문장들:
{chr(10).join(f'{i+1}. {s}' for i, s in enumerate(rep_sentences))}

응답 형식 (JSON): {{"label_ko": "한국어 레이블", "label_en": "English label", "description": "한 문장 설명"}}"""
```

---

## 분석 범위 제안

### 옵션 A: 전체 코퍼스 (23,181건)
"윤리/ethics/ethical"가 등장하는 모든 기사의 모든 문장을 대상으로 WSI 수행.
규모가 크므로 GPU 필요 (RTX 4060 이상 사용 가능).

### 옵션 B: "책임/윤리AI" 카테고리만 (2,092건)
연구의 직접적 관심 대상. 더 집중적인 분석 가능.

### 옵션 C: 속성별 비교 (권장)
각 정책 속성 카테고리 내에서 "윤리"가 어떻게 쓰이는지 비교하면
"산업정책 기사에서의 윤리" vs "AI안전 기사에서의 윤리" 차이를 볼 수 있어
정책 프레이밍 연구에 가장 유의미한 결과를 줄 수 있다.

---

## 환경 정보

```
OS: Windows 11
GPU: NVIDIA RTX 4060 (CUDA 사용 가능)
Python: 3.x
주요 패키지: transformers, torch, umap-learn, hdbscan, sentence-transformers
작업 디렉토리: c:\Users\jaeky\Documents\GitHub\assembly_data-1\
데이터 디렉토리: c:\Users\jaeky\Documents\GitHub\assembly_data-1\data\
```

패키지 설치 (미설치 시):
```bash
pip install transformers torch umap-learn hdbscan kss
```

---

## 출력 형식 제안

```json
{
  "target_word": "윤리",
  "language": "ko",
  "total_occurrences": 1842,
  "n_senses": 5,
  "senses": [
    {
      "sense_id": 0,
      "label_ko": "AI 규제·정책 윤리",
      "label_en": "AI Regulatory Ethics",
      "description": "AI 규제, 기준, 법안 맥락에서 사용되는 규범적 윤리",
      "count": 612,
      "source_dist": {"guardian": 120, "nyt": 89, "naver": 403},
      "policy_attr_dist": {"책임/윤리AI": 280, "AI안전": 150, "산업정책": 95, ...},
      "representative_sentences": ["...", "...", "..."]
    },
    ...
  ]
}
```

결과 저장 경로: `data/wsi_ethics_ko.json`, `data/wsi_ethics_en.json`
