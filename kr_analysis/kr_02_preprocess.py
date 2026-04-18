"""한국 국회 Step 2: 전처리 — 진행 단계 분류, 정당/여야 매핑, Bipartisanship.

Usage:
    python replicate_carvao/kr_02_preprocess.py
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import duckdb

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "assembly.duckdb")
INPUT_FILE = os.path.join(DATA_DIR, "kr_ai_bills.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "kr_bills_processed.json")

# ─── 정당 분류 ───
# 미국 분석(Democrat/Republican)과 동일하게 정당명 그대로 사용.
# 한국은 여소야대(국민의힘=여당/소수, 더불어민주당=야당/다수)로
# 여야 라벨이 혼란을 주므로 정당명 기반으로 분석.
MAJOR_PARTIES = ["더불어민주당", "국민의힘"]
MINOR_PARTIES = ["조국혁신당", "개혁신당", "사회민주당", "진보당", "기본소득당"]


def classify_party_bloc(party: str) -> str:
    """정당 → 정당명 그대로 / 소수당 / 무소속."""
    if not party:
        return "미상"
    if party in MAJOR_PARTIES:
        return party
    elif party in MINOR_PARTIES:
        return party
    elif party == "무소속":
        return "무소속"
    else:
        return party


# ─── 법안 진행 단계 ───

PROC_RESULT_STAGE = {
    # 통과
    "원안가결": 6,
    "수정가결": 6,
    "대안가결": 6,
    # 대안반영 → 원안은 폐기되지만 내용은 대안에 반영
    "대안반영폐기": 5,
    # 위원회 단계
    "위원회심사": 2,
    # 폐기/철회
    "폐기": -1,
    "철회": -1,
    "부결": -1,
}


def classify_stage(proc_result: str) -> tuple[int, str]:
    """처리결과 → (stage, stage_label).

    한국 국회 진행 단계:
    0: 발의/접수
    1: 상임위 회부
    2: 상임위/소위 심사 중
    3: 상임위 의결
    4: 법사위 체계자구심사
    5: 대안반영폐기 (내용은 통합법에 반영)
    6: 본회의 의결 (원안/수정가결)
    -1: 폐기/철회/부결
    """
    if not proc_result:
        return 1, "계류중(상임위 회부)"

    proc = proc_result.strip()

    if proc in ("원안가결", "수정가결", "대안가결"):
        return 6, "본회의 의결"
    elif proc == "대안반영폐기":
        return 5, "대안반영폐기"
    elif proc in ("폐기", "임기만료폐기"):
        return -1, "폐기"
    elif proc == "철회":
        return -1, "철회"
    elif proc == "부결":
        return -1, "부결"
    elif "심사" in proc:
        return 2, "심사중"
    else:
        return 1, "계류중"


def compute_bipartisanship(lead_party: str, co_proposers_str: str, con) -> dict:
    """공동발의자 기반 정당 비율 계산.

    미국의 Democratic ratio에 대응하여 대표발의자 소속 정당 비율을 계산.
    lead_party의 비율이 높으면 단일정당 주도, 낮으면 초당적.
    """
    co_names = [n.strip() for n in co_proposers_str.split(",") if n.strip()] if co_proposers_str else []

    # 공동발의자 정당 조회
    parties = [lead_party or "미상"]  # 대표발의자 포함
    for name in co_names:
        row = con.execute("""
            SELECT party FROM v_member WHERE name = ? LIMIT 1
        """, [name]).fetchone()
        if row and row[0]:
            parties.append(row[0])
        else:
            parties.append("미상")

    total = len(parties)
    from collections import Counter as _Counter
    party_counts = _Counter(parties)

    # 대표발의자 소속 정당의 비율
    lead_count = party_counts.get(lead_party, 0) if lead_party else 0
    lead_ratio = lead_count / total if total > 0 else 0

    # 양대 정당 비율
    dem_count = party_counts.get("더불어민주당", 0)
    ppp_count = party_counts.get("국민의힘", 0)
    dem_ratio = dem_count / total if total > 0 else 0

    if lead_ratio > 0.6:
        category = "단일정당주도"
    elif lead_ratio < 0.4:
        category = "초당적"
    else:
        category = "혼합"

    return {
        "total_sponsors": total,
        "lead_party_count": lead_count,
        "lead_party_ratio": round(lead_ratio, 4),
        "dem_count": dem_count,
        "ppp_count": ppp_count,
        "dem_ratio": round(dem_ratio, 4),
        "party_breakdown": dict(party_counts),
        "category": category,
    }


def main():
    with open(INPUT_FILE, encoding="utf-8") as f:
        bills = json.load(f)

    con = duckdb.connect(DB_PATH, read_only=True)
    print(f"입력: {len(bills)}건")

    processed = []
    for bill in bills:
        # 진행 단계
        stage, stage_label = classify_stage(bill.get("proc_result", ""))

        # 정당
        party = bill.get("party", "")
        party_bloc = classify_party_bloc(party)

        # Bipartisanship
        bipartisan = compute_bipartisanship(
            party,
            bill.get("co_proposers", ""),
            con,
        )

        # 월
        propose_date = bill.get("propose_date", "")
        month = propose_date[:7] if len(propose_date) >= 7 else ""

        rec = {
            "bill_id": bill.get("bill_id", ""),
            "bill_name": bill.get("bill_name", ""),
            "lead_proposer": bill.get("lead_proposer", ""),
            "lead_mona_cd": bill.get("lead_mona_cd", ""),
            "party": party,
            "party_bloc": party_bloc,
            "propose_date": propose_date,
            "month": month,
            "committee": bill.get("committee", ""),
            "proc_result": bill.get("proc_result", ""),
            "stage": stage,
            "stage_label": stage_label,
            "cosponsor_count": bill.get("cosponsor_count", 0),
            "bipartisan": bipartisan,
            "ai_mention_count": bill.get("ai_mention_count", 0),
            "gpt_reason": bill.get("gpt_reason", ""),
            "reason_snippet": bill.get("reason_snippet", ""),
        }
        processed.append(rec)

    con.close()

    # Sort by date
    processed.sort(key=lambda x: x["propose_date"])

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(processed, f, indent=2, ensure_ascii=False)

    print(f"출력: {len(processed)}건 → {OUTPUT_FILE}")

    # ─── 검증 통계 ───

    print(f"\n{'='*60}")
    print("한국 국회 AI 법안 통계")
    print(f"{'='*60}")

    # 정당별
    party_counts = Counter(b["party"] for b in processed)
    print(f"\n[정당별 발의]")
    for p, c in party_counts.most_common():
        bloc = classify_party_bloc(p)
        print(f"  {p or '미상':15s} ({bloc}): {c}")

    # 정당 블록
    bloc_counts = Counter(b["party_bloc"] for b in processed)
    print(f"\n[정당별 블록]")
    for bloc, c in bloc_counts.most_common():
        print(f"  {bloc}: {c}")

    # 위원회별
    comm_counts = Counter(b["committee"] for b in processed)
    print(f"\n[위원회별] (상위 10)")
    for cm, c in comm_counts.most_common(10):
        print(f"  {cm or '미상'}: {c}")

    # 진행 단계
    stage_counts = Counter(b["stage_label"] for b in processed)
    print(f"\n[진행 단계]")
    for label, c in stage_counts.most_common():
        print(f"  {label}: {c}")

    # Bipartisanship
    bipart_cats = Counter(b["bipartisan"]["category"] for b in processed)
    print(f"\n[초당성 (대표발의자 소속 정당 비율 기준)]")
    for cat, c in bipart_cats.most_common():
        print(f"  {cat}: {c}")

    # 월별 발의
    month_counts = Counter(b["month"] for b in processed if b["month"])
    print(f"\n[월별 발의]")
    for m in sorted(month_counts):
        print(f"  {m}: {month_counts[m]}")

    # 최다 발의 의원
    proposer_counts = Counter(b["lead_proposer"] for b in processed)
    print(f"\n[최다 발의 의원 (상위 10)]")
    for name, c in proposer_counts.most_common(10):
        party = next((b["party"] for b in processed if b["lead_proposer"] == name), "")
        print(f"  {name} ({party}): {c}")

    # 미국과 비교
    print(f"\n{'='*60}")
    print("한미 비교 요약")
    print(f"{'='*60}")
    print(f"  한국 AI 법안: {len(processed)}건 (22대, 2024.05~)")
    print(f"  미국 AI 법안: 154건 (118th, 2023.01~2025.01)")
    dem_cnt = bloc_counts.get("더불어민주당", 0)
    ppp_cnt = bloc_counts.get("국민의힘", 0)
    print(f"  한국 더불어민주당: {dem_cnt}건 ({dem_cnt/len(processed)*100:.0f}%)")
    print(f"  미국 민주당: 100건 (65%)")
    print(f"  한국 국민의힘: {ppp_cnt}건 ({ppp_cnt/len(processed)*100:.0f}%)")
    print(f"  미국 공화당: 53건 (34%)")


if __name__ == "__main__":
    main()
