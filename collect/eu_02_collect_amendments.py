"""EU AI Act 수정안 808건 수집 및 Policy Attribute 분류.

EP Committee Report A9-0188/2023에서 수정안 파싱 후 GPT 분류.

Usage:
    python replicate_carvao/eu_02_collect_amendments.py
"""
import json
import os
import re
import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: F401
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv

import config

load_dotenv()

HTML_CACHE = os.path.join(config.BILLS_EU_DIR, "eu_amendments_raw.html")
OUTPUT_AMENDMENTS = os.path.join(config.BILLS_EU_DIR, "eu_amendments.json")
OUTPUT_CLASSIFIED = os.path.join(config.BILLS_EU_DIR, "eu_amendments_classified.json")

client = OpenAI()

REPORT_URL = "https://www.europarl.europa.eu/doceo/document/A-9-2023-0188_EN.html"


def download_html() -> str:
    """EP 위원회 보고서 HTML 다운로드."""
    if os.path.exists(HTML_CACHE):
        print(f"  캐시 사용: {HTML_CACHE}")
        with open(HTML_CACHE, encoding="utf-8") as f:
            return f.read()

    print("  EP에서 다운로드 중...")
    resp = requests.get(REPORT_URL, headers={
        "User-Agent": "Mozilla/5.0 (research project)",
    }, timeout=60)
    resp.raise_for_status()
    html = resp.text

    with open(HTML_CACHE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  저장: {len(html):,} bytes")
    return html


def parse_amendments(html: str) -> list[dict]:
    """HTML에서 수정안 파싱."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text()

    # "Amendment N" 패턴으로 분할
    # 각 수정안 블록: Amendment N ~ 다음 Amendment N-1
    amendment_pattern = re.compile(r'\bAmendment\s+(\d+)\b')
    positions = [(m.start(), int(m.group(1))) for m in amendment_pattern.finditer(text)]

    # 각 번호의 첫 번째 occurrence만 유지 (본문 참조 ��외)
    seen_nums = set()
    filtered = []
    for pos, num in positions:
        if num not in seen_nums:
            seen_nums.add(num)
            filtered.append((pos, num))
    filtered.sort(key=lambda x: x[0])  # 위치순 정렬

    print(f"  수정안: {len(filtered)}건 (1~{max(n for _, n in filtered)})")

    amendments = []
    for i, (pos, num) in enumerate(filtered):
        end = filtered[i + 1][0] if i + 1 < len(filtered) else len(text)
        chunk = text[pos:end].strip()

        # 대상 조항 파싱
        target = "unknown"
        # "Article N" 또는 "Recital N" 또는 "Annex" 패턴
        target_m = re.search(
            r'(?:Article|Recital|Annex|Citation|Title|Chapter)\s*[\d\w]*(?:\s*[–-]\s*paragraph\s*\d+)?',
            chunk[:500]
        )
        if target_m:
            target = target_m.group(0).strip()

        # 수정 내용 (Amendment 이후 텍스트)
        # "Text proposed by the Commission" 과 "Amendment" 사이
        content_lines = chunk.split('\n')
        # 첫 5줄은 헤더, 나머지가 내용
        content = '\n'.join(content_lines[1:]).strip()

        amendments.append({
            "amendment_num": num,
            "target": target,
            "text": content[:3000],
        })

    return amendments


SYSTEM_PROMPT = """You are an expert AI policy analyst.
Classify this EU AI Act amendment into the top 3 policy attributes below.

Policy Attributes:
1. Market efficiency and power concentration (antitrust)
2. Safety
3. Responsible and ethical AI
4. National security
5. Industrial policy
6. Public interest
7. Labor
8. Copyright
9. International collaboration
10. Elections

If the amendment is purely procedural (definitions, timelines, etc.),
respond with "Procedural/Administrative" as primary.

Respond ONLY with JSON: {"primary": "...", "secondary": "...", "tertiary": "..."}"""


def classify_amendments(amendments: list[dict]) -> list[dict]:
    """GPT로 수정안 분류."""
    print(f"\n=== GPT Classification ({len(amendments)} amendments) ===")

    if os.path.exists(OUTPUT_CLASSIFIED):
        with open(OUTPUT_CLASSIFIED, encoding="utf-8") as f:
            existing = json.load(f)
        done_nums = {a["amendment_num"] for a in existing}
        print(f"  existing: {len(existing)}")
    else:
        existing = []
        done_nums = set()

    new_count = 0
    for i, amd in enumerate(amendments, 1):
        if amd["amendment_num"] in done_nums:
            continue

        user_msg = f"Amendment {amd['amendment_num']}\nTarget: {amd['target']}\n\n{amd['text'][:2500]}"

        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)
            result["amendment_num"] = amd["amendment_num"]
            result["target"] = amd["target"]
            existing.append(result)
            new_count += 1

            if new_count % 50 == 0:
                print(f"  [{new_count}] Amd.{amd['amendment_num']} ({amd['target'][:30]}) -> {result.get('primary', '?')[:25]}")
                with open(OUTPUT_CLASSIFIED, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False)

        except Exception as e:
            print(f"  Amd.{amd['amendment_num']}: ERROR {e}")
            existing.append({
                "amendment_num": amd["amendment_num"],
                "target": amd["target"],
                "primary": "error",
                "error": str(e),
            })

        time.sleep(0.05)

    with open(OUTPUT_CLASSIFIED, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    print(f"  new: {new_count}, total: {len(existing)}")
    return existing


def print_summary(classified: list[dict]):
    from collections import Counter
    print(f"\n{'='*60}")
    print(f"EU AI Act Amendments: {len(classified)} classified")

    primary = Counter(a.get("primary", "?") for a in classified)
    for attr, cnt in primary.most_common():
        print(f"  {attr}: {cnt} ({cnt/len(classified)*100:.1f}%)")

    # Target article distribution
    print(f"\nMost amended targets:")
    targets = Counter(a.get("target", "?") for a in classified)
    for t, cnt in targets.most_common(10):
        print(f"  {t}: {cnt}")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    print("=== EU AI Act Amendments ===")
    html = download_html()

    print("\n=== Parsing Amendments ===")
    amendments = parse_amendments(html)
    print(f"  parsed: {len(amendments)}")

    if not amendments:
        print("ERROR: no amendments found")
        return

    with open(OUTPUT_AMENDMENTS, "w", encoding="utf-8") as f:
        json.dump(amendments, f, indent=2, ensure_ascii=False)

    classified = classify_amendments(amendments)
    print_summary(classified)


if __name__ == "__main__":
    main()
