# 한국 국회 적용 계획: Carvão 방법론 → 22대 국회 AI 법안

## 목표

118th Congress replication에서 검증한 방법론(TF-IDF + LDA + GPT 3단계)을
22대 국회 AI 법안에 동일하게 적용하고, 한미 비교 분석을 수행한다.

## 방법론 차이: 영어 vs 한국어

### 1. 텍스트 전처리

| 항목 | 미국 (영어) | 한국 (한국어) |
|------|-----------|------------|
| 토큰화 | 공백 분리 + sklearn | **kiwipiepy 명사 추출** (NNG, NNP) |
| 불용어 | sklearn english + 법률용어 | **한국어 법률 불용어** 직접 정의 |
| TF-IDF | sklearn TfidfVectorizer | sklearn + **kiwipiepy 커스텀 tokenizer** |
| n-gram | unigram + bigram | **명사 단위 unigram** (한국어 bigram은 의미 없음) |

kiwipiepy는 이미 `topic_bills.py`, `topic_news.py`에서 사용 중.
기존 코드의 `extract_nouns()` 함수 재사용.

### 2. Policy Attributes — 한국 맥락 적응

Carvão의 10개 카테고리를 한국에 적용할 때 맥락 차이:

| 미국 Attribute | 한국 대응 | 차이점 |
|---------------|---------|--------|
| Market efficiency (antitrust) | 시장경쟁/독과점 | 한국은 공정거래법 별도 체계, AI 독과점 논의 적음 |
| Safety | AI 안전 | 유사하나 "위험관리" 표현 다름 |
| Responsible & Ethical AI | 책임/윤리 AI | 한국은 "신뢰할 수 있는 AI" 표현 선호 |
| National security | 국가안보 | 한국은 북한/사이버 맥락, 미국은 중국 맥락 |
| Industrial policy | 산업정책 | 한국 AI 산업 육성법, 과학기술 투자 |
| Public interest | 공익/소비자보호 | 유사 |
| Labor | 노동/고용 | 한국은 플랫폼 노동 논의 활발 |
| Copyright | 저작권 | 유사하나 한국은 웹툰/K-pop 맥락 |
| International collaboration | 국제협력 | 한국은 한미/한EU 협력 중심 |
| Elections | 선거 | 한국은 공직선거법 + 딥페이크 |

### 3. 한국어 키워드 사전

각 Attribute별 한국어 키워드 (kiwipiepy 명사 기준):

```
시장경쟁/독과점: 공정거래, 독점, 독과점, 시장지배, 플랫폼, 경쟁, 공정위, 불공정
AI안전: 안전, 위험, 사고, 피해, 테스트, 검증, 평가, 취약, 보안, 안전성
책임/윤리AI: 책임, 윤리, 투명, 투명성, 편향, 차별, 공정, 신뢰, 거버넌스, 감독, 설명가능
국가안보: 국방, 군사, 안보, 사이버, 정보기관, 북한, 테러, 방위, 국가정보원
산업정책: 산업, 육성, 연구개발, 투자, 반도체, 인프라, 데이터센터, 스타트업, 혁신, 진흥
공익/소비자: 소비자, 보호, 건강, 의료, 교육, 아동, 청소년, 환경, 에너지, 복지
노동/고용: 노동, 고용, 근로, 자동화, 일자리, 재교육, 플랫폼노동, 임금
저작권: 저작권, 지식재산, 창작, 생성, 딥페이크, 초상, 저작물, 학습데이터
국제협력: 국제, 협력, 조약, 표준, OECD, 글로벌, 다자, 양자
선거: 선거, 후보, 투표, 딥페이크, 허위정보, 가짜뉴스, 정치광고, 여론조작
```

### 4. 법안 진행 단계 (Stage) — 한국 국회

미국 Stage 0~10 → 한국 국회 의안 처리 과정:

| Stage | 한국 국회 | 대응 |
|-------|---------|------|
| 0 | 발의/제출 | Introduced |
| 1 | 상임위 회부 | Referred to Committee |
| 2 | 소위 회부 | Referred to Subcommittee |
| 3 | 소위/상임위 심사 | Committee Review |
| 4 | 법사위 회부 | Judiciary Committee (체계/자구 심사) |
| 5 | 본회의 상정 | Floor Consideration |
| 6 | 본회의 의결 | Passed |
| 7 | 정부 이송 | Sent to Government |
| 8 | 공포 | Promulgated |

`proc_result` 필드 활용: 원안가결, 수정가결, 대안반영폐기, 폐기, 철회 등

### 5. 발의자 분석 — 정당/지역

| 항목 | 미국 | 한국 |
|------|------|------|
| 정당 | D/R/I (2+1당) | 더불어민주당, 국민의힘, 조국혁신당 등 (다당) |
| 지역 | State (50개) | 지역구 (254개) 또는 비례대표 |
| Sponsor | 1명 대표발의 | 대표발의 1명 + 공동발의 10명+ (의무) |
| Bipartisanship | Dem ratio | 여당/야당 비율 |

## 데이터 현황

| 데이터 | 현황 | 필요 작업 |
|--------|------|----------|
| 22대 전체 법안 목록 | assembly.duckdb에 있음 | ✅ |
| 법안 텍스트 (bill_txt/) | 4,127건 (6개월분) | `download_bills.py --all`로 2년치 확장 필요 |
| AI 법안 필터 | 537건 (sim≥0.35) | 2년치 텍스트 확보 후 `tag_bills.py` 재실행 |
| 의원 정보 (정당/지역) | assembly.duckdb 의원 테이블 | ✅ |
| 위원회 정보 | 법안별 소관위원회 | ✅ |
| 처리 결과 | proc_result 필드 | ✅ |

## 파이프라인

```
Step 1: 데이터 준비
├── download_bills.py --all (법안 텍스트 2년치)
├── tag_bills.py (AI 법안 필터링)
└── 22대 AI 법안 목록 확정 (예상 500~800건)

Step 2: 상세 데이터 구축
├── assembly.duckdb에서 의원/위원회/처리결과 JOIN
├── 발의자 정당/지역 매핑
└── bills_processed_kr.json

Step 3: NLP 분류 (한국어)
├── kiwipiepy 명사 추출 + 한국어 불용어
├── TF-IDF → 10개 Policy Attribute 분류 (한국어 키워드)
├── LDA 토픽 모델링 (10개 토픽)
└── TF-IDF vs LDA 교차 히트맵

Step 4: GPT 검증
├── GPT-4o-mini 한국어 프롬프트
├── 동일 10개 Policy Attribute
└── TF-IDF vs GPT 일치도 확인

Step 5: 시각화
├── 정당별 법안 수 (여당/야당/소수당)
├── 의원별 발의 순위
├── 위원회별 분포
├── 월별 발의 추이
├── Policy Attribute 분포
└── 한미 비교 차트

Step 6: 한미 비교
├── Policy Attribute 분포 비교 (한국 vs 미국)
├── 시간별 추이 비교
├── 정당 구조 차이 분석
└── 갭 분석: 한쪽에만 있는 토픽 식별
```

## 핵심 구현 파일

```
replicate_carvao/
├── kr_01_prepare_data.py      ← 22대 AI 법안 목록 + 메타데이터
├── kr_02_preprocess.py        ← 전처리, Stage 분류, 정당 매핑
├── kr_03_nlp_classify.py      ← TF-IDF(kiwipiepy) + LDA
├── kr_04_gpt_validate.py      ← GPT 한국어 분류
├── kr_05_visualize.py         ← 한국 국회 시각화
├── kr_06_compare.py           ← 한미 비교 분석
└── data/
    ├── kr_ai_bills.json
    ├── kr_bills_processed.json
    ├── kr_tfidf_classification.json
    ├── kr_lda_topics.json
    └── kr_gpt_classification.json
```

## 리스크

1. **법안 텍스트 2년치 확보**: `download_bills.py --all`이 아직 미실행. PDF 다운로드 + OCR에 수 시간 소요.
2. **한국어 TF-IDF 키워드 사전**: 영어와 달리 한국어는 합성어/조사 처리가 복잡. kiwipiepy 명사 추출에 의존하므로 "인공지능"은 하나의 명사로, "자율주행자동차"도 하나로 처리됨 — 키워드 매칭이 더 직관적일 수 있음.
3. **AI 법안 필터 범위**: 미국은 Brennan Center가 수동 식별한 150건. 한국은 SBERT 유사도 기반 자동 필터 — 방법론 비대칭. GPT 기반 AI 관련성 판단으로 보완 가능.
