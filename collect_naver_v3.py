"""Naver News 재수집 v3: sedaily 제거 + heraldcorp 추가 + yna 영문판 제외."""
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_RAW = os.path.join(DATA_DIR, "naver_articles_v3_raw.json")
OUTPUT = os.path.join(DATA_DIR, "naver_articles_v3.json")

NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"

PUBLISHERS = [
    # 일간지
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
    # 경제지 (sedaily.com → heraldcorp.com)
    "mk.co.kr", "hankyung.com", "heraldcorp.com", "edaily.co.kr",
    # IT 전문
    "zdnet.co.kr", "etnews.com", "bloter.net", "ddaily.co.kr",
    # 통신사
    "yna.co.kr", "news1.kr", "newsis.com",
]
KEYWORDS = ["인공지능", "AI"]
QUERIES = [f"{pub} {kw}" for pub in PUBLISHERS for kw in KEYWORDS]

TODAY = datetime.now(timezone(timedelta(hours=9)))
CUTOFF = TODAY - timedelta(days=730)


def fetch_query(query: str) -> list[dict]:
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
    return url.split("//")[1].split("/")[0].removeprefix("www.")


def matches_publisher(url: str, pub: str) -> bool:
    d = domain(url)
    # 연합뉴스는 영문판 제외
    if pub == "yna.co.kr":
        return d == "yna.co.kr" or d.startswith("m.yna.co.kr")
    # 헤럴드경제는 biz.heraldcorp.com, biz.heraldcorp.com 등 서브도메인 허용
    if pub == "heraldcorp.com":
        return "heraldcorp.com" in d
    return pub in d


def parse_date(s: str):
    try:
        return datetime.strptime(s, '%a, %d %b %Y %H:%M:%S %z')
    except Exception:
        return None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    print(f"=== Naver Collection v3 ===", flush=True)
    print(f"  Publishers: {len(PUBLISHERS)}, Keywords: {len(KEYWORDS)}, Queries: {len(QUERIES)}", flush=True)
    print(f"  Cutoff: {CUTOFF.date()}", flush=True)

    if os.path.exists(OUTPUT_RAW):
        with open(OUTPUT_RAW, encoding="utf-8") as f:
            raw = json.load(f)
        print(f"  cached raw: {len(raw)}건", flush=True)
    else:
        raw = []
        for i, q in enumerate(QUERIES, 1):
            pub = q.split()[0]
            print(f"  [{i}/{len(QUERIES)}] {q}", end=" ", flush=True)
            items = fetch_query(q)
            kept = [it for it in items if matches_publisher(it.get("originallink") or it.get("link", ""), pub)]
            raw.extend(kept)
            print(f"→ fetched={len(items)}, pub_match={len(kept)}", flush=True)

        with open(OUTPUT_RAW, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        print(f"\n  raw 저장: {len(raw)}건", flush=True)

    # 2년 필터
    filtered = [a for a in raw if (d := parse_date(a.get("pub_date", ""))) and d >= CUTOFF]
    print(f"  2년 필터: {len(raw)} → {len(filtered)}", flush=True)

    # URL dedup
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

    # 분석
    print(f"\n  === 최종 {len(final)}건 ===", flush=True)
    dates = sorted(parse_date(a["pub_date"]) for a in final if parse_date(a["pub_date"]))
    if dates:
        print(f"  기간: {dates[0].date()} ~ {dates[-1].date()} ({(dates[-1]-dates[0]).days}일)", flush=True)

    pub_dist = Counter()
    for a in final:
        url = a.get("originallink") or a.get("link", "")
        d = domain(url)
        for p in PUBLISHERS:
            if matches_publisher(url, p):
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
