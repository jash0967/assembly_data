"""NYT Archive API로 AI 관련 기사 수집.

Archive API: 월별 전체 기사를 한 번에 반환 (~20MB/월).
로컬에서 키워드 + desk 필터링.

분류는 별도 스크립트 classify.py 사용:
    python classify.py nyt

Usage:
    python collect_nyt.py
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

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ARCHIVE_DIR = os.path.join(DATA_DIR, "nyt_archive")
OUTPUT_RAW = os.path.join(DATA_DIR, "nyt_articles_raw.json")

NYT_API_KEY = os.environ["NYT_API_KEY"]

ARCHIVE_URL = "https://api.nytimes.com/svc/archive/v1/{year}/{month}.json"

# 2023.01 ~ 2025.04
MONTHS = []
for y in range(2016, 2027):
    for m in range(1, 13):
        if y == 2016 and m < 3:
            continue
        if y == 2026 and m > 4:
            break
        MONTHS.append((y, m))

# 키워드 필터 (대소문자 무시, headline/abstract/snippet/keywords에서)
AI_PATTERNS = [
    re.compile(r'\bartificial intelligence\b', re.IGNORECASE),
    re.compile(r'\bA\.I\.\b'),
    re.compile(r'\bAI\b'),  # 대문자 AI만 (said, fair 등 제외)
]

# 정책 관련 desk만
POLICY_DESKS = {
    "Business", "Washington", "Foreign", "Science",
    "Politics", "National", "SundayBusiness", "OpEd",
    "Climate", "Investigative", "Express", "NYTNow",
}


def download_month(year: int, month: int) -> list[dict]:
    """한 달치 아카이브 다운로드 (캐시)."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    cache = os.path.join(ARCHIVE_DIR, f"{year}_{month:02d}.json")

    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    url = ARCHIVE_URL.format(year=year, month=month)
    resp = requests.get(url, params={"api-key": NYT_API_KEY}, timeout=120)
    resp.raise_for_status()
    docs = resp.json().get("response", {}).get("docs", [])

    with open(cache, "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False)

    return docs


def is_ai_article(doc: dict) -> bool:
    """키워드 매칭으로 AI 관련 기사 판별."""
    text_fields = [
        doc.get("headline", {}).get("main", ""),
        doc.get("abstract", "") or "",
        doc.get("snippet", "") or "",
        doc.get("lead_paragraph", "") or "",
    ]
    # keywords 필드도
    for kw in (doc.get("keywords") or []):
        text_fields.append(kw.get("value", ""))

    combined = " ".join(text_fields)
    return any(p.search(combined) for p in AI_PATTERNS)


def extract_article(doc: dict) -> dict:
    """필요한 필드만 추출."""
    return {
        "title": doc.get("headline", {}).get("main", ""),
        "abstract": doc.get("abstract", "") or doc.get("snippet", "") or "",
        "pub_date": doc.get("pub_date", ""),
        "url": doc.get("web_url", ""),
        "section": doc.get("section_name", ""),
        "desk": doc.get("news_desk", "") or doc.get("desk", "") or "",
        "keywords": [k.get("value", "") for k in (doc.get("keywords") or [])],
        "type_of_material": doc.get("type_of_material", ""),
    }


def collect_articles() -> list[dict]:
    """전체 수집: 28개월 아카이브."""
    if os.path.exists(OUTPUT_RAW):
        with open(OUTPUT_RAW, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"  cached: {len(existing)} articles", flush=True)
        return existing

    print("=== NYT Archive API ===", flush=True)
    all_articles = []
    seen_urls = set()
    total_docs = 0
    total_ai = 0
    total_filtered = 0

    for i, (year, month) in enumerate(MONTHS, 1):
        print(f"  [{i}/{len(MONTHS)}] {year}-{month:02d}...", end=" ", flush=True)

        try:
            docs = download_month(year, month)
        except Exception as e:
            print(f"ERROR: {e}", flush=True)
            time.sleep(12)
            continue

        total_docs += len(docs)

        # AI 키워드 필터
        ai_docs = [d for d in docs if is_ai_article(d)]
        total_ai += len(ai_docs)

        # Desk 필터
        filtered = []
        for d in ai_docs:
            desk = d.get("news_desk", "") or d.get("desk", "") or ""
            if desk in POLICY_DESKS or not desk:  # desk 없는 것도 포함
                art = extract_article(d)
                if art["url"] not in seen_urls:
                    seen_urls.add(art["url"])
                    filtered.append(art)

        all_articles.extend(filtered)
        total_filtered += len(filtered)

        print(f"total={len(docs):,}, ai={len(ai_docs)}, kept={len(filtered)}, cumul={len(all_articles)}", flush=True)

        time.sleep(12)  # rate limit

    print(f"\n  Summary:", flush=True)
    print(f"    Total NYT articles scanned: {total_docs:,}", flush=True)
    print(f"    AI keyword match: {total_ai:,}", flush=True)
    print(f"    After desk filter + dedup: {len(all_articles):,}", flush=True)

    with open(OUTPUT_RAW, "w", encoding="utf-8") as f:
        json.dump(all_articles, f, indent=2, ensure_ascii=False)

    return all_articles


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    articles = collect_articles()
    print(f"\nCollected {len(articles)} articles. Run 'python classify.py nyt' to classify.", flush=True)


if __name__ == "__main__":
    main()
