"""Step 7: GPT-4o-mini로 법안 분류 검증.

논문 p.94: "We validated the proposed labels using GPT-4o's API
using the same bill text processing techniques."

각 법안의 텍스트를 GPT에게 10개 Policy Attribute 중 Top 3로 분류시킴.

Usage:
    python replicate_carvao/06_gpt_validate.py
"""
import json
import os
import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TEXT_DIR = os.path.join(DATA_DIR, "bills_text")
PROCESSED_FILE = os.path.join(DATA_DIR, "bills_processed.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "gpt_classification.json")

client = OpenAI()

POLICY_ATTRIBUTES = [
    "Market efficiency and power concentration (antitrust)",
    "Safety",
    "Responsible and ethical AI",
    "National security",
    "Industrial policy",
    "Public interest",
    "Labor",
    "Copyright",
    "International collaboration",
    "Elections",
]

SYSTEM_PROMPT = """You are an expert policy analyst specializing in U.S. AI legislation.

You will be given the text of a bill introduced in the 118th U.S. Congress related to artificial intelligence.

Classify the bill into the following 10 policy attributes. Assign exactly 3 attributes ranked by relevance (primary, secondary, tertiary).

Policy Attributes:
1. Market efficiency and power concentration (antitrust) — market competition, monopoly, Big Tech regulation, platform dominance
2. Safety — AI system safety, risk management, testing, evaluation, red-teaming, incidents
3. Responsible and ethical AI — accountability, transparency, bias, fairness, governance, oversight, regulation frameworks, standards, impact assessments
4. National security — defense, military, intelligence, cybersecurity, adversarial threats, China competition
5. Industrial policy — AI industry development, R&D investment, semiconductors, infrastructure, workforce development, STEM, NSF/NIST
6. Public interest — consumer protection, healthcare, education, children, environment, energy, public services
7. Labor — workers, employment, automation, job displacement, retraining, wages, workplace
8. Copyright — intellectual property, creative works, training data, generative content, fair use, licensing
9. International collaboration — multilateral cooperation, treaties, global standards, OECD, G7, EU alignment
10. Elections — electoral integrity, deepfakes in elections, political ads, voter manipulation, campaign AI use

Respond ONLY with valid JSON in this exact format:
{"primary": "attribute name", "secondary": "attribute name", "tertiary": "attribute name"}"""


def classify_bill(title: str, text: str) -> dict | None:
    """GPT-4o-mini로 법안 분류."""
    # 텍스트 길이 제한 (토큰 절약)
    truncated = text[:8000] if len(text) > 8000 else text

    user_msg = f"Bill Title: {title}\n\nBill Text:\n{truncated}"

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            result = json.loads(content)

            # Validate
            for key in ["primary", "secondary", "tertiary"]:
                if key not in result:
                    return None

            return result

        except Exception as e:
            print(f"    Error: {e}")
            time.sleep(2)

    return None


def load_bill_texts() -> dict[str, str]:
    """법안 전문 텍스트 로드."""
    import re
    texts = {}
    for fname in os.listdir(TEXT_DIR):
        if not fname.endswith(".txt"):
            continue
        bill_id = fname.replace(".txt", "")
        bill_id = re.sub(r"^HR_", "H.R. ", bill_id)
        bill_id = re.sub(r"^S_", "S. ", bill_id)
        bill_id = re.sub(r"^HJRes_", "H.J.Res. ", bill_id)

        with open(os.path.join(TEXT_DIR, fname), encoding="utf-8") as f:
            texts[bill_id] = f.read()
    return texts


def main():
    with open(PROCESSED_FILE, encoding="utf-8") as f:
        processed = json.load(f)

    all_texts = load_bill_texts()
    print(f"법안: {len(processed)}건, 텍스트: {len(all_texts)}건")

    # Load existing progress
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            results = json.load(f)
        print(f"기존 분류: {len(results)}건, 이어서 진행")
    else:
        results = {}

    classified = 0
    for i, bill in enumerate(processed, 1):
        bill_id = bill["bill_id"]
        title = bill["title"]

        if bill_id in results:
            continue

        if bill_id not in all_texts:
            print(f"[{i}/{len(processed)}] {bill_id}: no text, skip")
            continue

        print(f"[{i}/{len(processed)}] {bill_id}...", end=" ", flush=True)

        result = classify_bill(title, all_texts[bill_id])
        if result:
            results[bill_id] = result
            classified += 1
            print(f"{result['primary'][:30]}")
        else:
            print("FAIL")

        # Save every 20
        if classified % 20 == 0 and classified > 0:
            with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print(f"  (saved {len(results)}건)")

        time.sleep(0.3)

    # Final save
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n분류 완료: {len(results)}건")
    print(f"저장: {OUTPUT_FILE}")

    # ─── 결과 통계 ───
    from collections import Counter

    print(f"\n{'='*60}")
    print("[GPT] Primary Policy Attribute Distribution:")
    primary_counts = Counter(r["primary"] for r in results.values())
    for attr, cnt in primary_counts.most_common():
        paper_val = ""
        if "Responsible" in attr: paper_val = " (논문 최종: 71)"
        elif "National security" in attr: paper_val = " (논문 최종: 24)"
        elif "Safety" in attr: paper_val = " (논문 최종: 14)"
        elif "Public interest" in attr: paper_val = " (논문 최종: 13)"
        print(f"  {attr}: {cnt}{paper_val}")

    print(f"\n[GPT] Top 3 Policy Attribute Distribution:")
    top3_counts = Counter()
    for r in results.values():
        top3_counts[r["primary"]] += 1
        top3_counts[r["secondary"]] += 1
        top3_counts[r["tertiary"]] += 1
    for attr, cnt in top3_counts.most_common():
        paper_val = ""
        if "Responsible" in attr: paper_val = " (논문 최종: 141)"
        elif "National security" in attr: paper_val = " (논문 최종: 106)"
        elif "Safety" in attr: paper_val = " (논문 최종: 66)"
        elif "Public interest" in attr: paper_val = " (논문 최종: 53)"
        print(f"  {attr}: {cnt}{paper_val}")

    # Compare with TF-IDF
    tfidf_file = os.path.join(DATA_DIR, "tfidf_classification.json")
    if os.path.exists(tfidf_file):
        with open(tfidf_file, encoding="utf-8") as f:
            tfidf = json.load(f)

        agree = 0
        disagree = 0
        for bill_id in results:
            if bill_id in tfidf:
                if results[bill_id]["primary"] == tfidf[bill_id]["primary"]:
                    agree += 1
                else:
                    disagree += 1

        total = agree + disagree
        print(f"\n[TF-IDF vs GPT 일치도]")
        print(f"  일치: {agree}/{total} ({agree/total*100:.1f}%)")
        print(f"  불일치: {disagree}/{total} ({disagree/total*100:.1f}%)")


if __name__ == "__main__":
    main()
