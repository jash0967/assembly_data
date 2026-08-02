# 국회 Open API 데이터 코드북

> **수집일**: 2026-04-13 (Phase 1~6 통합 마이그레이션 2026-04-18)
> **수집 스크립트**: `collect/download_all.py`
> **데이터베이스**: `data/bills_kr/assembly_raw.duckdb` (~7.3 GB, 수집·추출) + `data/bills_kr/assembly_analysis.duckdb` (~3 MB, 분류·태깅·집계) + `data/news/news.duckdb` (한국 도메스틱 뉴스 157K). raw/analysis 분리는 2026-05-09부터, news DB는 2026-05-21 추가.
> **수집 범위**: 제13대 ~ 제22대 국회 (일부 테이블은 전체 대수 포함)
> **총 레코드**: 약 4,890,000건 (37개 Open API + bill_text 77K + 분류 결과 1.7K)
>
> **2026-04-18 변경**: 모든 per-age 테이블에 `age INTEGER` 컬럼 표준화. 기존
> AGE/DAE_NUM/ERACO/UNIT_CD 등 다양한 형식의 대수 식별자는 그대로 유지하되,
> 신규 `age` 컬럼이 정수 캐스트 + 표준 의미를 보장. 자세한 출처는 §13의
> [AGE 컬럼 출처표](#age-컬럼-출처표) 참고. ApiSpec 선언은
> [config.py](config.py)의 `_RAW`에 있음.

---

## 목차

1. [의원 기본정보](#1-의원-기본정보)
2. [의원 활동](#2-의원-활동)
3. [의안(법률안)](#3-의안법률안)
4. [표결](#4-표결)
5. [청원](#5-청원)
6. [정당/위원회](#6-정당위원회)
7. [법안 심사](#7-법안-심사)
8. [회의록](#8-회의록)
9. [예산](#9-예산)
10. [네트워크/맥락](#10-네트워크맥락)
11. [연구/보고서](#11-연구보고서)
12. [공통 사항](#12-공통-사항)

---

## 1. 의원 기본정보

### `nwvrqwxyaytdsfvhu` — 국회의원 인적사항
- **건수**: 295
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 현재(22대) 국회의원의 기본 인적사항. 의원 단위 분석의 마스터 테이블.
- **주요 조인키**: `MONA_CD` (의원 고유코드)

| 컬럼명 | 설명 |
|---------|------|
| `MONA_CD` | 의원 고유코드 (조인키) |
| `HG_NM` | 한글 이름 |
| `HJ_NM` | 한자 이름 |
| `ENG_NM` | 영문 이름 |
| `BTH_GBN_NM` | 생년월일 구분 (양력/음력) |
| `BTH_DATE` | 생년월일 |
| `SEX_GBN_NM` | 성별 (남/여) |
| `POLY_NM` | 정당명 |
| `ORIG_NM` | 선거구명 |
| `ELECT_GBN_NM` | 당선 구분 (지역구/비례대표) |
| `REELE_GBN_NM` | 재선 구분 (초선, 재선, 3선 ...) |
| `CMIT_NM` | 소속 위원회 |
| `CMITS` | 겸임 위원회 목록 |
| `UNITS` | 역대 당선 대수 |
| `JOB_RES_NM` | 직책 |
| `TEL_NO` | 전화번호 |
| `E_MAIL` | 이메일 |
| `HOMEPAGE` | 홈페이지 URL |
| `STAFF` | 보좌관 |
| `SECRETARY` | 비서관 |
| `SECRETARY2` | 비서 |
| `MEM_TITLE` | 약력 |
| `ASSEM_ADDR` | 사무실 주소 |

### `nexgtxtmaamffofof` — 국회의원 의원이력
- **건수**: 613
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 의원의 경력 이력 (복수 행/의원 가능).

| 컬럼명 | 설명 |
|---------|------|
| `MONA_CD` | 의원 고유코드 |
| `HG_NM` | 한글 이름 |
| `HJ_NM` | 한자 이름 |
| `FRTO_DATE` | 기간 |
| `PROFILE_SJ` | 경력 내용 |
| `UNIT_CD` | 대수 코드 |
| `UNIT_NM` | 대수명 |

### `nyzrglyvagmrypezq` — 국회의원 위원회 경력
- **건수**: 3,716
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 의원이 역임한 위원회 활동 경력.

| 컬럼명 | 설명 |
|---------|------|
| `MONA_CD` | 의원 고유코드 |
| `HG_NM` | 한글 이름 |
| `HJ_NM` | 한자 이름 |
| `FRTO_DATE` | 활동 기간 |
| `PROFILE_SJ` | 위원회 경력 내용 |
| `PROFILE_UNIT_CD` | 대수 코드 |
| `PROFILE_UNIT_NM` | 대수명 |

---

## 2. 의원 활동

### `nzmimeepazxkubdpn` — 국회의원 발의법률안
- **건수**: 97,417
- **수집 전략**: 대수별 (age, 13대~22대)
- **설명**: 의원이 대표/공동 발의한 법률안 목록.

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 의안 ID (조인키) |
| `BILL_NO` | 의안 번호 |
| `BILL_NAME` | 의안명 |
| `PROPOSER` | 대표발의자 |
| `RST_PROPOSER` | 대표발의자 (코드) |
| `RST_MONA_CD` | 대표발의자 의원코드 |
| `PUBL_PROPOSER` | 공동발의자 |
| `PUBL_MONA_CD` | 공동발의자 의원코드 |
| `MEMBER_LIST` | 발의 의원 목록 |
| `COMMITTEE` | 소관 위원회 |
| `COMMITTEE_ID` | 위원회 코드 |
| `PROPOSE_DT` | 발의일 |
| `PROC_RESULT` | 처리 결과 |
| `PROC_DT` | 처리일 |
| `AGE` | 대수 |
| `DETAIL_LINK` | 상세 URL |
| `CMT_PROC_RESULT_CD` | 위원회 처리 결과 |
| `CMT_PROC_DT` | 위원회 처리일 |
| `CMT_PRESENT_DT` | 위원회 상정일 |
| `COMMITTEE_DT` | 위원회 회부일 |
| `LAW_PROC_DT` | 법사위 처리일 |
| `LAW_PROC_RESULT_CD` | 법사위 처리 결과 |
| `LAW_PRESENT_DT` | 법사위 상정일 |
| `LAW_SUBMIT_DT` | 법사위 회부일 |

### `nuvypcdgahexhvrjt` — 국회의원 상임위 활동
- **건수**: 14,710
- **수집 전략**: 대수별 (age, `DAE_NUM`)
- **설명**: 의원의 상임위원회 회의 참석/활동 기록.

| 컬럼명 | 설명 |
|---------|------|
| `DAE_NUM` | 대수 |
| `SES_NUM` | 회기 |
| `DEGREE_NUM` | 차수 |
| `COMM_NAME` | 위원회명 |
| `CONF_DATE` | 회의일 |
| `BILL_URL` | 회의록 URL |
| `CLASS_NAME` | 회의 구분 |

### `negnlnyvatsjwocar` — 국회의원 SNS정보
- **건수**: 295
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 의원별 SNS 계정 정보.

| 컬럼명 | 설명 |
|---------|------|
| `MONA_CD` | 의원 고유코드 |
| `HG_NM` | 한글 이름 |
| `T_URL` | 트위터(X) URL |
| `F_URL` | 페이스북 URL |
| `Y_URL` | 유튜브 URL |
| `B_URL` | 블로그 URL |

### `nbqbmccpamsvwebkn` — 국회의원 정책 세미나 개최
- **건수**: 22,661
- **수집 전략**: 연도별 (year_host, 2000~현재)
- **설명**: 의원이 주최한 정책 세미나 목록.

| 컬럼명 | 설명 |
|---------|------|
| `TITLE` | 세미나 제목 |
| `SEMINAR_DIV_CODE` | 세미나 구분 코드 |
| `HOST_DT` | 개최일 |
| `HOST_PLACE_NAME` | 개최 장소 |
| `HOST_INS_NAME` | 주최 기관 |
| `ATTENDANCE_NAME1` | 참석자1 |
| `ATTENDANCE_NAME2` | 참석자2 |
| `DETAIL_VIEW_URL` | 상세 URL |

### `numwhtqhavaqssfle` — 국회의원 연구단체 등록현황
- **건수**: 455
- **수집 전략**: 대수별 (age, `REGDAESU`)
- **설명**: 의원 연구단체 등록 현황.

| 컬럼명 | 설명 |
|---------|------|
| `REGDAESU` | 등록 대수 |
| `RE_TOPIC_NAME` | 연구 주제명 |
| `RE_NAME` | 연구단체명 |
| `RE_OBJECTIVE` | 연구 목적 |
| `MAIN_MEM` | 대표 의원 |
| `RE_MEM` | 연구 의원 |
| `OBJ_MEM` | 참여 의원 |
| `MEMBER_CNT` | 참여 인원수 |
| `LINK_URL` | URL |

### `npbzvuwvasdqldskm` — 국회의원 기자회견
- **건수**: 41,476
- **수집 전략**: 연도별 (year, 2000~현재)
- **설명**: 의원 기자회견 기록.

| 컬럼명 | 설명 |
|---------|------|
| `TAKING_DATE` | 회견일 |
| `OPEN_TIME` | 시작 시간 |
| `TITLE` | 회견 제목 |
| `PERSON` | 회견자 |
| `REC_TIME` | 녹화 시간 |
| `LINK_URL` | URL |

### `nmfcjtvmajsbhhckf` — 국회의원 의정보고서
- **건수**: 33,079
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 의원이 발행한 의정보고서 목록.

| 컬럼명 | 설명 |
|---------|------|
| `TITLE` | 보고서 제목 |
| `PUBLISHER` | 발행자 |
| `PUBLISH_DT` | 발행일 |
| `UPDATE_DT` | 수정일 |
| `DETAIL_VIEW_URL` | 상세 URL |

### `nqfvrbsdafrmuzixe` — 날짜별 의정활동
- **건수**: 933,120
- **수집 전략**: 일별 (daily, 대수별 전체 기간)
- **설명**: 날짜별 의안 처리 이력. 의안의 단계 변화를 추적할 수 있는 타임라인 데이터.

| 컬럼명 | 설명 |
|---------|------|
| `SEQ` | 순번 |
| `DT` | 날짜 |
| `AGE` | 대수 |
| `BILL_ID` | 의안 ID |
| `BILL_NO` | 의안 번호 |
| `BILL_NM` | 의안명 |
| `BILL_KIND` | 의안 종류 |
| `STAGE` | 심사 단계 |
| `DTL_STAGE` | 세부 단계 |
| `COMMITTEE` | 위원회 |
| `COMMITTEE_ID` | 위원회 코드 |
| `ACT_STATUS` | 활동 상태 |
| `LINK_URL` | URL |

---

## 3. 의안(법률안)

### `billrcp` — 의안 접수목록
- **건수**: 118,705
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 역대 전체 의안 접수 목록. 제헌의회~22대까지 포함.
- **주요 조인키**: `BILL_ID`

| 컬럼명 | 설명 |
|---------|------|
| `ERACO` | 대수 (예: "제22대", "제헌") |
| `BILL_ID` | 의안 ID (조인키) |
| `BILL_NO` | 의안 번호 |
| `BILL_KIND` | 의안 종류 (법률안, 예산안 등) |
| `BILL_NM` | 의안명 |
| `PPSR_KIND` | 제안 구분 (의원, 정부, 위원장) |
| `PPSL_DT` | 제안일 |
| `PROC_RSLT` | 처리 결과 |
| `LINK_URL` | 상세 URL |

### `billinfodetail` — 의안 상세정보
- **건수**: 107,300
- **수집 전략**: BILL_ID별 개별 조회 (lookup_bill, phase 2)
- **설명**: 의안별 심사 진행 단계 상세. `billrcp`의 각 의안에 대한 소관위/법사위/본회의 처리 결과.

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 의안 ID |
| `BILL_NO` | 의안 번호 |
| `BILL_NM` | 의안명 |
| `PPSR_KIND` | 제안 구분 |
| `PPSR` | 제안자 |
| `PPSL_DT` | 제안일 |
| `PPSL_SESS` | 제안 회기 |
| `JRCMIT_NM` | 소관위원회명 |
| `JRCMIT_CMMT_DT` | 소관위 회부일 |
| `JRCMIT_PRSNT_DT` | 소관위 상정일 |
| `JRCMIT_PROC_DT` | 소관위 처리일 |
| `JRCMIT_PROC_RSLT` | 소관위 처리 결과 |
| `LAW_CMMT_DT` | 법사위 회부일 |
| `LAW_PRSNT_DT` | 법사위 상정일 |
| `LAW_PROC_DT` | 법사위 처리일 |
| `LAW_PROC_RSLT` | 법사위 처리 결과 |
| `RGS_PRSNT_DT` | 본회의 상정일 |
| `RGS_RSLN_DT` | 본회의 의결일 |
| `RGS_CONF_NM` | 본회의 회의명 |
| `RGS_CONF_RSLT` | 본회의 의결 결과 |
| `GVRN_TRSF_DT` | 정부이송일 |
| `PROM_LAW_NM` | 공포 법률명 |
| `PROM_DT` | 공포일 |
| `PROM_NO` | 공포 번호 |

### `nzivskufaliivfhpb` — 역대 의안 통계
- **건수**: 20
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 대수별 의안 처리 통계 요약.

| 컬럼명 | 설명 |
|---------|------|
| `ERACO` | 대수 |
| `SBM` | 제출 건수 |
| `DEAL_CN_PSSG` | 가결 건수 |
| `DEAL_CN_RJCTN` | 부결 건수 |
| `DEAL_CN_DSU` | 폐기 건수 |
| `DEAL_CN_WTHD` | 철회 건수 |
| `DEAL_CN_GVB` | 대안반영 건수 |
| `DEAL_CN_RSVT` | 보류 건수 |
| `TERM_EXPR_DSU` | 임기만료 폐기 건수 |

---

## 4. 표결

### `ncocpgfiaoituanbr` — 의안별 표결현황
- **건수**: 8,134
- **수집 전략**: 대수별 (age)
- **설명**: 의안별 본회의 표결 집계 (찬성/반대/기권 총수).

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 의안 ID |
| `BILL_NO` | 의안 번호 |
| `BILL_NAME` | 의안명 |
| `AGE` | 대수 |
| `PROC_DT` | 표결일 |
| `PROC_RESULT_CD` | 표결 결과 |
| `BILL_KIND_CD` | 의안 종류 |
| `CURR_COMMITTEE` | 소관 위원회 |
| `CURR_COMMITTEE_ID` | 위원회 코드 |
| `MEMBER_TCNT` | 재적 의원수 |
| `VOTE_TCNT` | 투표 의원수 |
| `YES_TCNT` | 찬성수 |
| `NO_TCNT` | 반대수 |
| `BLANK_TCNT` | 기권수 |
| `LINK_URL` | URL |

### `nojepdqqaweusdfbi` — 국회의원 본회의 표결정보
- **건수**: 2,420,862
- **수집 전략**: 의안+대수별 개별 조회 (lookup_bill_age, phase 2)
- **설명**: 의원 개인별 표결 기록. 가장 큰 테이블.

| 컬럼명 | 설명 |
|---------|------|
| `MONA_CD` | 의원 고유코드 |
| `HG_NM` | 한글 이름 |
| `HJ_NM` | 한자 이름 |
| `POLY_NM` | 정당 |
| `POLY_CD` | 정당 코드 |
| `ORIG_NM` | 선거구 |
| `ORIG_CD` | 선거구 코드 |
| `MEMBER_NO` | 의원 번호 |
| `AGE` | 대수 |
| `BILL_ID` | 의안 ID |
| `BILL_NO` | 의안 번호 |
| `BILL_NAME` | 의안명 |
| `LAW_TITLE` | 법률 제목 |
| `VOTE_DATE` | 표결일 |
| `RESULT_VOTE_MOD` | 표결 결과 (찬성/반대/기권) |
| `CURR_COMMITTEE` | 소관 위원회 |
| `CURR_COMMITTEE_ID` | 위원회 코드 |
| `SESSION_CD` | 회기 코드 |
| `CURRENTS_CD` | 차수 코드 |
| `DEPT_CD` | 부서 코드 |
| `DISP_ORDER` | 정렬 순서 |
| `BILL_URL` | 의안 URL |
| `BILL_NAME_URL` | 의안명 URL |

---

## 5. 청원

### `nvqbafvaajdiqhehi` — 청원 계류현황
- **건수**: 274
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 현재 계류 중인 청원 목록.

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 청원 ID |
| `BILL_NO` | 청원 번호 |
| `BILL_NAME` | 청원명 |
| `AGE` | 대수 |
| `PROPOSER` | 청원인 |
| `APPROVER` | 소개 의원 |
| `PROPOSE_DT` | 청원일 |
| `CURR_COMMITTEE` | 소관 위원회 |
| `CURR_COMMITTEE_ID` | 위원회 코드 |
| `COMMITTEE_DT` | 회부일 |
| `LINK_URL` | URL |

### `ncryefyuaflxnqbqo` — 청원 처리현황
- **건수**: 3,742
- **수집 전략**: 대수별 (age)
- **설명**: 대수별 청원 처리 결과.

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 청원 ID |
| `BILL_NO` | 청원 번호 |
| `BILL_NAME` | 청원명 |
| `AGE` | 대수 |
| `PROPOSER` | 청원인 |
| `APPROVER` | 소개 의원 |
| `PROPOSE_DT` | 청원일 |
| `PROC_RESULT_CD` | 처리 결과 |
| `CURR_COMMITTEE` | 소관 위원회 |
| `CURR_COMMITTEE_ID` | 위원회 코드 |
| `COMMITTEE_DT` | 회부일 |
| `LINK_URL` | URL |

---

## 6. 정당/위원회

### `nepjpxkkabqiqpbvk` — 정당 및 교섭단체 의석수
- **건수**: 9
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 현재 정당/교섭단체별 의석 분포.

| 컬럼명 | 설명 |
|---------|------|
| `POLY_GROUP_NM` | 교섭단체명 |
| `POLY_NM` | 정당명 |
| `N1` ~ `N4` | 의석수 관련 수치 |

### `nxrvzonlafugpqjuh` — 위원회 현황 정보
- **건수**: 356
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 국회 위원회 구성 현황.

| 컬럼명 | 설명 |
|---------|------|
| `CMT_DIV_CD` | 위원회 구분 코드 |
| `CMT_DIV_NM` | 위원회 구분명 |
| `HR_DEPT_CD` | 부서 코드 |
| `COMMITTEE_NAME` | 위원회명 |
| `HG_NM` | 위원장명 |
| `HG_NM_LIST` | 간사 의원 목록 |
| `LIMIT_CNT` | 정원 |
| `CURR_CNT` | 현원 |
| `POLY99_CNT` | 여당 의원수 |
| `POLY_CNT` | 야당 의원수 |

### `nktulghcadyhmiqxi` — 위원회 위원 명단
- **건수**: 524
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 각 위원회 소속 의원 명단.

| 컬럼명 | 설명 |
|---------|------|
| `DEPT_CD` | 위원회 코드 |
| `DEPT_NM` | 위원회명 |
| `JOB_RES_NM` | 직책 (위원장, 간사 등) |
| `HG_NM` | 한글 이름 |
| `HJ_NM` | 한자 이름 |
| `ORIG_NM` | 선거구 |
| `POLY_NM` | 정당 |
| `MONA_CD` | 의원 고유코드 |
| `ASSEM_TEL` | 국회 전화 |
| `ASSEM_EMAIL` | 국회 이메일 |
| `ROOM_NO` | 사무실 호 |
| `STAFF` | 보좌관 |
| `SECRETARY` | 비서관 |
| `SECRETARY2` | 비서 |

---

## 7. 법안 심사

### `ndiwuqmpambgvnfsj` — 위원회 계류법률안
- **건수**: 13,174
- **수집 전략**: 위원회별 (committee)
- **설명**: 각 위원회에 계류 중인 법률안 목록.

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 의안 ID |
| `BILL_NO` | 의안 번호 |
| `BILL_NAME` | 의안명 |
| `PROPOSER` | 제안자 |
| `RST_PROPOSER` | 대표발의자 |
| `RST_MONA_CD` | 대표발의자 코드 |
| `CURR_COMMITTEE` | 소관 위원회 |
| `CURR_COMMITTEE_ID` | 위원회 코드 |
| `AGE` | 대수 |
| `PROPOSE_DT` | 제안일 |
| `COMMITTEE_DT` | 회부일 |
| `CMT_PRESENT_DT` | 위원회 상정일 |
| `CMT_PROC_DT` | 위원회 처리일 |
| `CMT_PROC_RESULT_CD` | 위원회 처리 결과 |
| `LAW_SUBMIT_DT` | 법사위 회부일 |
| `LAW_PRESENT_DT` | 법사위 상정일 |
| `LAW_PROC_DT` | 법사위 처리일 |
| `LAW_PROC_RESULT_CD` | 법사위 처리 결과 |
| `URL` | URL |

### `nwbpacrgavhjryiph` — 본회의 처리안건 법률안
- **건수**: 74,640
- **수집 전략**: 대수별 (age)
- **설명**: 본회의에서 처리된 법률안. 표결 수치 포함.

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 의안 ID |
| `BILL_NO` | 의안 번호 |
| `BILL_NM` | 의안명 |
| `BILL_KIND` | 의안 종류 |
| `AGE` | 대수 |
| `PROPOSER` | 제안자 |
| `COMMITTEE_NM` | 소관 위원회 |
| `PROC_RESULT_CD` | 처리 결과 |
| `VOTE_TCNT` | 투표수 |
| `YES_TCNT` | 찬성수 |
| `NO_TCNT` | 반대수 |
| `BLANK_TCNT` | 기권수 |
| `PROPOSE_DT` | 제안일 |
| `COMMITTEE_SUBMIT_DT` | 위원회 회부일 |
| `COMMITTEE_PRESENT_DT` | 위원회 상정일 |
| `COMMITTEE_PROC_DT` | 위원회 처리일 |
| `LAW_SUBMIT_DT` | 법사위 회부일 |
| `LAW_PRESENT_DT` | 법사위 상정일 |
| `LAW_PROC_DT` | 법사위 처리일 |
| `RGS_PRESENT_DT` | 본회의 상정일 |
| `RGS_PROC_DT` | 본회의 처리일 |
| `CURR_TRANS_DT` | 정부이송일 |
| `ANNOUNCE_DT` | 공포일 |
| `CURR_COMMITTEE_ID` | 위원회 코드 |
| `LINK_URL` | URL |

### `nrvsawtaauyihadij` — 인사청문회
- **건수**: 58
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 인사청문회 실시 내역.

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 의안 ID |
| `BILL_NO` | 의안 번호 |
| `BILL_NAME` | 안건명 |
| `AGE` | 대수 |
| `APPOINT_GRADE` | 임명 직급 |
| `APPOINT_NAME` | 후보자 이름 |
| `PROPOSE_DT` | 요청일 |
| `CURR_COMMITTEE` | 소관 위원회 |
| `CURR_COMMITTEE_ID` | 위원회 코드 |
| `SUBMIT_DT` | 회부일 |
| `PRESENT_DT` | 상정일 |
| `PROC_DT` | 처리일 |
| `PROC_RESULT` | 처리 결과 |
| `LINK_URL` | 의안 URL |
| `MP_BOOK_URL` | 인사청문회 경과보고서 URL |

---

## 8. 회의록

### `nzbyfwhwaoanttzje` — 본회의 회의록
- **건수**: 26,381
- **수집 전략**: 대수+연도별 (age_year)
- **설명**: 본회의 회의록 메타정보 (PDF 링크 포함).

| 컬럼명 | 설명 |
|---------|------|
| `CONF_ID` | 회의록 ID |
| `CONFER_NUM` | 회의 번호 |
| `TITLE` | 회의 제목 |
| `CLASS_NAME` | 회의 구분 |
| `DAE_NUM` | 대수 |
| `CONF_DATE` | 회의일 |
| `SUB_NAME` | 부제 |
| `VOD_LINK_URL` | 영상 URL |
| `CONF_LINK_URL` | 회의록 URL |
| `PDF_LINK_URL` | PDF URL |

### `ncwgseseafwbuheph` — 위원회 회의록
- **건수**: 426,171
- **수집 전략**: 대수+연도별 (age_year)
- **설명**: 위원회 회의록 메타정보.

| 컬럼명 | 설명 |
|---------|------|
| `CONF_ID` | 회의록 ID |
| `CONFER_NUM` | 회의 번호 |
| `TITLE` | 회의 제목 |
| `CLASS_NAME` | 회의 구분 |
| `DAE_NUM` | 대수 |
| `COMM_NAME` | 위원회명 |
| `VODCOMM_CODE` | 영상 코드 |
| `CONF_DATE` | 회의일 |
| `SUB_NAME` | 부제 |
| `VOD_LINK_URL` | 영상 URL |
| `CONF_LINK_URL` | 회의록 URL |
| `PDF_LINK_URL` | PDF URL |
| `PDF_FILE_ID` | PDF 파일 ID |
| `DEPT_CD` | 부서 코드 |

### `ngytonzwavydlbbha` — 전원위원회 회의록
- **건수**: 113
- **수집 전략**: 대수+연도별 (age_year)
- **설명**: 전원위원회 회의록 메타정보.

| 컬럼명 | 설명 |
|---------|------|
| `CONF_ID` | 회의록 ID |
| `CONFER_NUM` | 회의 번호 |
| `TITLE` | 회의 제목 |
| `CLASS_NAME` | 회의 구분 |
| `DAE_NUM` | 대수 |
| `CONF_DATE` | 회의일 |
| `SUB_NAME` | 부제 |
| `VOD_LINK_URL` | 영상 URL |
| `CONF_LINK_URL` | 회의록 URL |
| `PDF_LINK_URL` | PDF URL |

### `vconfsubcconflist` — 소위원회 회의록
- **건수**: 193
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 소위원회 회의록 목록.

| 컬럼명 | 설명 |
|---------|------|
| `CONF_ID` | 회의록 ID |
| `ERACO` | 대수 |
| `SESS` | 회기 |
| `DGR` | 차수 |
| `CONF_DT` | 회의일 |
| `CONF_KND` | 회의 종류 |
| `CMIT_CD` | 위원회 코드 |
| `CMIT_NM` | 위원회명 |
| `SB_CMIT_CD` | 소위원회 코드 |
| `SB_CMIT_NM` | 소위원회명 |
| `DOWN_URL` | 다운로드 URL |

### `vconfdetail` — 회의록별 상세정보
- **건수**: 193
- **수집 전략**: CONF_ID별 개별 조회 (lookup_conf, phase 2)
- **설명**: 개별 회의록 상세 메타 (시작/종료 시간, 장소, 청문회 여부 등).

| 컬럼명 | 설명 |
|---------|------|
| `CONF_ID` | 회의록 ID |
| `ERACO` | 대수 |
| `SESS` | 회기 |
| `DGR` | 차수 |
| `CONF_DT` | 회의일 |
| `CONF_KND` | 회의 종류 |
| `CMIT_NM` | 위원회명 |
| `SB_CMIT_NM` | 소위원회명 |
| `CONF_PLC` | 회의 장소 |
| `BG_PTM` | 시작 시간 |
| `ED_PTM` | 종료 시간 |
| `CONF_PTM` | 회의 시간 |
| `HR_HRG_YN` | 인사청문 여부 |
| `PBHRG_YN` | 공청회 여부 |
| `HRG_YN` | 청문회 여부 |
| `SITG_YN` | 대정부질문 여부 |
| `RMND_SPH_YN` | 긴급현안질문 여부 |
| `RDJM_SPH_YN` | 교섭단체대표연설 여부 |
| `FRNGUS_SPH_YN` | 외국인 연설 여부 |
| `DOWN_URL` | 다운로드 URL |

### `vconfbillconflist` — 의안별 회의록 목록
- **건수**: 177,973
- **수집 전략**: BILL_ID별 개별 조회 (lookup_bill, phase 2)
- **설명**: 특정 의안이 논의된 회의록 목록.

| 컬럼명 | 설명 |
|---------|------|
| `BILL_ID` | 의안 ID |
| `BILL_NM` | 의안명 |
| `CONF_KND` | 회의 종류 |
| `CONF_ID` | 회의록 ID |
| `ERACO` | 대수 |
| `SESS` | 회기 |
| `DGR` | 차수 |
| `CONF_DT` | 회의일 |
| `DOWN_URL` | 다운로드 URL |

---

## 9. 예산

### `nztwkhgzakucszgls` — 사업별 예산 편성 규모
- **건수**: 125
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 국회 사업별 예산 규모.

| 컬럼명 | 설명 |
|---------|------|
| `YR` | 연도 |
| `BZ_NM` | 사업명 |
| `BDG_TAMT` | 예산 총액 |

---

## 10. 네트워크/맥락

### `nxcxrdmpaonzzbkic` — 의원외교협의회 명단
- **건수**: 7
- **수집 전략**: 전체 1회 호출 (none)

| 컬럼명 | 설명 |
|---------|------|
| `RPT_NO` | 보고서 번호 |
| `RPT_TITLE` | 보고서 제목 |
| `STND_DT` | 기준일 |
| `WRT_NM` | 작성자 |
| `UNIT_CD` | 대수 코드 |
| `UNIT_NM` | 대수명 |
| `FILE_ID` | 파일 ID |

### `nbicgazsalnfamoyp` — 의원친선협회 명단
- **건수**: 42
- **수집 전략**: 전체 1회 호출 (none)

| 컬럼명 | 설명 |
|---------|------|
| `RPT_NO` | 보고서 번호 |
| `RPT_TITLE` | 보고서 제목 |
| `STND_DT` | 기준일 |
| `WRT_NM` | 작성자 |
| `UNIT_CD` | 대수 코드 |
| `UNIT_NM` | 대수명 |
| `FILE_ID` | 파일 ID |

### `nahfbzwvatmaxscwq` — 국회의원 겸직 결정 내역
- **건수**: 575
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 의원의 영리업무 겸직 허가/불허 내역.

| 컬럼명 | 설명 |
|---------|------|
| `ORD_NUM` | 순번 |
| `YR` | 연도 |
| `OPB_DAY` | 공표일 |
| `PN` | 의원명 |
| `CCOF_INST_NM` | 겸직 기관명 |
| `PSIT_NM` | 직위명 |
| `CCOF_PSB_YN_CD` | 겸직 가능 여부 |

### `nnzoijvcaiexypqaf` — 연구단체 활동 실적
- **건수**: 41
- **수집 전략**: 전체 1회 호출 (none)
- **설명**: 연구단체 활동 실적 요약.

| 컬럼명 | 설명 |
|---------|------|
| `DIV` | 구분 |
| `POL_RSC_RPT` | 정책연구보고서 |
| `LEG_INIT` | 입법발의 |
| `SEMINAR` | 세미나 |
| `CONF` | 회의 |
| `RESC` | 연구 |
| `DIV2` | 구분2 |

---

## 11. 연구/보고서

### `nfvmtaqoaldzhobsw` — 소규모 연구용역 결과보고서
- **건수**: 1,786
- **수집 전략**: 대수별 (age_unit, `UNIT_CD`)
- **설명**: 국회 소규모 연구용역 보고서 목록.

| 컬럼명 | 설명 |
|---------|------|
| `RPT_NO` | 보고서 번호 |
| `RPT_TITLE` | 보고서 제목 |
| `YEAR` | 연도 |
| `RG_DE` | 등록일 |
| `UNIT_CD` | 대수 코드 |
| `UNIT_NM` | 대수명 |
| `ASBLM_NM` | 의뢰인 |
| `QUARTER` | 분기 |
| `DIV_NM` | 구분 |
| `FILE_ID` | 파일 ID |

### `ncrwiahparxrpodcv` — 연구단체 연구활동 보고서
- **건수**: 1,740
- **수집 전략**: 대수별 (age, `REGDAESU`)
- **설명**: 의원 연구단체 연구활동 결과보고서.

| 컬럼명 | 설명 |
|---------|------|
| `REGDAESU` | 등록 대수 |
| `REPORT_TITLE` | 보고서 제목 |
| `YEAR` | 연도 |
| `RE_NAME` | 연구단체명 |
| `PDF_DOWN_URL` | PDF 다운로드 URL |
| `REPORT_CLASSIFICATION_NM` | 보고서 분류 |

---

## 12. 공통 사항

### 내부 컬럼
모든 테이블에 `_task_key` 컬럼이 존재합니다. 이는 수집 단위를 식별하는 내부 키로, 분석 시 무시해도 됩니다.

### 데이터 타입
모든 컬럼은 `VARCHAR` 타입으로 저장됩니다. 숫자/날짜 값은 사용 시 캐스팅이 필요합니다.

```sql
-- 예: 대수를 정수로 변환
SELECT CAST(AGE AS INTEGER) FROM ncocpgfiaoituanbr;

-- 예: 날짜를 DATE로 변환
SELECT CAST(PROPOSE_DT AS DATE) FROM billrcp WHERE PROPOSE_DT IS NOT NULL;
```

### 수집 범위
- **기본 범위**: 제13대~제22대 (1988~현재)
- **의안 접수목록(billrcp)**: 제헌~22대 전체 포함
- **일부 API**: 연도 기반 수집으로 2000년 이후 데이터

### 조인 관계

```
nwvrqwxyaytdsfvhu (의원 인적사항)
  └─ MONA_CD ─── nzmimeepazxkubdpn (발의법률안) .RST_MONA_CD
  └─ MONA_CD ─── nojepdqqaweusdfbi (표결정보)   .MONA_CD
  └─ MONA_CD ─── negnlnyvatsjwocar (SNS정보)    .MONA_CD
  └─ MONA_CD ─── nktulghcadyhmiqxi (위원회 위원) .MONA_CD

billrcp (의안 접수목록)
  └─ BILL_ID ─── billinfodetail   (의안 상세)     .BILL_ID
  └─ BILL_ID ─── ncocpgfiaoituanbr (표결현황)     .BILL_ID
  └─ BILL_ID ─── nojepdqqaweusdfbi (표결정보)     .BILL_ID
  └─ BILL_ID ─── vconfbillconflist (의안별 회의록) .BILL_ID
  └─ BILL_ID ─── nqfvrbsdafrmuzixe (의정활동)     .BILL_ID

nzbyfwhwaoanttzje (본회의 회의록)
  └─ CONF_ID ─── vconfdetail      (회의록 상세)   .CONF_ID
  └─ CONF_ID ─── vconfbillconflist (의안별 회의록) .CONF_ID

vconfsubcconflist (소위원회 회의록)
  └─ CONF_ID ─── vconfdetail      (회의록 상세)   .CONF_ID
```

### Phase 2 테이블
아래 테이블은 Phase 1 테이블의 ID를 참조하여 개별 조회(lookup)로 수집됩니다:

| Phase 2 테이블 | 참조 테이블 | 조회키 |
|----------------|------------|--------|
| `billinfodetail` | `billrcp` | `BILL_ID` |
| `nojepdqqaweusdfbi` | `ncocpgfiaoituanbr` | `BILL_ID` + `AGE` |
| `vconfdetail` | `vconfsubcconflist` | `CONF_ID` |
| `vconfbillconflist` | `billrcp` | `BILL_ID` |

### 수집 진행 관리
`_progress` 테이블에 각 task의 수집 상태가 기록됩니다:

| 컬럼명 | 설명 |
|---------|------|
| `task_key` | 수집 단위 키 (PK) |
| `api_id` | API ID |
| `name_kr` | API 한글명 |
| `row_count` | 수집 건수 |
| `fetched_at` | 수집 시각 |

`_progress`는 task별 **최신 상태만** 유지합니다(`INSERT OR REPLACE`). 실행별 이력은 아래 감사 로그를 보세요.

### DB 변경 이력 — `data/_audit/db_updates.jsonl` (2026-07-27 도입)

4개 DuckDB(`assembly_raw`, `assembly_analysis`, `news`, `news_analysis`)에 대한 **실행별 변경 기록**. DB 안이 아니라 사이드카 JSONL 파일에 쌓입니다(이유는 [db_audit.py](db_audit.py) 모듈 docstring). 한 줄 = 이벤트 1건:

| event | 언제 | 주요 필드 |
|-------|------|-----------|
| `run_start` | writer 스크립트 시작 | `run_id`, `db`, `script`, `argv`, `started_at`, `git_sha`, `host`, `pid` |
| `run_end` | 종료(정상·예외·중단 모두) | `status`(`ok`/`error`/`interrupted`/`exit:N`), `duration_s`, `changed_tables`, `tables[]`, `rewritten_tables[]`, `content_hash_skipped[]`, `collect_tasks[]`, `note`, `error` |
| `external_change` | 계측 밖 변경 탐지 | `detected_at`, `since`, `tables[]` |

`tables[]` 원소: `table`, `change`, `rows_before`, `rows_after`, `delta`, `touched`(이번 run이 실제로 기록한 행 수), `ts_column`, `rebuilt`(재작성 사유), `content_cmp`, `content_skip`.

`change` 값:

| change | 뜻 |
|--------|-----|
| `rows_added` / `rows_removed` | 행 수가 늘거나 줄었다 |
| `rows_updated` | 행 수는 그대로인데 이번 run 이 남긴 write 타임스탬프가 있다 |
| `content_changed` | **행 수·스키마가 그대로인데 내용 지문이 달라졌다** (2026-08-02 도입) |
| `content_unknown` | 물리적으로 다시 쓰긴 했는데 내용 비교가 불가능했다 (비용 초과로 해시 생략·해시 실패·DuckDB 버전 변경). "변경 없음"이 아니라 "모른다" |
| `rewritten_identical` | 다시 썼으나 내용은 완전히 동일 (재수집했는데 데이터가 안 바뀐 경우). 실질 변경이 아니라 `changed_tables` 에 세지 않고 `rewritten_tables[]` 로 뺀다 |
| `schema_changed` | 컬럼 추가·타입 변경 |
| `table_created` / `table_dropped` | 테이블 생성·삭제 |
| `table_rebuilt` | 스크립트가 `mark_rebuilt()` 로 사유를 붙인 전면 재작성 |

`content_hash_skipped[]` 는 비용 예산(`config.AUDIT_CONTENT_HASH_BUDGET_S`, 기본 1.0초/테이블)을 넘겨 내용 해시를 생략한 테이블 목록입니다. 현재 raw DB에서는 `document_text`(8.3초)·`bill_text`(2.0초) 둘뿐이고, 이 둘은 write 타임스탬프와 저장 지문이 따로 덮습니다. 판정 원리는 [db_audit.py](db_audit.py) 모듈 docstring 참조.

`run_start`만 있고 대응하는 `run_end`가 없으면 그 실행은 SIGKILL 등으로 죽은 것입니다.

```sql
-- 최근 변경 20건 (MCP query 도구에서도 그대로 동작)
SELECT script, argv, status, changed_tables, tables
FROM read_json_auto('/home/jays0967/assembly_data/data/_audit/db_updates.jsonl')
WHERE event = 'run_end' AND changed_tables > 0
ORDER BY finished_at DESC LIMIT 20;
```

CLI: `python db_audit.py --log | --runs | --check | --selftest`

---

## 13. AI 정책 분석 통합 테이블 (Phase 3~5 도입)

이 섹션의 테이블·뷰는 `PLAN_db_consolidation.md` 실행으로 (계획서는 git history에 보존)
2026-04-18에 추가된 통합 데이터 레이어입니다. 외부 JSON 파일에서 DB로 이관 완료.

### `bill_text` — 법안 원문
[`collect/download_bills.py`](collect/download_bills.py) (옛 seed 스크립트 `migrate_bill_text.py`는 git history에 보관)
가 채움. PDF는 파일시스템 유지, 텍스트만 DB.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `bill_id` | VARCHAR PK | `billrcp.BILL_ID` 와 동일 |
| `age` | INTEGER NOT NULL | 19~22 |
| `reason_and_content` | TEXT | 제안이유 및 주요내용 (parse_bill_sections 결과) |
| `full_text` | TEXT | fitz 추출 전체 텍스트 |
| `pdf_path` | VARCHAR | 원본 PDF 절대경로 |
| `extractor_version` | VARCHAR DEFAULT 'fitz-1.0' | 텍스트 추출 도구 버전 |
| `extracted_at` | TIMESTAMP | 추출 시각 |

행수: 77,104 (age 19=15,426 / 20=21,593 / 21=23,639 / 22=16,446).

### `bill_classifications` — 10-속성 분류 결과
[`classify_bills.py`](classify_bills.py) (옛 seed 스크립트 `migrate_bill_classifications.py`는 git history에 보관).

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `bill_id` | VARCHAR | KR=`PRC_*`, US=`H.R. n` / `S. n`, EU=조문번호/수정안번호 |
| `source` | VARCHAR | `kr_19`/`kr_20`/`kr_21`/`kr_22`/`us_118`/`us_119`/`eu_act`/`eu_amendments` |
| `prompt_version` | VARCHAR | `prompt_versions.version` 참조 |
| `primary_attr` | VARCHAR | Carvão 10-속성 1차 |
| `secondary_attr` | VARCHAR | 2차 |
| `tertiary_attr` | VARCHAR | 3차 |
| `title` | VARCHAR | 분류 시점의 법안명 |
| `classified_at` | TIMESTAMP | 분류 시각 |

PK = `(bill_id, source, prompt_version)`. 같은 법안의 여러 prompt 버전 공존 가능.

### `bill_ai_filter` — KR Stage-2 GPT 필터
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `bill_id` | VARCHAR PK | KR `billrcp.BILL_ID` |
| `age` | INTEGER NOT NULL | 19~22 |
| `classification` | VARCHAR | `core` / `adjacent` / `unrelated` |
| `is_ai_bill` | BOOLEAN | core+adjacent → true |
| `gpt_reason` | TEXT | 1문장 한국어 이유 |
| `ai_provisions` | TEXT | adjacent일 경우 AI 관련 조항 요약 |
| `filtered_at` | TIMESTAMP | 분류 시각 |

행수: 330 (kr_19=5 / 20=17 / 21=67 / 22=241).

### `prompt_versions` — 프롬프트 버전 레지스트리
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `version` | VARCHAR PK | `v{major}_{lang}_{YYYYMMDD}` |
| `description` | TEXT | 한 줄 설명 |
| `released_at` | TIMESTAMP | 등록 시각 |

현재 등록: `v2_en_20260418` (영문 SYSTEM_PROMPT). 프롬프트 변경 시 [`classify_bills.py`](classify_bills.py)
의 `PROMPT_VERSION` 상수를 새 값으로 수정하면 자동 등록됨.

### `v_bill_classifications_current` (뷰)
가장 최신 `prompt_version`만 노출. 일상 쿼리는 이걸 사용.

### `v_kr_bills_analysis` (뷰)
KR 법안 분석용 통합 뷰. `v_bill` LEFT JOIN `bill_text` LEFT JOIN
`v_bill_classifications_current` (source LIKE 'kr_%') LEFT JOIN `bill_ai_filter`.
[`bill_loaders.load_kr_bills`](bill_loaders.py)가 이 뷰를 사용.

### `document_text` — PDF/HWP 추출 결과 통합 (2026-04-19)
[`collect/download_documents.py`](collect/download_documents.py) (옛 seed 스크립트 `migrate_legacy_docs.py`는 git history에 보관).
6개 API 테이블의 첨부 PDF·HWP를 받아 fitz/hwp5txt로 텍스트화.

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `doc_id` | VARCHAR | 소스별 자연키 (FILE_ID / CONF_ID / 등) |
| `source` | VARCHAR | `research` / `report` / `minutes_plenary` / `minutes_committee` / `minutes_committee_of_whole` / `minutes_subcommittee` |
| `source_table` | VARCHAR | 원천 API 테이블명 |
| `age` | INTEGER | 대수 |
| `title` | VARCHAR | 보고서 제목 / 회의명 |
| `author` | VARCHAR | 발의 의원 / 연구단체명 |
| `doc_date` | VARCHAR | 회의일 / 보고서 연도 |
| `url` | VARCHAR | 원본 다운로드 URL |
| `file_format` | VARCHAR | `pdf` / `hwp` / `hwpx` / `unknown` (매직 바이트 감지) |
| `file_path` | VARCHAR | 로컬 raw 파일 경로 (`data/bills_kr/docs/{source}/`) |
| `full_text` | TEXT | 추출된 전체 텍스트 |
| `text_length` | INTEGER | 문자 수 |
| `status` | VARCHAR | `extracted_ok` / `url_error` / `url_404` / `format_unsupported` / `extract_failed` / `no_url` |
| `error_message` | TEXT | 실패 이유 요약 |
| `extractor_version` | VARCHAR | `fitz-1.0` / `hwp5txt-0.1` / `hwpx-xml-0.1` / `legacy-unknown` |
| `fetched_at` / `extracted_at` | TIMESTAMP | |

**PK = `(doc_id, source)`** — 같은 `doc_id` 숫자가 다른 소스에서 충돌 가능하므로 분리.

**커버리지 (2026-04-19 1차 실행 후)**:

| source | 테이블 | 건수 | 평균 길이 | 상태 |
|--------|--------|------|-----------|------|
| research | nfvmtaqoaldzhobsw | 295 | 53K | 레거시 seed만 (URL 도출 불가) |
| report | ncrwiahparxrpodcv | 1,948 | 23K | 전체 완료 |
| minutes_plenary | nzbyfwhwaoanttzje | 1,832 (-3 url_error) | 90K | 완료 |
| minutes_committee_of_whole | ngytonzwavydlbbha | 8 | 55K | 완료 |
| minutes_subcommittee | vconfsubcconflist | 189 | 75K | 완료 |
| minutes_committee | ncwgseseafwbuheph | 22,275 (-8 url_error) | 75K | 완료 |
| **합계** | — | **26,547** | — | **extracted_ok 26,536 / url_error 11** |

url_error 11건은 모두 `record.assembly.go.kr` 서버가 119바이트 placeholder를 반환하는 비가용 ID (실제 회의록 없음).

원본 raw 파일(~25GB PDF)은 `data/bills_kr/docs/{source}/`에 유지 (gitignore). 파일 포맷은 매직 바이트로 감지하며, 이번 실행에서는 전부 PDF — HWP 샘플 확보되면 `hwp5txt` CLI 자동 디스패치.

`research` 소스의 `nfvmtaqoaldzhobsw` (소규모 연구용역)는 URL 컬럼이 없고 `FILE_ID`만 존재. 국회 포털 B0000108 상세 페이지가 첨부를 노출하지 않아 URL 도출 불가. 레거시 `data/txt/*/research/` 의 295건만 seed로 보존 (가능해지면 추후 확장).

---

## AGE 컬럼 출처표

[`config.py`](config.py)의 ApiSpec 선언과 동일. `age_behavior`별로 정렬.

`age` 컬럼은 모든 per_age/by_date/by_bill_id 테이블에 INTEGER로 존재하며
NULL이 없음 (Phase 6 [`collect/validate_collection.py`](collect/validate_collection.py)가 보장).
`current_only` 테이블은 모두 `age = 22` 단일값.

### per_age (23 테이블)
| 테이블 | 한글명 | age 출처 |
|--------|--------|----------|
| nyzrglyvagmrypezq | 위원회 경력 | column:PROFILE_UNIT_CD |
| nzmimeepazxkubdpn | 발의법률안 | param:AGE |
| nuvypcdgahexhvrjt | 상임위 활동 | param:AGE (collector가 __AGE_n로 인코딩) |
| numwhtqhavaqssfle | 연구단체 등록 | param:AGE |
| nojepdqqaweusdfbi | 본회의 표결정보 | param:AGE |
| ncocpgfiaoituanbr | 의안별 표결현황 | param:AGE |
| BILLRCP | 의안 접수목록 | column:ERACO (제N대 / N대 국회 / 제헌 / 특수기관) |
| nzivskufaliivfhpb | 역대 의안 통계 | column:ERACO ("N대 국회" 형식) |
| ncryefyuaflxnqbqo | 청원 처리현황 | param:AGE |
| nwbpacrgavhjryiph | 본회의 처리안건 | param:AGE |
| nrvsawtaauyihadij | 인사청문회 | column:AGE |
| nqfvrbsdafrmuzixe | 날짜별 의정활동 | param:AGE |
| ngytonzwavydlbbha | 전원위 회의록 | param:AGE |
| nzbyfwhwaoanttzje | 본회의 회의록 | param:AGE |
| ncwgseseafwbuheph | 위원회 회의록 | param:AGE |
| VCONFSUBCCONFLIST | 소위원회 회의록 | column:ERACO |
| VCONFDETAIL | 회의록 상세 | column:ERACO |
| VCONFBILLCONFLIST | 의안별 회의록 | column:ERACO |
| nxcxrdmpaonzzbkic | 외교협의회 | column:UNIT_CD |
| nbicgazsalnfamoyp | 친선협회 | column:UNIT_CD |
| nahfbzwvatmaxscwq | 겸직 결정 | column:ORD_NUM |
| nnzoijvcaiexypqaf | 연구단체 활동 실적 | column:DIV |
| nfvmtaqoaldzhobsw | 연구용역 보고서 | column:UNIT_CD |
| ncrwiahparxrpodcv | 연구단체 연구활동 | param:AGE |

### current_only (9 테이블, 모두 age=22)
| 테이블 | 한글명 | age 출처 |
|--------|--------|----------|
| nwvrqwxyaytdsfvhu | 의원 인적사항 | constant:22 |
| nexgtxtmaamffofof | 의원 이력 | column:UNIT_CD (실측값 22 단일) |
| negnlnyvatsjwocar | SNS 정보 | constant:22 |
| nvqbafvaajdiqhehi | 청원 계류현황 | constant:22 |
| nepjpxkkabqiqpbvk | 정당 의석수 | constant:22 |
| nxrvzonlafugpqjuh | 위원회 현황 | constant:22 |
| nktulghcadyhmiqxi | 위원회 위원 명단 | constant:22 |
| ndiwuqmpambgvnfsj | 위원회 계류법률안 | constant:22 |

### by_date (4 테이블, 5월 30일 임기 시작 경계로 매핑)
| 테이블 | 한글명 | 날짜 컬럼 |
|--------|--------|-----------|
| nbqbmccpamsvwebkn | 정책 세미나 | HOST_DT |
| npbzvuwvasdqldskm | 기자회견 | TAKING_DATE |
| nztwkhgzakucszgls | 사업예산 | YR (연도, 6월 30일 기준 매핑) |
| nmfcjtvmajsbhhckf | 의정보고서 | PUBLISH_DT |

### by_bill_id (1 테이블)
| 테이블 | 한글명 | 조인 |
|--------|--------|------|
| billinfodetail | 의안 상세 | join: billrcp.BILL_ID → ERACO 파싱 |

### BILLRCP 특수 ERACO 값 (음수 age)
일반 `WHERE age >= 1` 쿼리는 이들을 자동 배제:

| ERACO 원문 | age | 비고 |
|-----------|-----|------|
| `국가보위입법회의` | -3 | 1980-1981 |
| `국가재건최고회의` | -2 | 1961-1963 |
| `비상국무회의` | -1 | 1972-1973 |
| `제헌` | 1 | 1948 (제헌국회를 1대로 매핑) |

---

## 14. 한국 도메스틱 뉴스 (`data/news/news.duckdb`)

2026-05-21에 도입. Open API와 무관한 별도 DB. 외부 라이선스로 입수한 6개 매체 정식 아카이브.

### news_articles
| 컬럼 | 타입 | 설명 |
|------|------|------|
| `news_id` | TEXT (PK) | 매체별 유니크 ID (예: `08100101.20180101105910001`). 매체 prefix + 타임스탬프 인코딩 |
| `title` | TEXT | 기사 제목 (plain text) |
| `content` | TEXT | 기사 본문 전문 (plain text, `\n` 포함) |
| `dateline` | TIMESTAMP WITH TIME ZONE | 송고 시각 |
| `published_at` | TIMESTAMP WITH TIME ZONE | 발행 시각 (date만 의미 있고 time은 0) |
| `enveloped_at` | TIMESTAMP WITH TIME ZONE | 봉투(전달) 시각 |
| `provider` | TEXT | `KBS` / `MBC` / `SBS` / `YTN` / `중앙일보` / `한겨레` |
| `byline` | TEXT | 기자명 / 이메일 |
| `provider_link_page` | TEXT | 원문 URL |
| `category` | JSON | 카테고리 태그 배열 (예: `["경제>산업_기업", "경제>반도체"]`) |
| `category_incident` | JSON | 사건 카테고리 (대부분 `[]`) |
| `hilight` | TEXT | 검색 강조 스니펫 (`<b>...</b>` 포함) |
| `printing_page` | TEXT | 지면 정보 (대부분 빈 문자열) |
| `ingested_at` | TIMESTAMP | 적재 시각 (자동) |

인덱스: `provider`, `published_at`, `(provider, published_at)`.

### 매체·연도 분포 (2026-05-21 적재 기준)
| provider | rows |
|----------|------|
| 중앙일보 | 48,019 |
| YTN | 37,260 |
| MBC | 33,144 |
| 한겨레 | 18,713 |
| KBS | 17,659 |
| SBS | 3,091 |
| **총합** | **157,886** |

기간: 2018-01-01 ~ 2026-03-31 (`published_at` 기준).

### 적재 스크립트
[collect/build_news_db.py](collect/build_news_db.py) — `data/news/raw_news_archive/{provider}/{year}/{month}/{day}/*.json` walk → batch insert. `INSERT OR IGNORE` 로 idempotent.

원본 JSON은 `data/news/raw_news_archive/` (gitignored, ~665 MB) 그대로 보존 — 향후 스키마 변경 시 재적재 가능.

### AI 키워드 필터링·10속성 분류
이번 reorg에서는 미수행. 후속 작업으로 `analyze/`에 별도 스크립트 추가 예정.
