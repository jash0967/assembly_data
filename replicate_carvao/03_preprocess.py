"""Step 3: 데이터 전처리 — Bill Progression Stage 분류, Sponsor/Cosponsor 분석.

논문 Appendix II (p.90-95)의 방법론을 정확히 재현:
- Bill Progression Stage 0~10 (Figure 18 regex)
- Sponsor party/state 매핑
- Bipartisanship 계산 (Democratic cosponsor ratio)
- Cosponsor 중복 제거

Usage:
    python replicate_carvao/03_preprocess.py
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INPUT_FILE = os.path.join(DATA_DIR, "bills_detail.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "bills_processed.json")


# ─── Bill Progression Stage (Figure 18, p.91) ───

STAGE_KEYWORDS = {
    10: [r"Signed by President"],
    9:  [r"Presented to President"],
    8:  [r"Senate agreed to", r"House agreed to"],
    7:  [r"Received in the Senate", r"Received in the House"],
    6:  [r"Passed Senate", r"Passed House", r"pass the bill"],
    5:  [r"Discharged", r"Considered under suspension of the rules",
         r"Measure laid before Senate"],
    4:  [r"Placed on calendar", r"\bcalendar\b", r"Calendar No\."],
    3:  [r"\bReported\b", r"ordered to be reported", r"\bleas\b", r"\bNays\b"],
    2:  [r"\bMark-up\b", r"\bMarkup\b", r"\bMark up\b"],
    1:  [r"Referred to Subcommittee\b", r"\bSubcommittee\b"],
    0:  [r"\bIntroduced\b", r"Referred to (?!.*Subcommittee).*Committee"],
}


def classify_stage(actions: list[dict]) -> int:
    """법안의 최고 진행 단계를 반환 (0~10).

    논문: "Whenever the last action on the bill did not match a specific stage,
    we referred to the most recent stage that fit the categorization."
    모든 action을 순회하면서 매칭되는 가장 높은 stage를 반환.
    """
    max_stage = -1
    for action in actions:
        text = action.get("text", "")
        for stage in range(10, -1, -1):  # 높은 stage부터 확인
            for pattern in STAGE_KEYWORDS[stage]:
                if re.search(pattern, text, re.IGNORECASE):
                    max_stage = max(max_stage, stage)
                    break
    return max(max_stage, 0)  # 최소 0 (Introduced)


# ─── Bipartisanship 계산 ───

def compute_bipartisanship(sponsor: dict, cosponsors: list[dict]) -> dict:
    """Sponsor + cosponsor 기반 bipartisanship 계산.

    논문: Democratic sponsor ratio = # Democratic sponsors / total sponsors
    - >60% Democratic → Democratic-led (blue)
    - <40% Democratic → Republican-led (red)
    - 40~60% → Bipartisan (purple)
    """
    all_sponsors = []

    # Primary sponsor
    if sponsor:
        all_sponsors.append(sponsor.get("party", ""))

    # Cosponsors (중복 제거 — 논문 p.91: "duplicate metadata... were pruned")
    seen = set()
    for c in cosponsors:
        bio_id = c.get("bioguide_id", "")
        if bio_id and bio_id not in seen:
            seen.add(bio_id)
            all_sponsors.append(c.get("party", ""))

    total = len(all_sponsors)
    dem_count = sum(1 for p in all_sponsors if p == "D")
    rep_count = sum(1 for p in all_sponsors if p == "R")
    ind_count = sum(1 for p in all_sponsors if p not in ("D", "R"))

    dem_ratio = dem_count / total if total > 0 else 0

    if dem_ratio > 0.6:
        category = "Democratic"
    elif dem_ratio < 0.4:
        category = "Republican"
    else:
        category = "Bipartisan"

    return {
        "total_sponsors": total,
        "dem_count": dem_count,
        "rep_count": rep_count,
        "ind_count": ind_count,
        "dem_ratio": round(dem_ratio, 4),
        "category": category,
        "unique_cosponsors": len(seen),
    }


# ─── Chamber 판별 ───

def get_chamber(bill_id: str) -> str:
    if bill_id.startswith("H.R.") or bill_id.startswith("H.J.Res."):
        return "House"
    elif bill_id.startswith("S.") or bill_id.startswith("S.J.Res."):
        return "Senate"
    return "Unknown"


# ─── Sponsor state → 약어 ───

def get_sponsor_state(detail: dict) -> str:
    """Sponsor의 state를 반환."""
    sponsor = detail.get("sponsor", {})
    if isinstance(sponsor, dict):
        return sponsor.get("state", "")
    return ""


def main():
    with open(INPUT_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    print(f"입력: {len(raw)}건")

    processed = []
    for bill_id, detail in raw.items():
        # Stage 분류
        stage = classify_stage(detail.get("actions", []))

        # Bipartisanship
        bipartisan = compute_bipartisanship(
            detail.get("sponsor", {}),
            detail.get("cosponsors", [])
        )

        # Sponsor info
        sponsor = detail.get("sponsor", {})
        sponsor_party = sponsor.get("party", "") if isinstance(sponsor, dict) else ""
        sponsor_state = get_sponsor_state(detail)
        sponsor_name = sponsor.get("name", "") if isinstance(sponsor, dict) else ""

        # Chamber
        chamber = get_chamber(bill_id)

        # Date
        intro_date = detail.get("introduced_date", "")

        # Month for timeline
        month = ""
        if intro_date:
            try:
                dt = datetime.strptime(intro_date, "%Y-%m-%d")
                month = dt.strftime("%Y-%m")
            except ValueError:
                pass

        # Committees
        committees = detail.get("committees", [])
        primary_committee = committees[0].get("name", "") if committees else ""

        rec = {
            "bill_id": bill_id,
            "bill_type": detail.get("bill_type", ""),
            "bill_number": detail.get("bill_number", 0),
            "title": detail.get("title", "") or detail.get("brennan_title", ""),
            "chamber": chamber,
            "introduced_date": intro_date,
            "month": month,
            "policy_area": detail.get("policy_area", ""),

            # Sponsor
            "sponsor_name": sponsor_name,
            "sponsor_party": sponsor_party,
            "sponsor_state": sponsor_state,

            # Stage
            "stage": stage,
            "latest_action": detail.get("latest_action", ""),
            "latest_action_date": detail.get("latest_action_date", ""),

            # Bipartisanship
            "bipartisan": bipartisan,

            # Counts
            "cosponsor_count": detail.get("cosponsor_count", 0),
            "committee_count": len(committees),
            "primary_committee": primary_committee,
            "summary_count": len(detail.get("summaries", [])),
            "text_version_count": len(detail.get("text_versions", [])),

            # Brennan Center metadata
            "brennan_summary": detail.get("brennan_summary", ""),
        }
        processed.append(rec)

    # Sort by introduced date (newest first, matching paper)
    processed.sort(key=lambda x: x["introduced_date"], reverse=True)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(processed, f, indent=2, ensure_ascii=False)

    print(f"출력: {len(processed)}건 → {OUTPUT_FILE}")

    # ─── 논문 검증용 통계 ───

    print(f"\n{'='*60}")
    print("논문 검증 통계")
    print(f"{'='*60}")

    # Party distribution (Figure 8)
    party_counts = Counter(b["sponsor_party"] for b in processed)
    print(f"\n[Figure 8] Sponsor party:")
    print(f"  Democratic (D): {party_counts.get('D', 0)}  (논문: 99)")
    print(f"  Republican (R): {party_counts.get('R', 0)}  (논문: 50)")
    print(f"  Independent (I): {party_counts.get('I', 0)}  (논문: 1)")

    # Chamber breakdown (Figure 23)
    house = [b for b in processed if b["chamber"] == "House"]
    senate = [b for b in processed if b["chamber"] == "Senate"]
    print(f"\n[Figure 23] By chamber:")
    h_party = Counter(b["sponsor_party"] for b in house)
    s_party = Counter(b["sponsor_party"] for b in senate)
    print(f"  House: D={h_party.get('D',0)} R={h_party.get('R',0)}  (논문: D=49, R=31)")
    print(f"  Senate: D={s_party.get('D',0)} R={s_party.get('R',0)} I={s_party.get('I',0)}  (논문: D=50, R=19, I=1)")

    # States (Figure 10)
    state_counts = Counter(b["sponsor_state"] for b in processed if b["sponsor_state"])
    print(f"\n[Figure 10] States: {len(state_counts)}개  (논문: 36)")
    top5 = state_counts.most_common(5)
    for st, cnt in top5:
        print(f"  {st}: {cnt}", end="")
        if st == "CA":
            print(f"  (논문: 23)", end="")
        print()

    # Bill progression (Figure 30)
    stage_counts = Counter(b["stage"] for b in processed)
    print(f"\n[Figure 30] Bill progression:")
    for s in range(11):
        cnt = stage_counts.get(s, 0)
        if cnt > 0:
            print(f"  Stage {s}: {cnt}")
    past_stage3 = sum(1 for b in processed if b["stage"] >= 4)
    print(f"  Stage 4+ (calendar): {past_stage3}  (논문: 17)")

    # Most active sponsors (Figure 27, 28)
    print(f"\n[Figure 27-28] Most active sponsors:")
    sponsor_counts = Counter(b["sponsor_name"] for b in processed)
    for name, cnt in sponsor_counts.most_common(10):
        party = next((b["sponsor_party"] for b in processed if b["sponsor_name"] == name), "")
        chamber = next((b["chamber"] for b in processed if b["sponsor_name"] == name), "")
        print(f"  {name} [{party}] ({chamber}): {cnt}")

    # Committees (Figure 29)
    print(f"\n[Figure 29] Most active committees:")
    committee_counts = Counter(b["primary_committee"] for b in processed if b["primary_committee"])
    for name, cnt in committee_counts.most_common(8):
        print(f"  {name}: {cnt}")

    # Bipartisanship (Figure 9)
    bipart_cats = Counter(b["bipartisan"]["category"] for b in processed)
    print(f"\n[Figure 9] Bipartisanship:")
    for cat, cnt in bipart_cats.most_common():
        print(f"  {cat}: {cnt}")

    # Monthly distribution (Figure 22)
    month_counts = Counter(b["month"] for b in processed if b["month"])
    print(f"\n[Figure 22] Monthly distribution:")
    for m in sorted(month_counts):
        print(f"  {m}: {month_counts[m]}")


if __name__ == "__main__":
    main()
