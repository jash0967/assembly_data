-- 국회 데이터 분석용 뷰
-- 원본 테이블의 난독화된 이름을 읽기 좋은 뷰로 변환하고,
-- 타입 캐스팅과 컬럼 정리를 수행.
-- 사용: python -c "import duckdb; con=duckdb.connect('data/assembly.duckdb'); con.execute(open('setup_views.sql').read())"

-- ── 의원 마스터 ──────────────────────────────────────────
CREATE OR REPLACE VIEW v_member AS
SELECT
    MONA_CD                         AS mona_cd,        -- 의원 고유코드 (조인키)
    HG_NM                           AS name,           -- 한글 이름
    HJ_NM                           AS name_hanja,     -- 한자 이름
    ENG_NM                          AS name_eng,       -- 영문 이름
    POLY_NM                         AS party,          -- 정당
    ORIG_NM                         AS district,       -- 지역구
    ELECT_GBN_NM                    AS elect_type,     -- 선거구분 (지역구/비례)
    CMIT_NM                         AS committee,      -- 소속 상임위
    CMITS                           AS committees,     -- 소속 위원회 전체
    REELE_GBN_NM                    AS reelect,        -- 재선 구분 (초선/재선/3선...)
    UNITS                           AS terms_list,     -- 당선 대수 목록
    SEX_GBN_NM                      AS gender,         -- 성별
    BTH_DATE                        AS birth_date,     -- 생년월일
    E_MAIL                          AS email
FROM nwvrqwxyaytdsfvhu;

-- ── 발의법률안 ───────────────────────────────────────────
CREATE OR REPLACE VIEW v_bill AS
SELECT
    BILL_ID                         AS bill_id,
    BILL_NO                         AS bill_no,
    BILL_NAME                       AS bill_name,      -- 법안명
    PROPOSER                        AS proposer,       -- 대표발의자
    RST_PROPOSER                    AS lead_proposer,  -- 대표발의 의원
    RST_MONA_CD                     AS lead_mona_cd,   -- 대표발의 의원코드
    MEMBER_LIST                     AS co_proposers,   -- 공동발의 목록
    COMMITTEE                       AS committee,      -- 소관 위원회
    PROPOSE_DT                      AS propose_date,   -- 발의일
    PROC_RESULT                     AS proc_result,    -- 처리 결과
    PROC_DT                         AS proc_date,      -- 처리일
    CMT_PROC_RESULT_CD              AS cmt_result,     -- 위원회 처리결과
    CMT_PROC_DT                     AS cmt_date,       -- 위원회 처리일
    LAW_PROC_RESULT_CD              AS law_result,     -- 법사위 처리결과
    LAW_PROC_DT                     AS law_date,       -- 법사위 처리일
    CAST(AGE AS INTEGER)            AS age             -- 대수
FROM nzmimeepazxkubdpn;

-- ── 의원별 표결 ──────────────────────────────────────────
CREATE OR REPLACE VIEW v_vote AS
SELECT
    MONA_CD                         AS mona_cd,        -- 의원코드
    HG_NM                           AS name,           -- 의원명
    POLY_NM                         AS party,          -- 정당
    ORIG_NM                         AS district,       -- 지역구
    BILL_ID                         AS bill_id,
    BILL_NO                         AS bill_no,
    BILL_NAME                       AS bill_name,
    RESULT_VOTE_MOD                 AS vote_result,    -- 찬성/반대/기권
    VOTE_DATE                       AS vote_date,      -- 표결일
    CURR_COMMITTEE                  AS committee,      -- 소관위원회
    CAST(AGE AS INTEGER)            AS age
FROM nojepdqqaweusdfbi;

-- ── 의안별 표결 집계 ─────────────────────────────────────
CREATE OR REPLACE VIEW v_vote_summary AS
SELECT
    BILL_ID                         AS bill_id,
    BILL_NO                         AS bill_no,
    BILL_NAME                       AS bill_name,
    PROC_DT                         AS proc_date,      -- 처리일
    PROC_RESULT_CD                  AS proc_result,    -- 처리결과
    CURR_COMMITTEE                  AS committee,
    CAST(MEMBER_TCNT AS INTEGER)    AS member_total,   -- 재적
    CAST(VOTE_TCNT AS INTEGER)      AS vote_total,     -- 투표
    CAST(YES_TCNT AS INTEGER)       AS yes_count,      -- 찬성
    CAST(NO_TCNT AS INTEGER)        AS no_count,       -- 반대
    CAST(BLANK_TCNT AS INTEGER)     AS abstain_count,  -- 기권
    CAST(AGE AS INTEGER)            AS age
FROM ncocpgfiaoituanbr;

-- ── 의안 상세정보 ────────────────────────────────────────
CREATE OR REPLACE VIEW v_bill_detail AS
SELECT
    BILL_ID                         AS bill_id,
    BILL_NO                         AS bill_no,
    BILL_NM                         AS bill_name,
    PPSR_KIND                       AS proposer_type,  -- 발의자 유형
    PPSR                            AS proposer,       -- 발의자
    PPSL_DT                         AS propose_date,
    JRCMIT_NM                       AS committee,      -- 소관위
    JRCMIT_PROC_RSLT                AS cmt_result,     -- 위원회 처리결과
    LAW_PROC_RSLT                   AS law_result,     -- 법사위 처리결과
    RGS_CONF_RSLT                   AS plenary_result, -- 본회의 결과
    PROM_LAW_NM                     AS law_name,       -- 공포 법률명
    PROM_DT                         AS promulgation_date -- 공포일
FROM billinfodetail;

-- ── 본회의 회의록 메타 ───────────────────────────────────
CREATE OR REPLACE VIEW v_plenary_conf AS
SELECT
    CONF_ID                         AS conf_id,
    CAST(DAE_NUM AS INTEGER)        AS age,
    CONF_DATE                       AS conf_date,
    TITLE                           AS title,
    PDF_LINK_URL                    AS pdf_url
FROM nzbyfwhwaoanttzje;

-- ── 위원회 회의록 메타 ───────────────────────────────────
CREATE OR REPLACE VIEW v_committee_conf AS
SELECT
    CONF_ID                         AS conf_id,
    CAST(DAE_NUM AS INTEGER)        AS age,
    COMM_NAME                       AS committee,
    CONF_DATE                       AS conf_date,
    TITLE                           AS title,
    PDF_LINK_URL                    AS pdf_url
FROM ncwgseseafwbuheph;

-- ── 회의록 상세 (소위원회) ───────────────────────────────
CREATE OR REPLACE VIEW v_conf_detail AS
SELECT
    CONF_ID                         AS conf_id,
    ERACO                           AS age_str,
    CONF_DT                         AS conf_date,
    CONF_KND                        AS conf_type,      -- 회의 종류
    CMIT_NM                         AS committee,
    SB_CMIT_NM                      AS sub_committee,
    CONF_PLC                        AS place,
    DOWN_URL                        AS pdf_url
FROM vconfdetail;

-- ── 본회의 처리안건 ──────────────────────────────────────
CREATE OR REPLACE VIEW v_plenary_bill AS
SELECT
    BILL_ID                         AS bill_id,
    BILL_NO                         AS bill_no,
    BILL_NM                         AS bill_name,
    BILL_KIND                       AS bill_type,
    PROPOSER                        AS proposer,
    COMMITTEE_NM                    AS committee,
    PROC_RESULT_CD                  AS proc_result,
    CAST(VOTE_TCNT AS INTEGER)      AS vote_total,
    CAST(YES_TCNT AS INTEGER)       AS yes_count,
    CAST(NO_TCNT AS INTEGER)        AS no_count,
    CAST(BLANK_TCNT AS INTEGER)     AS abstain_count,
    CAST(AGE AS INTEGER)            AS age
FROM nwbpacrgavhjryiph;
