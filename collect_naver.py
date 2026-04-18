"""Naver 검색 API로 AI 관련 뉴스 기사 수집.

Naver API 한계: 최근 결과만 반환 (약 6주).
키워드별 최대 1,000건 (start 최대 1000).

Usage:
    python collect_naver.py
"""
import json
import os
import re
import sys
import time
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_RAW = os.path.join(DATA_DIR, "naver_articles_raw.json")

NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]

SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"

# AI 관련 검색어 — Guardian/NYT와 동일 원칙 (broad 키워드만, 정책 속성별 쿼리 제외)
# Naver 형태소 분석으로 "인공 지능" 띄어쓰기도 자동 포함
QUERIES = [
    "인공지능",   # artificial intelligence
    "AI",         # AI
]


def fetch_query(query: str) -> list[dict]:
    """하나의 검색어로 최대 1,000건 수집."""
    articles = []
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }

    for start in range(1, 1001, 100):
        params = {
            "query": query,
            "display": 100,
            "start": start,
            "sort": "date",
        }

        try:
            resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])

            if not items:
                break

            for item in items:
                # HTML 태그 제거
                title = re.sub(r'<[^>]+>', '', item.get("title", ""))
                desc = re.sub(r'<[^>]+>', '', item.get("description", ""))
                articles.append({
                    "title": title,
                    "description": desc,
                    "link": item.get("link", ""),
                    "originallink": item.get("originallink", ""),
                    "pub_date": item.get("pubDate", ""),
                    "query": query,
                })

            time.sleep(0.1)

        except Exception as e:
            print(f"    error at start={start}: {e}", flush=True)
            break

    return articles


def collect_articles() -> list[dict]:
    """전체 수집: 20개 검색어."""
    if os.path.exists(OUTPUT_RAW):
        with open(OUTPUT_RAW, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"  cached: {len(existing)} articles", flush=True)
        return existing

    print("=== Naver News Search ===", flush=True)
    all_articles = []
    seen_links = set()

    for i, query in enumerate(QUERIES, 1):
        print(f"  [{i}/{len(QUERIES)}] {query}...", end=" ", flush=True)

        articles = fetch_query(query)

        new = 0
        for a in articles:
            if a["link"] not in seen_links:
                seen_links.add(a["link"])
                all_articles.append(a)
                new += 1

        print(f"fetched={len(articles)}, new={new}, total={len(all_articles)}", flush=True)

    # 중복 제거 (제목 기준)
    seen_titles = set()
    deduped = []
    for a in all_articles:
        t = a["title"].strip()
        if t and t not in seen_titles:
            seen_titles.add(t)
            deduped.append(a)

    print(f"\n  URL dedup: {len(all_articles)} -> title dedup: {len(deduped)}", flush=True)

    with open(OUTPUT_RAW, "w", encoding="utf-8") as f:
        json.dump(deduped, f, indent=2, ensure_ascii=False)

    return deduped


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    articles = collect_articles()
    print(f"\nTotal: {len(articles)} articles saved to {OUTPUT_RAW}", flush=True)


if __name__ == "__main__":
    main()
