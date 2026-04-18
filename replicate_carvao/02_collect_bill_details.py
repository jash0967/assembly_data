"""Brennan Center 118th Congress AI 법안 154건의 상세 정보를 Congress.gov API로 수집.

수집 항목:
- 기본 정보 (policy area, origin chamber 등)
- Sponsors + Cosponsors (정당, 주, 지구)
- Committees
- Actions (입법 진행 이력)
- Summaries (CRS 요약)
- Text versions (법안 전문 URL)

Usage:
    python replicate_carvao/02_collect_bill_details.py
"""
import json
import os
import re
import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.environ.get("CONGRESS_API_KEY", "")
BASE_URL = "https://api.congress.gov/v3"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INPUT_FILE = os.path.join(DATA_DIR, "brennan_118th_bills.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "bills_detail.json")
TEXT_DIR = os.path.join(DATA_DIR, "bills_text")

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json"})

RATE_DELAY = 0.8  # seconds between requests (~4500 req/hr, under 5000 limit)


def api_get(endpoint: str, params: dict = None) -> dict | None:
    """Congress.gov API GET with retry."""
    if params is None:
        params = {}
    params["api_key"] = API_KEY
    params["format"] = "json"

    for attempt in range(3):
        try:
            resp = SESSION.get(f"{BASE_URL}{endpoint}", params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                print(f"  Rate limited, waiting 10s...")
                time.sleep(10)
                continue
            else:
                print(f"  {resp.status_code} for {endpoint}")
                return None
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(2)
    return None


def parse_bill_id(bill_id: str) -> tuple[str, int]:
    """'H.R. 4814' -> ('hr', 4814), 'S. 1234' -> ('s', 1234)"""
    bill_id = bill_id.strip()
    # Normalize "H.R " -> "H.R." etc.
    bill_id = re.sub(r"^H\.R\s+", "H.R. ", bill_id)
    if bill_id.startswith("H.R."):
        return "hr", int(bill_id.replace("H.R.", "").strip())
    elif bill_id.startswith("S."):
        return "s", int(bill_id.replace("S.", "").strip())
    elif bill_id.startswith("H.J.Res."):
        return "hjres", int(bill_id.replace("H.J.Res.", "").strip())
    elif bill_id.startswith("S.J.Res."):
        return "sjres", int(bill_id.replace("S.J.Res.", "").strip())
    else:
        raise ValueError(f"Unknown bill format: {bill_id}")


def collect_bill_detail(bill_type: str, bill_number: int) -> dict:
    """하나의 법안에 대한 모든 상세 정보 수집."""
    detail = {}

    # 1. Basic info
    data = api_get(f"/bill/118/{bill_type}/{bill_number}")
    time.sleep(RATE_DELAY)
    if data and "bill" in data:
        b = data["bill"]
        detail["title"] = b.get("title", "")
        detail["policy_area"] = (b.get("policyArea") or {}).get("name", "")
        detail["origin_chamber"] = b.get("originChamber", "")
        detail["introduced_date"] = b.get("introducedDate", "")
        detail["latest_action"] = (b.get("latestAction") or {}).get("text", "")
        detail["latest_action_date"] = (b.get("latestAction") or {}).get("actionDate", "")
        detail["update_date"] = b.get("updateDate", "")
        detail["congress_url"] = b.get("url", "")  # congress.gov link

    # 2. Sponsors (from bill detail, primary sponsor)
    if data and "bill" in data:
        sponsors_list = data["bill"].get("sponsors", [])
        if sponsors_list:
            s = sponsors_list[0]
            detail["sponsor"] = {
                "bioguide_id": s.get("bioguideId", ""),
                "name": f"{s.get('lastName', '')}, {s.get('firstName', '')}",
                "party": s.get("party", ""),
                "state": s.get("state", ""),
                "district": s.get("district", ""),
                "is_by_request": s.get("isByRequest", ""),
            }

    # 3. Cosponsors
    cosponsor_data = api_get(f"/bill/118/{bill_type}/{bill_number}/cosponsors", {"limit": 250})
    time.sleep(RATE_DELAY)
    cosponsors = []
    if cosponsor_data and "cosponsors" in cosponsor_data:
        for c in cosponsor_data["cosponsors"]:
            cosponsors.append({
                "bioguide_id": c.get("bioguideId", ""),
                "name": f"{c.get('lastName', '')}, {c.get('firstName', '')}",
                "party": c.get("party", ""),
                "state": c.get("state", ""),
                "district": c.get("district", ""),
                "sponsorship_date": c.get("sponsorshipDate", ""),
                "is_original": c.get("isOriginalCosponsor", False),
            })
    detail["cosponsors"] = cosponsors
    detail["cosponsor_count"] = len(cosponsors)

    # 4. Committees
    committee_data = api_get(f"/bill/118/{bill_type}/{bill_number}/committees")
    time.sleep(RATE_DELAY)
    committees = []
    if committee_data and "committees" in committee_data:
        for cm in committee_data["committees"]:
            committees.append({
                "name": cm.get("name", ""),
                "chamber": cm.get("chamber", ""),
                "type": cm.get("type", ""),
            })
    detail["committees"] = committees

    # 5. Actions (full history)
    actions_data = api_get(f"/bill/118/{bill_type}/{bill_number}/actions", {"limit": 250})
    time.sleep(RATE_DELAY)
    actions = []
    if actions_data and "actions" in actions_data:
        for a in actions_data["actions"]:
            actions.append({
                "date": a.get("actionDate", ""),
                "text": a.get("text", ""),
                "type": a.get("type", ""),
                "action_code": a.get("actionCode", ""),
                "chamber": (a.get("sourceSystem") or {}).get("name", ""),
            })
    detail["actions"] = actions

    # 6. Summaries
    summary_data = api_get(f"/bill/118/{bill_type}/{bill_number}/summaries")
    time.sleep(RATE_DELAY)
    summaries = []
    if summary_data and "summaries" in summary_data:
        for s in summary_data["summaries"]:
            summaries.append({
                "action_date": s.get("actionDate", ""),
                "action_desc": s.get("actionDesc", ""),
                "text": s.get("text", ""),
                "update_date": s.get("updateDate", ""),
            })
    detail["summaries"] = summaries

    # 7. Text versions
    text_data = api_get(f"/bill/118/{bill_type}/{bill_number}/text")
    time.sleep(RATE_DELAY)
    text_versions = []
    if text_data and "textVersions" in text_data:
        for tv in text_data["textVersions"]:
            formats = tv.get("formats", [])
            txt_url = ""
            for fmt in formats:
                if fmt.get("type") == "Formatted Text":
                    txt_url = fmt.get("url", "")
                    break
            if not txt_url:
                for fmt in formats:
                    if fmt.get("type") == "PDF":
                        txt_url = fmt.get("url", "")
                        break
            text_versions.append({
                "type": tv.get("type", ""),
                "date": tv.get("date", ""),
                "url": txt_url,
            })
    detail["text_versions"] = text_versions

    return detail


def main():
    os.makedirs(TEXT_DIR, exist_ok=True)

    with open(INPUT_FILE, encoding="utf-8") as f:
        brennan_bills = json.load(f)

    print(f"Brennan Center 118th AI Bills: {len(brennan_bills)}건")
    print(f"API Key: {API_KEY[:8]}...")
    print()

    # Load existing progress
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            results = json.load(f)
        print(f"기존 수집: {len(results)}건, 이어서 진행")
    else:
        results = {}

    for i, bill in enumerate(brennan_bills, 1):
        bill_id = bill["bill_id"]

        if bill_id in results:
            continue

        try:
            bill_type, bill_number = parse_bill_id(bill_id)
        except ValueError as e:
            print(f"  [{i}/{len(brennan_bills)}] SKIP: {e}")
            continue

        print(f"[{i}/{len(brennan_bills)}] {bill_id}...", end=" ", flush=True)

        detail = collect_bill_detail(bill_type, bill_number)
        detail["bill_id"] = bill_id
        detail["bill_type"] = bill_type
        detail["bill_number"] = bill_number
        detail["brennan_title"] = bill["title"]
        detail["brennan_sponsor"] = bill["sponsor"]
        detail["brennan_committee"] = bill["committee"]
        detail["brennan_summary"] = bill["summary"]

        results[bill_id] = detail

        # cosponsor/action counts for progress display
        nc = detail.get("cosponsor_count", 0)
        na = len(detail.get("actions", []))
        ns = len(detail.get("summaries", []))
        nt = len(detail.get("text_versions", []))
        print(f"co={nc} act={na} sum={ns} txt={nt}")

        # Save every 10 bills
        if i % 10 == 0:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"  (saved {len(results)}/{len(brennan_bills)})")

    # Final save
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n수집 완료: {len(results)}건")
    print(f"저장: {OUTPUT_FILE}")

    # Stats
    with_summary = sum(1 for r in results.values() if r.get("summaries"))
    with_text = sum(1 for r in results.values() if r.get("text_versions"))
    print(f"CRS 요약 있음: {with_summary}건")
    print(f"텍스트 버전 있음: {with_text}건")


if __name__ == "__main__":
    main()
