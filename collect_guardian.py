"""Guardian Content API로 AI 관련 기사 수집.

키워드 검색 기반, NYT와 동일 조건:
- 검색어: "artificial intelligence", "AI", "A.I."
- 기간: 2016.03 ~ 2026.04
- 정책 관련 섹션 필터

분류는 별도 스크립트 classify.py 사용:
    python classify.py guardian

Usage:
    python collect_guardian.py
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
OUTPUT_RAW = os.path.join(DATA_DIR, "guardian_articles_raw.json")

GUARDIAN_API_KEY = os.environ.get("GUARDIAN_API_KEY", "test")  # "test" = free tier

SEARCH_URL = "https://content.guardianapis.com/search"

QUERIES = [
    '"artificial intelligence"',
    '"A.I."',
    'AI',
]

# 2016.03 ~ 2026.04
PERIODS = [
    ("2016-03-01", "2016-12-31"),
    ("2017-01-01", "2017-12-31"),
    ("2018-01-01", "2018-12-31"),
    ("2019-01-01", "2019-12-31"),
    ("2020-01-01", "2020-12-31"),
    ("2021-01-01", "2021-12-31"),
    ("2022-01-01", "2022-12-31"),
    ("2023-01-01", "2023-12-31"),
    ("2024-01-01", "2024-12-31"),
    ("2025-01-01", "2025-12-31"),
    ("2026-01-01", "2026-04-08"),
]

# 정책 관련 섹션 (NYT desk 필터와 동등)
POLICY_SECTIONS = {
    "technology", "business", "politics", "world",
    "us-news", "uk-news", "australia-news",
    "science", "law", "global-development",
    "environment", "society", "commentisfree",
    "education", "media",
}


def fetch_slot(query: str, from_date: str, to_date: str) -> list[dict]:
    """하나의 검색어+기간 조합에서 전체 수집."""
    articles = []
    page = 1

    while True:
        params = {
            "q": query,
            "from-date": from_date,
            "to-date": to_date,
            "page-size": 200,
            "page": page,
            "show-fields": "headline,trailText,wordcount",
            "order-by": "newest",
            "api-key": GUARDIAN_API_KEY,
        }

        try:
            resp = requests.get(SEARCH_URL, params=params, timeout=30)

            if resp.status_code == 429:
                print("    rate limited, waiting 10s...", flush=True)
                time.sleep(10)
                continue

            resp.raise_for_status()
            data = resp.json().get("response", {})
            results = data.get("results", [])

            if not results:
                break

            for r in results:
                fields = r.get("fields", {})
                articles.append({
                    "id": r.get("id", ""),
                    "title": r.get("webTitle", ""),
                    "trail_text": fields.get("trailText", ""),
                    "url": r.get("webUrl", ""),
                    "section": r.get("sectionId", ""),
                    "pub_date": r.get("webPublicationDate", ""),
                    "word_count": fields.get("wordcount", ""),
                    "query": query,
                })

            total_pages = data.get("pages", 1)
            if page >= total_pages:
                break

            page += 1
            time.sleep(0.2)  # rate limit: 12 req/sec

        except requests.exceptions.HTTPError as e:
            print(f"    HTTP error page {page}: {e}", flush=True)
            break
        except Exception as e:
            print(f"    error page {page}: {e}", flush=True)
            time.sleep(1)
            continue

    return articles


def collect_articles() -> list[dict]:
    """전체 수집: 3 queries × 11 periods."""
    if os.path.exists(OUTPUT_RAW):
        with open(OUTPUT_RAW, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"  cached: {len(existing)} articles", flush=True)
        return existing

    print("=== Guardian Content API ===", flush=True)
    all_articles = []
    seen_urls = set()
    slot_num = 0
    total_slots = len(QUERIES) * len(PERIODS)

    for query in QUERIES:
        for from_date, to_date in PERIODS:
            slot_num += 1
            print(f"\n  [{slot_num}/{total_slots}] q={query} | {from_date}~{to_date}", flush=True)

            articles = fetch_slot(query, from_date, to_date)

            # Section 필터 + dedup
            new = 0
            for a in articles:
                if a["section"] in POLICY_SECTIONS and a["url"] not in seen_urls:
                    seen_urls.add(a["url"])
                    all_articles.append(a)
                    new += 1

            print(f"    fetched={len(articles)}, kept={new}, total={len(all_articles)}", flush=True)

            # 중간 저장
            with open(OUTPUT_RAW, "w", encoding="utf-8") as f:
                json.dump(all_articles, f, ensure_ascii=False)

    print(f"\n  Total: {len(all_articles)} unique articles", flush=True)
    with open(OUTPUT_RAW, "w", encoding="utf-8") as f:
        json.dump(all_articles, f, indent=2, ensure_ascii=False)

    return all_articles


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    articles = collect_articles()
    print(f"\nCollected {len(articles)} articles. Run 'python classify.py guardian' to classify.", flush=True)


if __name__ == "__main__":
    main()
