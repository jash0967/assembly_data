"""Naver News 재수집 v2: 언론사 도메인 필터 방식.

설계:
- 15개 주요 언론사 × 2개 broad 키워드(AI, 인공지능) = 30 쿼리
- 쿼리당 최대 1,000건 (네이버 API 한계)
- 최근 2년 필터 (2024-04-17 ~ 2026-04-17)
- URL 기준 전역 dedup

출력:
- data/naver_articles_v2_raw.json    — dedup 전 원본
- data/naver_articles_v2.json        — 최종 (2년 필터 + dedup)
"""
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_RAW = os.path.join(DATA_DIR, "naver_articles_v2_raw.json")
OUTPUT = os.path.join(DATA_DIR, "naver_articles_v2.json")

NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"

# 타깃 언론사 15개
PUBLISHERS = [
    # 일간지
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
    # 경제지
    "mk.co.kr", "hankyung.com", "sedaily.com", "edaily.co.kr",
    # IT 전문
    "zdnet.co.kr", "etnews.com", "bloter.net", "ddaily.co.kr",
    # 통신사
    "yna.co.kr", "news1.kr", "newsis.com",
]
KEYWORDS = ["인공지능", "AI"]

# 쿼리 생성
QUERIES = [f"{pub} {kw}" for pub in PUBLISHERS for kw in KEYWORDS]

# 2년 cutoff
TODAY = datetime.now(timezone(timedelta(hours=9)))
CUTOFF = TODAY - timedelta(days=730)


def fetch_query(query: str) -> list[dict]:
    """쿼리 하나로 최대 1,000건 수집 (10 page × 100)."""
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    out = []
    for start in range(1, 1001, 100):
        params = {"query": query, "display": 100, "start": start, "sort": "date"}
        try:
            resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                print(f"    HTTP {resp.status_code} at start={start}", flush=True)
                break
            items = resp.json().get("items", [])
            if not items:
                break
            for item in items:
                title = re.sub(r'<[^>]+>', '', item.get("title", ""))
                desc = re.sub(r'<[^>]+>', '', item.get("description", ""))
                out.append({
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
    return out


def domain(url: str) -> str:
    if "//" not in url:
        return ""
    d = url.split("//")[1].split("/")[0]
    return d.removeprefix("www.")


def matches_publisher(url: str, pub: str) -> bool:
    """URL이 해당 언론사 도메인인지 확인."""
    d = domain(url)
    return pub in d


def parse_date(s: str):
    try:
        return datetime.strptime(s, '%a, %d %b %Y %H:%M:%S %z')
    except Exception:
        return None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"=== Naver Collection v2 ===", flush=True)
    print(f"  Publishers: {len(PUBLISHERS)}개, Keywords: {len(KEYWORDS)}개, 총 {len(QUERIES)} 쿼리", flush=True)
    print(f"  Cutoff (2년): {CUTOFF.date()}", flush=True)

    # raw 캐시 확인
    if os.path.exists(OUTPUT_RAW):
        with open(OUTPUT_RAW, encoding="utf-8") as f:
            raw = json.load(f)
        print(f"  cached raw: {len(raw)}건", flush=True)
    else:
        raw = []
        seen_query_keys = set()
        for i, q in enumerate(QUERIES, 1):
            pub = q.split()[0]
            print(f"\n  [{i}/{len(QUERIES)}] {q}", end=" ", flush=True)
            items = fetch_query(q)
            # pub 필터 (sportschosun 등 자회사 제외하고 도메인 일치만)
            kept = [it for it in items if matches_publisher(it.get("originallink") or it.get("link", ""), pub)]
            raw.extend(kept)
            print(f"→ fetched={len(items)}, pub_match={len(kept)}", flush=True)

        with open(OUTPUT_RAW, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        print(f"\n  raw 저장: {len(raw)}건", flush=True)

    # 2년 필터
    filtered = []
    for a in raw:
        d = parse_date(a.get("pub_date", ""))
        if d is None:
            continue
        if d >= CUTOFF:
            filtered.append(a)

    print(f"\n  2년 필터: {len(raw)} → {len(filtered)}", flush=True)

    # URL dedup (originallink 우선)
    seen = set()
    deduped = []
    for a in filtered:
        key = a.get("originallink") or a.get("link", "")
        if key and key not in seen:
            seen.add(key)
            deduped.append(a)
    print(f"  URL dedup: {len(filtered)} → {len(deduped)}", flush=True)

    # 제목 dedup
    seen_t = set()
    final = []
    for a in deduped:
        t = a["title"].strip()
        if t and t not in seen_t:
            seen_t.add(t)
            final.append(a)
    print(f"  제목 dedup: {len(deduped)} → {len(final)}", flush=True)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)

    # 분포
    print(f"\n  === 최종 {len(final)}건 분석 ===", flush=True)
    dates = [parse_date(a["pub_date"]) for a in final]
    dates = [d for d in dates if d]
    dates.sort()
    if dates:
        print(f"  기간: {dates[0].date()} ~ {dates[-1].date()} ({(dates[-1]-dates[0]).days}일)", flush=True)

    pub_dist = Counter()
    for a in final:
        url = a.get("originallink") or a.get("link", "")
        d = domain(url)
        for p in PUBLISHERS:
            if p in d:
                pub_dist[p] += 1
                break
        else:
            pub_dist["other"] += 1
    print(f"\n  언론사별 분포:", flush=True)
    for p, c in pub_dist.most_common():
        print(f"    {p:<20} {c:>5}건 ({c/len(final)*100:.1f}%)", flush=True)

    year_dist = Counter(d.year for d in dates)
    print(f"\n  연도별 분포:", flush=True)
    for y, c in sorted(year_dist.items()):
        print(f"    {y}: {c}", flush=True)


if __name__ == "__main__":
    main()
