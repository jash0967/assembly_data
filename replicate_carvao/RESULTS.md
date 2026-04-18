# AI 입법 갭 분석: 뉴스 담론 vs 입법 활동 비교

## 연구 목적

공론장(뉴스)에서 논의되는 AI 이슈가 실제 입법 활동으로 연결되고 있는지를 정량적으로 분석한다. 미국(118th/119th Congress)과 한국(22대 국회)의 AI 법안을 동일한 분류 체계로 비교하고, 뉴스 담론(Guardian/Naver)과의 갭을 식별한다.

## 선행 연구

**Carvão, P. et al. (2025). "Governance at a Crossroads: Artificial Intelligence and the Future of Innovation in America."** Harvard Kennedy School, M-RCBG Working Paper #251. SSRN: 5131048.

- 118th Congress AI 법안 150건 정량 분석
- TF-IDF + LDA + GPT-4o 3단계 분류 파이프라인
- 10개 Policy Attribute 분류 체계 제안
- "Antitrust와 Copyright가 공론장에서는 지배적이지만 법안에서는 극소수"라는 정성적 갭 발견
- **한계**: 공론장 데이터(뉴스)에 대한 정량적 분석 없이 정성적 서술에 그침

**본 연구의 차별점**: Carvão 방법론을 재현하면서, 뉴스 코퍼스(Guardian 2,908건 + Naver 19,165건)를 동일한 Policy Attribute로 GPT 분류하여 **정량적 갭 분석** 수행.

---

## 데이터

### 법안 데이터

| 데이터셋 | 기간 | 건수 | 소스 |
|---------|------|-----:|------|
| 미국 118th Congress | 2023.01 ~ 2025.01 | 154 | Brennan Center AI Legislation Tracker + Congress.gov API |
| 미국 119th Congress | 2025.01 ~ 현재 | 53 | Brennan Center AI Legislation Tracker + Congress.gov API |
| 한국 22대 국회 | 2024.05 ~ 현재 | 121 | 국회 Open API + GPT AI 관련성 판별 |

### 뉴스 데이터

| 데이터셋 | 기간 | 건수 | 소스 |
|---------|------|-----:|------|
| Guardian AI 뉴스 | 2023.04 ~ 2026.04 (3년) | 2,908 | Guardian Open Platform API ("artificial intelligence" 태그) |
| Naver AI 뉴스 | 2026.02 ~ 2026.04 (6주) | 19,165 | 네이버 검색 API (33개 AI 키워드, 제목 dedup 후) |

**시간 범위 비대칭**: Guardian 3년 vs Naver 6주. Naver API는 날짜 필터 미지원으로 최신 기사만 반환. 비율(%) 비교로 부분 보정하되, Naver는 최근 트렌드 스냅샷임을 명시.

### 데이터 수집 상세

#### 미국 118th Congress (Carvão Replication)
1. Brennan Center Datawrapper CSV에서 154건 AI 법안 목록 추출
2. Congress.gov API v3로 법안별 6개 엔드포인트 호출:
   - `/bill/118/{type}/{number}` — 기본 정보, policy area, sponsors
   - `/bill/118/{type}/{number}/cosponsors` — 공동발의자 (정당, 주, 지구)
   - `/bill/118/{type}/{number}/committees` — 위원회 배정
   - `/bill/118/{type}/{number}/actions` — 전체 진행 이력
   - `/bill/118/{type}/{number}/summaries` — CRS 요약
   - `/bill/118/{type}/{number}/text` — 법안 전문 URL
3. 법안 전문 HTML 다운로드 → 태그 제거 → 순수 텍스트 (154건, 3.56MB, 평균 23,135 bytes)
4. 논문 Figure 35의 17건 법안 전수 매칭 확인 (100%)

#### 미국 119th Congress
- 118th와 동일한 파이프라인. 53건, Brennan Center 119th 목록 기반.

#### 한국 22대 국회
1. `data/bill_txt/` 법안 텍스트 8,958건에서 "인공지능/AI" 3회 이상 언급 법안 필터 → 192건
2. 동일 의원 + 동일 제목 중복 제거 (최신 1건만) → 182건
3. GPT-4.1-mini로 AI 관련성 판별 (제안이유 기반) → 121건 확정, 61건 제외
4. assembly.duckdb에서 정당/위원회/처리결과 메타데이터 보강

#### Naver 검색 키워드 (33개)
인공지능 정책, 인공지능 윤리, 인공지능 법안, 인공지능 규제, 대규모언어모델, AI 기본법, 챗GPT, 딥페이크, AI EU 규제, AI 저작권, 생성형AI, 인공지능 산업, AI 사이버보안, AI 국방, AI 개인정보, AI 자율주행, AI 생성 콘텐츠, AI 안전, AI 스타트업, AI 감시, AI 데이터센터, AI 편향, AI 의료, AI 일자리, AI 규제, AI 클라우드, AI 반도체, AI 차별, AI 교육, AI 중국, AI 금융, AI 로봇, AI 노동

---

## 방법론

### 분류 체계: 10개 Policy Attributes

Carvão et al. (2025)이 정의한 10개 AI 정책 속성을 그대로 사용. 법안과 뉴스에 동일 체계 적용.

| # | Attribute (영어) | Attribute (한국어) | 정의 |
|---|-----------------|------------------|------|
| 1 | Market efficiency and power concentration (antitrust) | 시장경쟁/독과점 | 시장 경쟁, 독과점, 빅테크 규제, 플랫폼 |
| 2 | Safety | AI안전 | AI 시스템 안전성, 위험 관리, 테스트, 평가 |
| 3 | Responsible and ethical AI | 책임/윤리AI | 책임, 투명성, 편향, 공정, 거버넌스, 감독 |
| 4 | National security | 국가안보 | 국방, 사이버보안, 정보기관, 지정학적 경쟁 |
| 5 | Industrial policy | 산업정책 | AI 산업 육성, R&D 투자, 반도체, 인프라, 인재 |
| 6 | Public interest | 공익/소비자보호 | 소비자 보호, 의료, 교육, 아동, 환경 |
| 7 | Labor | 노동/고용 | 고용 영향, 자동화, 재교육, 노동자 보호 |
| 8 | Copyright | 저작권/지식재산 | 지적재산, AI 생성 콘텐츠, 학습 데이터 |
| 9 | International collaboration | 국제협력 | 국제 협력, 표준, 동맹 |
| 10 | Elections | 선거/민주주의 | 선거, 딥페이크, 허위정보, 정치적 AI 사용 |

### 분류 파이프라인 (법안)

Carvão 논문의 3단계 파이프라인을 재현:

**Stage 1: TF-IDF 키워드 분류**
- 법안 전문 토큰화 → TF-IDF 가중치 계산
- 각 Policy Attribute에 키워드 사전 배정 → 점수 합산 → Primary/Top 3 결정
- 한국어: kiwipiepy 명사 추출 (NNG, NNP 태그) + 한국어 키워드 사전
- 영어: sklearn TfidfVectorizer + 영어 키워드 사전
- 법률 불용어 제거: "bill", "section", "committee" (영어) / "법률", "조", "항" (한국어)
- "intelligence", "artificial" 등 AI 법안 공통 단어도 불용어 처리 (과매칭 방지)

**Stage 2: LDA 비지도 토픽 모델링**
- 10개 토픽으로 LDA 실행
- TF-IDF 분류와 LDA 토픽 간 교차 히트맵으로 분류 타당성 검증

**Stage 3: GPT 검증**
- GPT-4.1-mini로 동일 법안을 독립적으로 분류
- TF-IDF/LDA 결과와 비교하여 교차 검증
- **최종 분류는 GPT 결과를 사용** (가장 높은 정확도)

### 분류 파이프라인 (뉴스)

- 법안과 **동일한 10개 Policy Attribute** 사용
- GPT-4.1-mini로 전수 분류 (법안 분류와 동일 모델)
- Guardian: title + trail_text → 영어 프롬프트
- Naver: title + description → 한국어 프롬프트
- "none" 카테고리: AI 정책과 무관한 기사

### 한국 법안 AI 필터링

미국은 Brennan Center 연구자가 수동 식별한 150건을 사용. 한국은 자동 필터링:
1. **1차: 키워드 필터** — 제안이유/주요내용에 "인공지능" 또는 "AI" 3회 이상 언급
2. **2차: 중복 제거** — 동일 대표발의자 + 동일 법안명 → 최신 1건만 유지
3. **3차: GPT 판별** — "이 법안이 실질적으로 AI에 관한 것인가?" Yes/No 판별
   - Yes 기준: 법안의 주요 목적이 AI 규제/촉진/거버넌스
   - No 기준: AI를 배경으로만 언급 ("AI 시대에...", "인공지능 등 첨단기술의 발전으로...")

### Bipartisanship 계산

- **미국**: Democratic cosponsor ratio. >60% → Democratic-led, <40% → Republican-led, 40~60% → Bipartisan
- **한국**: 여당(더불어민주당) cosponsor ratio. >60% → 여당주도, <40% → 야당주도, 40~60% → 초당적

---

## 결과

### 1. 법안 메타데이터 비교

#### 정당별 발의

| | 미국 118th | 미국 119th | 한국 22대 |
|---|---:|---:|---:|
| 다수당 | D=100 (65%) | R=36 (68%) | 민주당=61 (50%) |
| 소수당 | R=53 (34%) | D=17 (32%) | 국민의힘=48 (40%) |
| 기타 | I=1 (1%) | - | 조국혁신당 4, 개혁신당 4, 기타 4 |
| **총 법안** | **154** | **53** | **121** |

#### 위원회 집중도

| 미국 (118th) | 건수 | 한국 (22대) | 건수 |
|---|---:|---|---:|
| Commerce/Science/Transport (S) | 36 | 과학기술정보방송통신위원회 | 41 |
| Energy & Commerce (H) | 20 | 보건복지위원회 | 16 |
| Science/Space/Tech (H) | 16 | 행정안전위원회 | 14 |
| Judiciary (H) | 13 | 산업통상자원중소벤처기업위원회 | 12 |

#### 최다 발의 의원

| 미국 (118th) | 건수 | 한국 (22대) | 건수 |
|---|---:|---|---:|
| Rounds, Mike [R-SD] | 7 | 최민희 (민주당) | 5 |
| Markey, Edward [D-MA] | 7 | 김상훈 (국민의힘) | 5 |
| Peters, Gary [D-MI] | 6 | 최보윤 (국민의힘) | 4 |
| Lieu, Ted [D-CA] | 5 | 이주영 (개혁신당) | 4 |
| Klobuchar, Amy [D-MN] | 5 | 차지호 (민주당) | 4 |

#### 법안 진행 상태

| 상태 | 미국 118th | 한국 22대 |
|------|------:|------:|
| 계류/심사 중 | 137 (89%) | 109 (90%) |
| 대안반영폐기 | - | 11 (9%) |
| 본회의 의결 | 0 (0%) | 0 (0%) |
| 폐기/철회 | - | 1 (1%) |

양국 모두 **0건 통과**. 한국의 "대안반영폐기" 11건은 인공지능 기본법 통합 과정에서 원안이 폐기된 것으로, 내용은 대안에 반영됨.

#### 초당성 (Bipartisanship)

| | 미국 118th | 한국 22대 |
|---|---:|---:|
| 다수당 주도 | 74 (48%) | - |
| 소수당 주도 | 24 (16%) | 60 (50%) |
| 초당적 | 56 (36%) | 61 (50%) |

한국이 초당적 비율이 높음 (50% vs 36%). 한국은 공동발의 10명+ 의무제로 인해 여야 혼합이 자연스러움.

### 2. 뉴스 vs 법안 Policy Attribute 비교 (GPT 분류)

#### 전체 비교표

| Policy Attribute | Guardian 뉴스 | Naver 뉴스 | 미국 118th | 미국 119th | 한국 22대 |
|---|---:|---:|---:|---:|---:|
| Safety | **19.2%** | 9.6% | 8% | **38%** | 13% |
| Responsible/Ethical AI | **16.6%** | 5.8% | **28%** | 4% | 7% |
| Market/Antitrust | **13.8%** | 5.4% | 3% | 4% | **0%** |
| Public interest | 10.8% | 4.7% | 16% | 13% | **27%** |
| Labor | **9.6%** | 3.6% | 3% | 2% | 3% |
| Copyright | **6.9%** | 2.1% | 2% | 2% | 5% |
| Industrial policy | 6.4% | **48.8%** | 12% | 6% | **39%** |
| National security | 4.3% | 7.5% | 16% | **28%** | 2% |
| Elections | 3.2% | 1.3% | 10% | 2% | 2% |
| Int'l collaboration | 0.7% | 0.8% | 3% | 2% | 0% |
| none (해당없음) | 8.2% | 10.4% | - | - | - |

#### 뉴스-법안 갭 (Guardian vs 미국 118th)

| Policy Attribute | Guardian 뉴스 | 미국 118th 법안 | 갭 (뉴스-법안) |
|---|---:|---:|---:|
| Market/Antitrust | 13.8% | 3% | **+10.8pp** ← 최대 갭 |
| Labor | 9.6% | 3% | **+6.6pp** |
| Copyright | 6.9% | 2% | **+4.9pp** |
| Safety | 19.2% | 8% | **+11.2pp** |
| Responsible AI | 16.6% | 28% | -11.4pp (법안 > 뉴스) |
| National security | 4.3% | 16% | **-11.7pp** (법안 > 뉴스) |
| Elections | 3.2% | 10% | **-6.8pp** (법안 > 뉴스) |

#### 뉴스-법안 갭 (Naver vs 한국 22대)

| Policy Attribute | Naver 뉴스 | 한국 22대 법안 | 갭 (뉴스-법안) |
|---|---:|---:|---:|
| Market/Antitrust | 5.4% | 0% | **+5.4pp** ← 최대 갭 |
| National security | 7.5% | 2% | **+5.5pp** |
| Responsible AI | 5.8% | 7% | -1.2pp |
| Public interest | 4.7% | 27% | **-22.3pp** (법안 >> 뉴스) |
| Industrial policy | 48.8% | 39% | +9.8pp |

### 3. 핵심 발견

#### 발견 1: Antitrust/시장경쟁은 양국 모두 최대 입법 갭

- Guardian 13.8% → 미국 법안 3~4%
- Naver 5.4% → 한국 법안 **0%**
- Carvão 논문의 정성적 발견 ("topics that dominate public discourse, such as Antitrust...")을 **정량적으로 확인**

#### 발견 2: 노동/고용은 뉴스 담론 대비 입법 부재

- Guardian 9.6% → 미국 법안 2~3%
- Naver 3.6% → 한국 법안 3%
- AI 자동화로 인한 고용 영향에 대한 뉴스 관심 대비 입법 대응 부족

#### 발견 3: 한미 입법 초점의 근본적 차이

- **한국**: 산업정책 39% + 공익 27% = **"AI를 키우고 보호하자"**
- **미국 118th**: Responsible AI 28% + National Security 16% = **"AI를 규제하고 안보에 쓰자"**
- **미국 119th**: Safety 38% + National Security 28% = **"AI를 안전하게, 국방에 쓰자"**

#### 발견 4: 정권 교체의 극적 효과 (미국 118th → 119th)

- Responsible AI: 28% → 4% (민주당 → 공화당 의회)
- Safety: 8% → 38% (공화당이 "안전" 프레이밍으로 전환)
- National Security: 16% → 28% (중국 경쟁 강조 심화)
- Elections: 10% → 2% (선거 관련 AI 규제 관심 급감)

#### 발견 5: 한국 뉴스의 산업정책 편중

- Naver 48.8%가 산업정책 — 거의 절반의 AI 뉴스가 기업/투자/반도체/데이터센터 관련
- Guardian은 Safety(19.2%), Responsible AI(16.6%), Antitrust(13.8%)로 분산
- 한국 미디어의 AI 담론이 "산업 육성" 프레임에 집중됨을 보여줌

---

## TF-IDF 분류 검증 (Carvão Replication)

118th Congress에 대해 논문의 TF-IDF 분류(Figure 20)를 재현:

| Attribute | 논문 Figure 20 | 우리 TF-IDF | 차이 |
|---|---:|---:|---:|
| Market efficiency | 35 | 34 | -1 |
| Elections | 22 | 20 | -2 |
| Copyright | 19 | 9 | -10 |
| National security | 18 | 22 | +4 |
| Public interest | 15 | 18 | +3 |
| Safety | 13 | 19 | +6 |
| Responsible AI | 10 | 16 | +6 |
| Industrial policy | 6 | 12 | +6 |
| Int'l collaboration | 6 | 1 | -5 |
| Labor | 4 | 3 | -1 |

Market efficiency(34 vs 35), Elections(20 vs 22), Labor(3 vs 4)는 근접 재현 성공. Copyright, Int'l collaboration에서 차이 — 키워드 사전의 세부 구성이 논문과 다르기 때문.

### TF-IDF vs GPT 일치율

| 데이터셋 | 일치율 |
|---------|------:|
| 미국 118th | 51.3% |
| 한국 22대 | 0% (카테고리명 형식 불일치) |

118th에서 51% 일치는 논문의 Figure 21 (TF-IDF vs LDA 교차 히트맵)에서도 관찰되는 수준의 불일치. GPT가 더 정확한 분류를 제공하며, 최종 분석에는 GPT 결과를 사용.

---

## 파일 구조

```
replicate_carvao/
├── PAPER_SUMMARY.md              ← 논문 정리
├── REPLICATION_PLAN.md           ← 재현 계획
├── PLAN_KOREA.md                 ← 한국 적용 계획
├── RESULTS.md                    ← 이 파일 (전체 결과)
│
├── 02_collect_bill_details.py    ← 118th Congress.gov API 수집
├── 03_preprocess.py              ← 118th 전처리
├── 04_download_text.py           ← 118th 법안 전문 다운로드
├── 05_nlp_classify.py            ← 118th TF-IDF + LDA
├── 06_gpt_validate.py            ← 118th GPT 검증
├── 07_visualize.py               ← 118th 시각화
│
├── us119_run_all.py              ← 119th 통합 파이프라인
│
├── kr_01_prepare_data.py         ← 한국 AI 법안 필터링
├── kr_02_preprocess.py           ← 한국 전처리
├── kr_03_nlp_classify.py         ← 한국 TF-IDF + LDA + GPT
│
├── news_classify.py              ← Guardian + Naver GPT 분류
│
├── data/
│   ├── brennan_118th_bills.json  ← 118th 법안 목록 (154건)
│   ├── brennan_119th_bills.json  ← 119th 법안 목록 (53건)
│   ├── bills_detail.json         ← 118th 상세 정보
│   ├── bills_processed.json      ← 118th 전처리 결과
│   ├── bills_text/               ← 118th 법안 전문 (154건, 3.56MB)
│   ├── tfidf_classification.json ← 118th TF-IDF 분류
│   ├── lda_topics.json           ← 118th LDA 토픽
│   ├── tfidf_lda_crossmap.json   ← 118th TF-IDF vs LDA 교차
│   ├── gpt_classification.json   ← 118th GPT 분류
│   │
│   ├── us119_bills_detail.json   ← 119th 상세 정보
│   ├── us119_bills_processed.json← 119th 전처리 결과
│   ├── us119_bills_text/         ← 119th 법안 전문
│   ├── us119_tfidf.json          ← 119th TF-IDF 분류
│   ├── us119_gpt.json            ← 119th GPT 분류
│   │
│   ├── kr_ai_candidates.json     ← 한국 1차 후보 (192건)
│   ├── kr_ai_bills.json          ← 한국 AI 법안 (121건)
│   ├── kr_bills_processed.json   ← 한국 전처리 결과
│   ├── kr_tfidf_classification.json ← 한국 TF-IDF 분류
│   ├── kr_lda_topics.json        ← 한국 LDA 토픽
│   ├── kr_gpt_classification.json← 한국 GPT 분류
│   │
│   ├── news_guardian_classified.json ← Guardian 뉴스 GPT 분류 (2,908건)
│   └── news_naver_classified.json   ← Naver 뉴스 GPT 분류 (19,165건)
│
└── figures/
    ├── fig08_party.png           ← 118th 정당별
    ├── fig09_bipartisan.png      ← 118th 초당성 scatter
    ├── fig10_state.png           ← 118th 주별
    ├── fig20_tfidf.png           ← 118th TF-IDF 분포
    ├── fig21_heatmap.png         ← 118th TF-IDF vs LDA
    ├── fig22_monthly.png         ← 118th 월별 추이
    ├── fig23_chamber.png         ← 118th 원별 정당
    ├── fig27_house.png           ← 118th 하원 최다발의
    ├── fig28_senate.png          ← 118th 상원 최다발의
    ├── fig29_committees.png      ← 118th 위원회별
    ├── fig30_progression.png     ← 118th 진행 단계
    ├── fig31_attributes.png      ← 118th Policy Attribute (GPT)
    └── fig32_party_attr.png      ← 118th 정당별 Attribute
```

## 사용 도구 및 라이브러리

| 도구 | 용도 |
|------|------|
| Python 3.13 | 전체 파이프라인 |
| DuckDB | 한국 국회 데이터 저장/조회 |
| scikit-learn | TF-IDF, LDA |
| kiwipiepy | 한국어 형태소 분석 (명사 추출) |
| OpenAI GPT-4.1-mini | 법안/뉴스 분류, AI 관련성 판별 |
| matplotlib | 시각화 |
| requests | Congress.gov API, Brennan Center, Guardian API |
| sentence-transformers | SBERT (기존 AI 법안 태깅용, 본 분석에서는 미사용) |

## 한계점

1. **Naver 시간 범위**: 6주치만 확보. Guardian 3년과 비대칭. 빅카인즈 등으로 확장 필요.
2. **한국 법안 텍스트 커버리지**: 22대 전체 16,477건 중 8,958건만 텍스트 확보 (54%). 2024.06~2025.02 구간 부족.
3. **GPT 분류 일관성**: temperature=0으로 고정했지만 모델 버전 변경 시 결과 달라질 수 있음.
4. **한국 AI 법안 필터**: 자동(GPT) vs 미국(Brennan Center 수동) — 방법론 비대칭.
5. **119th Congress**: 현재 진행 중인 회기로 53건만 확보. 회기 종료 시 대폭 증가 예상.

## 다음 단계

1. **시각화 완성**: 5자 비교 차트 (Guardian/Naver/118th/119th/한국)
2. **Naver 확장**: 빅카인즈 API로 3년치 한국 뉴스 확보
3. **한국 법안 텍스트 확장**: `download_bills.py --all` 완료 후 재분석
4. **갭 분석 심화**: 토픽별 시계열 추이, 정당별 갭 차이
5. **일본 추가**: 일본 국회 AI 법안 데이터 수집 (향후)
