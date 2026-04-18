"""119th Congress AI 법안 분석 — 118th와 동일한 파이프라인.

02~06 단계를 119th 데이터로 일괄 실행.

Usage:
    python replicate_carvao/us119_run_all.py
"""
import csv
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import requests
from dotenv import load_dotenv
from openai import OpenAI
from sklearn.feature_extraction.text import TfidfVectorizer, CountVectorizer
from sklearn.decomposition import LatentDirichletAllocation

load_dotenv()

API_KEY = os.environ.get("CONGRESS_API_KEY", "")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TEXT_DIR = os.path.join(DATA_DIR, "us119_bills_text")

SESSION = requests.Session()
client = OpenAI()

# Reuse from 05_nlp_classify.py
POLICY_ATTRIBUTES = {
    "Market efficiency and power concentration (antitrust)": [
        "antitrust", "monopoly", "monopolies", "anticompetitive",
        "market power", "market concentration", "dominant platform",
        "merger", "acquisition", "gatekeeper",
        "self-preferencing", "anti-competitive", "market dominance",
        "interoperability", "portability", "app store",
        "covered platform", "dominant", "compete", "competing",
        "platform", "collusion", "cartel", "pricing",
        "algorithmic pricing", "price fixing",
        "section 230", "immunity", "liability",
        "commission", "enforcement", "ftc",
        "federal trade", "deceptive practice",
        "unfair", "dark pattern", "manipulative design",
    ],
    "Safety": [
        "safety", "safe", "risk assessment", "risk management",
        "hazard", "dangerous", "harm", "harmful",
        "incident", "accident", "red team", "red-team",
        "benchmark", "guardrail", "mitigation", "mitigate",
        "vulnerability", "robustness", "reliable", "reliability",
        "assurance", "secure", "watermark",
        "reporting requirement",
        "risk", "testing", "evaluation",
        "safety standard", "safety review",
        "notification", "warning", "recall",
    ],
    "Responsible and ethical AI": [
        "responsible", "ethical", "ethics", "accountability", "accountable",
        "transparent", "transparency", "explainable", "explainability",
        "bias", "fairness", "discrimination", "equitable",
        "trustworthy", "governance", "oversight",
        "impact assessment", "algorithmic", "automated decision",
        "audit", "auditing", "certification",
        "civil rights", "civil liberties",
    ],
    "National security": [
        "national security", "defense", "military",
        "homeland security", "cybersecurity", "cyber attack",
        "adversary", "adversarial", "china", "chinese",
        "foreign adversary", "threat", "critical infrastructure",
        "classified", "counterintelligence", "espionage", "weapons",
        "autonomous weapon", "department of defense", "pentagon",
        "intelligence community", "national defense",
    ],
    "Industrial policy": [
        "industrial", "manufacturing", "innovation",
        "research and development", "investment", "funding",
        "grant", "subsidy", "semiconductor", "chip", "compute",
        "infrastructure", "workforce development", "stem",
        "national science foundation", "nsf", "nist", "startup",
        "competitiveness", "leadership", "training program",
        "fellowship", "pilot program", "testbed",
    ],
    "Public interest": [
        "consumer", "consumer protection",
        "public health", "healthcare", "health care", "patient",
        "education", "student", "children", "child", "minor",
        "disability", "accessible", "accessibility",
        "housing", "transportation", "agriculture", "environment",
        "energy", "climate", "water", "food", "public service",
        "rural", "underserved", "vulnerable",
    ],
    "Labor": [
        "labor", "worker", "workers", "employment", "employee",
        "job", "jobs", "automation", "displacement",
        "wage", "union", "workplace", "occupational", "hiring",
        "recruitment", "retraining", "reskilling", "gig economy",
        "unemployment", "layoff",
    ],
    "Copyright": [
        "copyright", "intellectual property",
        "creative", "creator", "artist", "author",
        "generative", "generated content", "training data",
        "fair use", "license", "licensing", "royalty", "patent",
        "trademark", "infringement", "synthetic media",
        "likeness", "replica", "voice cloning",
    ],
    "International collaboration": [
        "international cooperation", "global", "multilateral", "bilateral",
        "treaty", "cooperation", "collaboration",
        "alliance", "diplomatic", "united nations",
        "oecd", "nato", "cross-border", "harmonization",
        "foreign government", "foreign country",
    ],
    "Elections": [
        "election", "elections", "electoral", "voter",
        "voting", "campaign", "candidate", "political",
        "deepfake", "disinformation", "misinformation",
        "manipulate", "deceptive", "impersonate", "robocall",
        "political advertising", "political ad", "ballot",
        "democracy", "democratic process",
    ],
}

LEGAL_STOPWORDS = [
    "bill", "act", "section", "subsection", "paragraph", "subparagraph",
    "clause", "title", "chapter", "part", "division", "subtitle",
    "congress", "senate", "house", "representatives", "committee",
    "subcommittee", "secretary", "director", "administrator",
    "agency", "department", "commission", "office", "bureau",
    "united", "states", "america", "federal", "government",
    "shall", "may", "including", "means", "purpose", "purposes",
    "general", "appropriate", "described", "established",
    "amended", "amend", "insert", "strike", "redesignate",
    "paragraph", "subparagraph", "preceding", "following",
    "date", "enactment", "fiscal", "year", "period",
    "report", "submit", "provide", "require", "determine",
    "intelligence", "artificial", "technology", "system", "systems",
    "information", "data", "use", "person", "entity",
    "term", "defined", "covered", "applicable", "relevant",
    "notice", "consent", "rights", "standards",
    "compliance", "framework", "guidelines", "regulatory",
    "disclosure", "service", "program", "plan",
]


def api_get(endpoint, params=None):
    if params is None: params = {}
    params["api_key"] = API_KEY
    params["format"] = "json"
    for _ in range(3):
        try:
            resp = SESSION.get(f"https://api.congress.gov/v3{endpoint}", params=params, timeout=30)
            if resp.status_code == 200: return resp.json()
            elif resp.status_code == 429: time.sleep(10); continue
            else: return None
        except: time.sleep(2)
    return None


def parse_bill_id(bill_id):
    bill_id = re.sub(r"^H\.R\s+", "H.R. ", bill_id.strip())
    if bill_id.startswith("H.R."): return "hr", int(bill_id.replace("H.R.", "").strip())
    elif bill_id.startswith("S."): return "s", int(bill_id.replace("S.", "").strip())
    raise ValueError(f"Unknown: {bill_id}")


def strip_html(html):
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<(?:p|div|br|hr|h[1-6]|li|tr)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&nbsp;", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main():
    os.makedirs(TEXT_DIR, exist_ok=True)

    with open(os.path.join(DATA_DIR, "brennan_119th_bills.json"), encoding="utf-8") as f:
        brennan = json.load(f)
    print(f"119th Congress AI Bills: {len(brennan)}건")

    # ─── Step 1: Collect details + text ───
    detail_file = os.path.join(DATA_DIR, "us119_bills_detail.json")
    if os.path.exists(detail_file):
        with open(detail_file, encoding="utf-8") as f:
            details = json.load(f)
        print(f"기존 상세: {len(details)}건")
    else:
        details = {}

    for i, bill in enumerate(brennan, 1):
        bid = bill["bill_id"]
        if bid in details:
            continue
        try:
            btype, bnum = parse_bill_id(bid)
        except ValueError:
            continue

        print(f"[{i}/{len(brennan)}] {bid}...", end=" ", flush=True)

        d = {}
        # Basic
        data = api_get(f"/bill/119/{btype}/{bnum}")
        time.sleep(0.5)
        if data and "bill" in data:
            b = data["bill"]
            d["title"] = b.get("title", "")
            sponsors = b.get("sponsors", [])
            if sponsors:
                s = sponsors[0]
                d["sponsor_name"] = f"{s.get('lastName','')}, {s.get('firstName','')}"
                d["sponsor_party"] = s.get("party", "")
                d["sponsor_state"] = s.get("state", "")
            d["introduced_date"] = b.get("introducedDate", "")
            d["policy_area"] = (b.get("policyArea") or {}).get("name", "")

        # Cosponsors
        cs = api_get(f"/bill/119/{btype}/{bnum}/cosponsors", {"limit": 250})
        time.sleep(0.5)
        d["cosponsor_count"] = len(cs.get("cosponsors", [])) if cs else 0
        cosponsors = cs.get("cosponsors", []) if cs else []
        d["cosponsors_party"] = [c.get("party", "") for c in cosponsors]

        # Text
        txt = api_get(f"/bill/119/{btype}/{bnum}/text")
        time.sleep(0.5)
        text_url = ""
        if txt and "textVersions" in txt:
            for tv in txt["textVersions"]:
                for fmt in tv.get("formats", []):
                    if fmt.get("type") == "Formatted Text":
                        text_url = fmt.get("url", "")
                        break
                if text_url: break
        d["text_url"] = text_url

        details[bid] = d
        nc = d.get("cosponsor_count", 0)
        has_txt = "Y" if text_url else "N"
        print(f"co={nc} txt={has_txt}")

        if i % 10 == 0:
            with open(detail_file, "w", encoding="utf-8") as f:
                json.dump(details, f, indent=2, ensure_ascii=False)

    with open(detail_file, "w", encoding="utf-8") as f:
        json.dump(details, f, indent=2, ensure_ascii=False)
    print(f"상세 수집: {len(details)}건")

    # ─── Step 2: Download text ───
    print(f"\n=== Download bill text ===")
    for bid, d in details.items():
        safe = bid.replace(" ", "_").replace(".", "")
        path = os.path.join(TEXT_DIR, f"{safe}.txt")
        if os.path.exists(path) and os.path.getsize(path) > 100:
            continue
        url = d.get("text_url", "")
        if not url:
            continue
        try:
            resp = SESSION.get(url, params={"api_key": API_KEY}, timeout=30)
            if resp.status_code == 200:
                text = strip_html(resp.text)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"  {bid}: {len(text):,} chars")
            time.sleep(0.3)
        except:
            pass

    # ─── Step 3: Preprocess ───
    print(f"\n=== Preprocess ===")
    processed = []
    for bid, d in details.items():
        party = d.get("sponsor_party", "")
        all_parties = [party] + d.get("cosponsors_party", [])
        total = len(all_parties)
        dem = sum(1 for p in all_parties if p == "D")
        dem_ratio = dem / total if total else 0
        if dem_ratio > 0.6: cat = "Democratic"
        elif dem_ratio < 0.4: cat = "Republican"
        else: cat = "Bipartisan"

        chamber = "House" if bid.startswith("H.R.") else "Senate"
        intro = d.get("introduced_date", "")
        month = intro[:7] if intro else ""

        processed.append({
            "bill_id": bid,
            "title": d.get("title", ""),
            "chamber": chamber,
            "introduced_date": intro,
            "month": month,
            "sponsor_name": d.get("sponsor_name", ""),
            "sponsor_party": party,
            "sponsor_state": d.get("sponsor_state", ""),
            "cosponsor_count": d.get("cosponsor_count", 0),
            "bipartisan_category": cat,
            "dem_ratio": round(dem_ratio, 4),
        })

    proc_file = os.path.join(DATA_DIR, "us119_bills_processed.json")
    with open(proc_file, "w", encoding="utf-8") as f:
        json.dump(processed, f, indent=2, ensure_ascii=False)

    # Stats
    pc = Counter(b["sponsor_party"] for b in processed)
    print(f"  D={pc.get('D',0)} R={pc.get('R',0)} I={pc.get('I',0)}")
    sc = Counter(b["sponsor_state"] for b in processed if b["sponsor_state"])
    print(f"  States: {len(sc)}")

    # ─── Step 4: TF-IDF + LDA ───
    print(f"\n=== NLP Classification ===")
    texts_map = {}
    for fname in os.listdir(TEXT_DIR):
        if not fname.endswith(".txt"): continue
        bid = fname.replace(".txt", "")
        bid = re.sub(r"^HR_", "H.R. ", bid)
        bid = re.sub(r"^S_", "S. ", bid)
        with open(os.path.join(TEXT_DIR, fname), encoding="utf-8") as f:
            texts_map[bid] = f.read()

    bill_ids = [b["bill_id"] for b in processed if b["bill_id"] in texts_map]
    texts = [texts_map[bid] for bid in bill_ids]
    print(f"  텍스트: {len(texts)}건")

    if len(texts) < 5:
        print("  텍스트 부족, NLP 스킵")
        return

    vectorizer = TfidfVectorizer(stop_words="english", max_features=10000, min_df=2, max_df=0.95, ngram_range=(1,2),
                                  token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z-]+\b")
    tfidf_matrix = vectorizer.fit_transform(texts)
    feat = {name: idx for idx, name in enumerate(vectorizer.get_feature_names_out())}

    tfidf_results = {}
    for i, bid in enumerate(bill_ids):
        row = tfidf_matrix[i].toarray().flatten()
        scores = {}
        for attr, kws in POLICY_ATTRIBUTES.items():
            score = 0.0
            for kw in kws:
                kw_l = kw.lower()
                if kw_l in feat:
                    score += row[feat[kw_l]] * (2.0 if " " in kw_l else 1.0)
                elif " " in kw_l:
                    for w in kw_l.split():
                        if w in feat: score += row[feat[w]] * 0.5
            scores[attr] = score
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        tfidf_results[bid] = {"primary": ranked[0][0], "top3": [r[0] for r in ranked[:3] if r[1]>0]}

    with open(os.path.join(DATA_DIR, "us119_tfidf.json"), "w", encoding="utf-8") as f:
        json.dump(tfidf_results, f, indent=2, ensure_ascii=False)

    tf_primary = Counter(r["primary"] for r in tfidf_results.values())
    print("  TF-IDF Primary:")
    for a, c in tf_primary.most_common(): print(f"    {a}: {c}")

    # ─── Step 5: GPT ───
    print(f"\n=== GPT Validation ===")
    gpt_file = os.path.join(DATA_DIR, "us119_gpt.json")
    if os.path.exists(gpt_file):
        with open(gpt_file, encoding="utf-8") as f:
            gpt_results = json.load(f)
    else:
        gpt_results = {}

    attr_list = "\n".join(f"- {a}" for a in POLICY_ATTRIBUTES)
    for i, bid in enumerate(bill_ids, 1):
        if bid in gpt_results: continue
        title = next((b["title"] for b in processed if b["bill_id"] == bid), "")
        text = texts_map.get(bid, "")[:8000]
        print(f"  [{i}/{len(bill_ids)}] {bid}...", end=" ", flush=True)
        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": f"Classify this AI bill into top 3 policy attributes.\nAttributes:\n{attr_list}\nJSON: {{\"primary\": \"...\", \"secondary\": \"...\", \"tertiary\": \"...\"}}"},
                    {"role": "user", "content": f"Title: {title}\n\nText:\n{text}"},
                ],
                temperature=0, response_format={"type": "json_object"},
            )
            gpt_results[bid] = json.loads(resp.choices[0].message.content)
            print(gpt_results[bid].get("primary", "?")[:25])
        except Exception as e:
            print(f"ERR: {e}")
        time.sleep(0.3)

    with open(gpt_file, "w", encoding="utf-8") as f:
        json.dump(gpt_results, f, indent=2, ensure_ascii=False)

    gpt_primary = Counter(r.get("primary", "") for r in gpt_results.values())
    print("\n  GPT Primary:")
    for a, c in gpt_primary.most_common(): print(f"    {a}: {c}")

    print(f"\n{'='*60}")
    print("119th Congress 완료")
    print(f"  총 법안: {len(processed)}건")
    print(f"  D={pc.get('D',0)} R={pc.get('R',0)}")


if __name__ == "__main__":
    main()
