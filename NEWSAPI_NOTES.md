# NewsAPI.ai (Event Registry) 활용 노트

본 문서는 본 연구의 해외 언론 데이터 확장을 위한 NewsAPI.ai 활용 시 참조용 기록.
2026-04-25 시점 실측 검증 결과.

---

## 1. 기본 정보

- 서비스명: NewsAPI.ai (Event Registry)
- 도메인: newsapi.ai (별칭: eventregistry.org)
- 기본 엔드포인트: `https://eventregistry.org/api/v1/article/getArticles`
- 인증: `apiKey` 파라미터 (본문 또는 쿼리스트링)
- 아카이브 시작: 2014년 (공식 명시)

---

## 2. 본문 제공 여부

**본문 전문 제공 (`body` 필드)**. 가디언·NYT 코퍼스 대비 동등하거나 더 긴 본문 확보 가능.

| 언어 | 본문 평균 길이 | 비고 |
|---|---|---|
| 영어 | 약 5,500자 | NonZero 같은 블로그는 10,000자 이상 |
| 한국어 | 약 1,338자 (중앙값 1,145자) | 최대 8,281자, 100% body 보유 |

---

## 3. 토큰 과금 모델 (실측)

응답 헤더로 매 요청 비용 확인 가능:

```
X-RateLimit-Limit: 2000        ← Free 플랜 총 토큰
X-RateLimit-Remaining: 1985    ← 남은 토큰
req-tokens: 1.000              ← 이번 요청 비용
req-archive: 0                 ← 1=archive, 0=recent
req-years: 0                   ← archive 시 검색한 연도 수
req-article-search: 1          ← 검색 횟수
```

### 비용 규칙

| 쿼리 유형 | 비용 |
|---|---|
| 최근 30일 검색 | **1 토큰 / 페이지 (최대 100건)** |
| 1 페이지당 1~100건 | **무관, 항상 1 토큰** |
| 역사적 검색 (archive) | **5 토큰 / 검색 연도** (공식 문서) |
| 10년치 검색 (1 페이지) | 50 토큰 |
| `sourceAggr` 등 집계 쿼리 | **5 토큰** (실측) |

### 환산 비용 예시

| 시나리오 | 페이지 수 | 토큰 |
|---|---|---|
| AI 영어 기사 최근 30일 전수 (≈11만 건) | 1,114 | 1,114 |
| AI 한국어 기사 최근 30일 전수 (≈4.9만 건) | 489 | 489 |
| AI 영어 기사 10년치 (≈50만 건 추정) | 5,000 | **250,000** |

---

## 4. 플랜 선택 가이드

| 플랜 | 월 토큰 | 월 비용 | 본 연구 적합 시나리오 |
|---|---|---|---|
| Free | 2,000 | $0 | 파일럿 (최근 30일 한정) |
| 5K | 5,000 | $90 | 본문 분석 prototype |
| 50K | 50,000 | 추정 $400~ | 10년치 한국어 + 영어 본격 수집 |
| 250K | 250,000 | 추정 $1,000~ | 다국어 풀 코퍼스 |

---

## 5. 한국어 검색 — 핵심 함정

### 작동 안 함
```python
{'$query': {'keyword': '인공지능', 'lang': 'kor'}}  # 결과 0건
{'$query': {'keyword': '인공지능'}}                  # 결과 0건
```

### 작동
```python
{'$query': {
    'conceptUri': 'http://en.wikipedia.org/wiki/Artificial_intelligence',
    'lang': 'kor'
}}  # 48,821건
```

**규칙**: NewsAPI의 `keyword` 파라미터는 한국어 텍스트를 매칭하지 못함. 반드시 **Wikipedia URI 형식의 `conceptUri`**를 사용해야 한다.

### 자주 쓸 conceptUri 후보 (예시)
- `http://en.wikipedia.org/wiki/Artificial_intelligence`
- `http://en.wikipedia.org/wiki/Deepfake`
- `http://en.wikipedia.org/wiki/Machine_learning`
- `http://en.wikipedia.org/wiki/Algorithmic_bias`
- `http://en.wikipedia.org/wiki/Generative_artificial_intelligence`
- `http://en.wikipedia.org/wiki/Large_language_model`

### 노이즈 주의
concept 매칭은 본문에 AI가 어디든 언급되면 잡히므로 **AI와 무관한 일반 기사 일부 포함**. 본 연구의 기존 2단계 필터(키워드 빈도 ≥3 + GPT 판별) 적용 필요.

---

## 6. 한국 매체 커버리지 (실측, 최근 30일 AI 기사)

### 확보 가능

| 매체 | URI | 건수 |
|---|---|---|
| 매일경제 | mk.co.kr | 1,813 |
| 경향신문 | khan.co.kr | 536 |
| 문화일보 | munhwa.com | 374 |
| 조선일보 | chosun.com | 334 |
| KBS World (영문) | world.kbs.co.kr | 238 |
| 동아일보 | donga.com | 213 |
| 국민일보 | kmib.co.kr | 176 |
| 서울신문 | seoul.co.kr | 158 |
| MBN | mbn.co.kr | 144 |
| 채널A | ichannela.com | 44 |
| 한겨레 영문 | english.hani.co.kr | 18 |
| 연합뉴스 / 연합뉴스TV | yna.co.kr / yonhapnewstv.co.kr | 다수 (샘플 31) |
| YTN | ytn.co.kr | 다수 (샘플 5) |
| 디지털데일리 | ddaily.co.kr | 다수 |
| 파이낸셜뉴스 | fnnews.com | 다수 |
| 서울경제 | sedaily.com | 다수 |

### 확보 불가 (디렉토리에 등록만 되고 크롤링 안 됨)

- 중앙일보 (joongang.co.kr, joins.com 모두)
- 한겨레 한국어판 (hani.co.kr)
- 한국경제 (hankyung.com)
- 머니투데이 (mt.co.kr)
- 이데일리 (edaily.co.kr)
- 한국일보 (hankookilbo.com)
- 세계일보 (segye.com)
- KBS 한국어 뉴스 (news.kbs.co.kr)
- MBC, SBS, JTBC, TV조선 모두

**한계**: 진보 일간지(중앙·한겨레)와 지상파 3사가 부재. BIGKINDS 병행 필요.

---

## 7. 글로벌 매체 커버리지 (실측, 최근 30일 AI 기사)

### Tier 1 — 영미 주요 매체

| 매체 | URI | 건수 |
|---|---|---|
| Bloomberg | bloomberg.com | 1,449 |
| Forbes | forbes.com | 865 |
| Reuters | reuters.com | 597 |
| Financial Times | ft.com | 570 |
| WSJ | wsj.com | 523 |
| The Guardian | theguardian.com | 434 |
| TechCrunch | techcrunch.com | 351 |
| BBC | bbc.com / bbc.co.uk | 308 / 309 |
| NYT | nytimes.com | 110 |
| AP News | apnews.com | 97 |
| Washington Post | washingtonpost.com | 19 |
| Wired | wired.com | 132 |

### Tier 2 — 국제 영문판

| 매체 | URI | 건수 |
|---|---|---|
| SCMP (홍콩) | scmp.com | 208 |
| Korea Times | koreatimes.co.kr | 252 |
| Korea Herald | koreaherald.com | 236 |
| France 24 | france24.com | 161 |
| Japan Times | japantimes.co.jp | 66 |
| DW | dw.com | 56 |
| RT | rt.com | 53 |
| Al Jazeera | aljazeera.com | 34 |
| Le Monde | lemonde.fr | 21 |

### 부재 (라이선스 없음)

- CNN (cnn.com)
- Spiegel (spiegel.de)
- El Pais (elpais.com)
- Xinhua (xinhuanet.com)

---

## 8. 응답 데이터 구조

### 표준 필드 (요청 없이도 반환)

| 필드 | 타입 | 설명 |
|---|---|---|
| `uri` | str | NewsAPI 내부 ID |
| `lang` | str | 언어 코드 (eng, kor 등) |
| `isDuplicate` | bool | 같은 사건 다른 매체 중복 여부 |
| `date`, `time`, `dateTime` | str | 인덱싱 시각 |
| `dateTimePub` | str | **원기사 발행 시각** |
| `dataType` | str | news / blog / pr 등 |
| `sim` | float | 같은 사건 다른 기사와 유사도 |
| `url` | str | 원기사 URL |
| `title` | str | 제목 |
| **`body`** | str | **본문 전문** |
| `source` | dict | `{uri, dataType, title}` |
| `authors` | list | `[{uri, name, type, isAgency}]` |
| `image` | str | 대표 이미지 URL |
| `eventUri` | str | **같은 사건 클러스터 ID** |
| `sentiment` | float | 감정 -1~+1 |
| `wgt` | int | 정렬 가중치 |
| `relevance` | int | 쿼리 관련도 |

### 부가 메타 (`includeArticleConcepts`, `includeArticleCategories` 옵션)

- 추출된 개체명·카테고리·이벤트 URI 등

---

## 9. 가디언·NYT 데이터와의 비교

| 항목 | 가디언 (현재) | NYT (현재) | NewsAPI.ai |
|---|---|---|---|
| 본문 | `body` 전문 (400건만) | **없음** (lead_paragraph까지) | **`body` 전문** |
| 발행 시각 | `pub_date` | `pub_date` | `dateTimePub` |
| 매체명 | URL에서 추출 | `desk` | `source.title` |
| 저자 | 없음 | 없음 | `authors[]` |
| **사건 클러스터링** | 없음 | 없음 | **`eventUri`** |
| **감정 점수** | 없음 | 없음 | **`sentiment`** |
| **중복 검출** | 별도 처리 | 별도 처리 | **`isDuplicate`** |

---

## 10. API 키 (Free 테스트용, 잔여 한정)

```
KEY = '37b9f0df-3981-4efc-a99a-ff32816c99a3'
```

2026-04-25 기준 약 1,920 토큰 잔여. 추가 테스트 시 잔여량 모니터링.

---

## 11. 표준 호출 템플릿 (Python)

```python
import urllib.request, json

KEY = '...'

def fetch(payload):
    req = urllib.request.Request(
        'https://eventregistry.org/api/v1/article/getArticles',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8'}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        cost = r.headers.get('req-tokens')
        remain = r.headers.get('X-RateLimit-Remaining')
        data = json.loads(r.read())
    return data, cost, remain

# 한국어 AI 기사 100건
payload = {
    'query': {'$query': {
        'conceptUri': 'http://en.wikipedia.org/wiki/Artificial_intelligence',
        'lang': 'kor'
    }},
    'resultType': 'articles',
    'articlesSortBy': 'date',
    'articlesCount': 100,
    'apiKey': KEY
}
```

### 역사적 검색 (유료 플랜)

```python
'query': {'$query': {
    'conceptUri': 'http://en.wikipedia.org/wiki/Artificial_intelligence',
    'lang': 'kor',
    'dateStart': '2024-01-01',
    'dateEnd': '2024-12-31'
}}
```

---

## 12. 본 연구 적용 권고

### 단기 (Free 플랜으로 가능)

- 최근 30일 한국어 + 영어 AI 기사 본문 코퍼스 구축 (약 16만 건)
- 가디언 본문(400건) 한계 보강용 영어 본문 다량 확보

### 중기 (5K Plan, $90)

- 특정 매체·기간 타겟 수집 (예: 매일경제 1년치)
- 본 연구의 한국 측 본문 분석 신뢰도 보강

### 장기 (50K Plan 이상)

- 2016~2026 10년치 다국가 풀 코퍼스 구축
- BIGKINDS 부재 매체(중앙일보 등) 외 매체 보완

---

## 13. 저작권 관련 메모

- NewsAPI.ai는 인덱싱·중계 사업자. 원 매체 저작권은 별도로 살아있음
- 한국에는 TDM 전용 면책 없음 (계류 중). §35-5 일반 공정이용 + §28 인용에 의존
- 분석 결과(통계·도표·짧은 인용) 공표는 안전 영역
- 본문 원문 외부 공개·데이터셋 배포는 위험
- 비영리 학술 목적이면 실무상 안전하나 출판 단계 법률 자문 권장
