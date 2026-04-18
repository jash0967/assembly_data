# Governance at a Crossroads: AI and the Future of Innovation in America

**Carvão, P., Ancheva, S., Atir, Y., Jeloka, S., & Zhou, B. (2025)**
Harvard Kennedy School, Mossavar-Rahmani Center for Business and Government
SSRN: 5131048 | M-RCBG Working Paper #251

## 논문 개요

AI 규제에 대한 동적 거버넌스 모델을 제안하는 논문. 118th Congress의 AI 법안 150건에 대한 정량 분석과 의원/산업계 인터뷰를 통해, 미국 AI 입법의 현황과 공론장-입법 간 갭을 분석.

## 논문 구조

### Part I — The Context (p.6-38)
- **A false choice**: 규제 vs 혁신은 이분법이 아님
- **AI triad**: 알고리즘 + 데이터 + 컴퓨팅 파워
- **Second AI triad**: 에너지 + 토지 + 노동 (데이터센터 중심)
- **AI policy considerations**: 8개 Policy Attribute 다이어그램 (Figure 5, p.23)
  - National Security, Industrial Policy, Market Efficiency & Power Concentration, Safety, Intellectual Property & Copyright, Public Interest, Environmental Considerations
- **How the U.S. implements policy**: Chevron Doctrine 폐지, 주정부 규제 파편화

### Part II — Industry and Congress Perspectives (p.39-63)
- **Industry voices**: CEO/투자자 인터뷰 — 규제 필요성 인정하되 속도 우려
- **118th Congress 정량 분석** (Appendix II 참조)
- **Congressional voices**: 5가지 테마
  1. 규제 vs 혁신 논쟁 (양당 시각 차이)
  2. 기업 이익에 의한 입법 부재
  3. 연방 차원 긴급성 부족
  4. 산업계-의회 협업의 필수성
  5. 입법 교착의 원인들
- **Where Congress and Industry could come together** (p.60-63)
  - 합의 영역: 미국 AI 리더십, 공공-민간 협력, R&D 투자, 에너지/데이터센터
  - 불일치 영역: Privacy, Copyright/IP, Market Competition, Workforce, 주정부 역할, Deepfake/CSAM

### Part III — A New Governance Model (p.64-84)
- **Dynamic Governance Model** 3가지 구성요소:
  1. 평가 표준을 위한 공공-민간 파트너십
  2. 감사 및 준수를 위한 시장 기반 생태계
  3. 입법부/행정부/법원에 의한 책임 및 배상 체계

## Appendix II — 118th Congress AI 법안 분석 (p.89-107)

### 데이터 수집
- **소스**: Congress.gov API + Brennan Center for Justice AI Legislation Tracker
- **대상**: 118th Congress (2023.01.03 ~ 2025.01.03) AI 관련 법안 **150건**
- **수집 필드**: bill number, title, text, summary, sponsor, cosponsors, committees, actions

### 데이터 전처리
- **Bill Progression Stage 0~10** (regex 기반 분류):

| Stage | 설명 |
|-------|------|
| 0 | Introduced |
| 1 | Referred to Committee |
| 2 | Referred to Subcommittee |
| 3 | Reported Out of Committee |
| 4 | Placed on Calendar |
| 5 | Floor Consideration |
| 6 | Passed One Chamber |
| 7 | Received by Second Chamber |
| 8 | Passed Second Chamber |
| 9 | Presented to President |
| 10 | Signed into Law |

### NLP 기반 분류 — 3단계 파이프라인

**Stage 1: TF-IDF**
- 법안 전문 토큰화 → 불용어 제거 ("bill", "committee", "section" 등)
- TF-IDF 가중치로 키워드 추출 → 10개 Policy Attribute에 매핑

**Stage 2: LDA (Latent Dirichlet Allocation)**
- 비지도 토픽 모델링으로 자연 토픽 클러스터 발견
- LDA 토픽 10개:
  1. AI Technology & Innovation
  2. Consumer Protection
  3. Data Privacy & Security
  4. Digital Infrastructure
  5. Economic Impact
  6. Government Oversight
  7. International Relations
  8. Public Safety
  9. Regulatory Framework
  10. Research & Development

**Stage 3: GPT-4o 검증**
- 동일 법안 텍스트를 GPT-4o에 분류시켜 NLP 결과와 교차 검증

### 10개 Policy Attributes

| # | Attribute | Primary | Top 3 |
|---|-----------|--------:|------:|
| 1 | Responsible and ethical AI | 71 | 141 |
| 2 | National security | 24 | 106 |
| 3 | Safety | 14 | 66 |
| 4 | Public interest | 13 | 53 |
| 5 | Market efficiency and power concentration (antitrust) | 5 | 29 |
| 6 | Industrial policy | 10 | 13 |
| 7 | Elections | 9 | 12 |
| 8 | Copyright | 4 | 9 |
| 9 | International collaboration | 3 | 8 |
| 10 | Labor | 3 | 4 |
| 11 | Privacy and Data Protection | 2 | 3 |

### 주요 메타데이터 분석 결과

**발의 정당 (Figure 8)**:
- Democratic: 99건 (66%)
- Republican: 50건 (33%)
- Independent: 1건

**발의 주 (Figure 10)**: 36개 주, CA 23건 1위

**위원회 (Figure 29)**:
- House: Energy & Commerce (20), Science/Space/Tech (15), Judiciary (9)
- Senate: Commerce/Science/Transportation (33), HELP (8), HSGAC (8)

**최다 발의 의원**:
- Senate: Mike Rounds [R-SD] 7건, Edward Markey [D-MA] 7건
- House: Ted Lieu [D-CA] 5건

**법안 진행 (Figure 30)**: 94건 Stage 0~1, 17건 Stage 4+, **0건 법률 서명**

**시간 분포 (Figure 22)**: 2024년 7~9월 피크 (월 15~17건)

### 핵심 발견: 공론장 vs 입법 갭

> "topics that dominate public discourse, such as **Antitrust** and **Copyright**, appear as primary attributes in only **5 and 4 bills**, respectively." (p.56)

- **Privacy/Data Protection**: Top 3 기준 단 3건 — 연방 차원 프라이버시 법 부재
- **Copyright**: 4건 — 법원 판결 대기 성향
- **Antitrust**: 5건 — 빅테크 로비 영향
- 반면 **Responsible AI** (71건), **National Security** (24건)은 과대 대표

### Figure 목록 (재현 대상)

| Figure | 내용 | 페이지 |
|--------|------|--------|
| 8 | Bill sponsorship by party | 53 |
| 9 | Bipartisanship analysis (scatter) | 54 |
| 10 | AI bills by state | 55 |
| 11 | Primary policy attributes | 56 |
| 12 | Top 3 policy attributes | 56 |
| 20 | TF-IDF Distribution of Bill Categories | 93 |
| 21 | TF-IDF vs LDA heatmap | 94 |
| 22 | Monthly distribution (Congress/House/Senate) | 97 |
| 23 | Sponsorship by chamber | 98 |
| 24 | Bipartisanship by chamber | 99 |
| 25 | Bipartisanship Congress total | 100 |
| 26 | Bills by state (Congress/House/Senate) | 101 |
| 27 | Most active members (House) | 102 |
| 28 | Most active members (Senate) | 102 |
| 29 | Most active committees | 103 |
| 30 | Bill progression | 104 |
| 31 | Bill policy attributes (Primary + Top 3) | 104 |
| 32 | Primary attribute by party | 105 |
| 33 | Top 3 attributes by party | 106 |
| 34 | Attributes for Stage 3+ bills | 107 |
| 35 | 17 bills on legislative calendar (table) | 107 |

## 인용

```bibtex
@techreport{carvao2025governance,
  title={Governance at a Crossroads: Artificial Intelligence and the Future of Innovation in America},
  author={Carvão, Paulo and Ancheva, Slavina and Atir, Yam and Jeloka, Shaurya and Zhou, Brian},
  year={2025},
  institution={Harvard Kennedy School, Mossavar-Rahmani Center for Business and Government},
  number={M-RCBG AWP 251},
  url={https://ssrn.com/abstract=5131048}
}
```
