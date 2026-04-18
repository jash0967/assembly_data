"""세부 주제 쿼리로 데이터 확장.

설계:
- 8개 매체 × 10개 세부 주제 키워드 = 80 쿼리 (기존 broad 쿼리는 별도)
- 기존 v3_clean 3,379건과 dedup
- 2년 필터 (2024-04-17 ~ 2026-04-17)
- 신규 URL만 본문 fetch → 3회 이상 AI 필터
- 기존 filtered 1,207건과 병합
"""
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SRC_V3 = os.path.join(DATA_DIR, "naver_articles_v3_clean.json")
SRC_FILTERED = os.path.join(DATA_DIR, "naver_articles_filtered.json")
CACHE_BODIES = os.path.join(DATA_DIR, "naver_bodies_cache.json")
OUT_RAW = os.path.join(DATA_DIR, "naver_articles_subtopic_raw.json")
OUT_FINAL = os.path.join(DATA_DIR, "naver_articles_final.json")

NAVER_CLIENT_ID = os.environ["NAVER_CLIENT_ID"]
NAVER_CLIENT_SECRET = os.environ["NAVER_CLIENT_SECRET"]
SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"
HEADERS_NAVER = {
    "X-Naver-Client-Id": NAVER_CLIENT_ID,
    "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
}
HEADERS_HTTP = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

PUBLISHERS = ["chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
              "yna.co.kr", "news1.kr", "newsis.com"]

# Carvão 10속성 커버하는 세부 주제 키워드
SUBTOPICS = [
    "AI 규제",        # 전반
    "AI 윤리",        # 책임/윤리
    "AI 안전",        # 안전
    "AI 저작권",      # 저작권
    "AI 국가안보",    # 국가안보
    "AI 노동",        # 노동
    "AI 선거",        # 선거
    "딥페이크",       # 윤리·프라이버시
    "챗GPT",          # 생성형 AI
    "생성형 AI",      # 산업
]

QUERIES = [f"{pub} {kw}" for pub in PUBLISHERS for kw in SUBTOPICS]

TODAY = datetime.now(timezone(timedelta(hours=9)))
CUTOFF = TODAY - timedelta(days=730)


def domain(url):
    return url.split("//")[1].split("/")[0].removeprefix("www.") if "//" in url else ""


def match_pub(url, pub):
    d = domain(url)
    if pub == "yna.co.kr":
        return d == "yna.co.kr" or d.startswith("m.yna.co.kr")
    return pub in d


def parse_date(s):
    try:
        return datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
    except Exception:
        return None


def fetch_naver(query: str) -> list[dict]:
    out = []
    for start in range(1, 1001, 100):
        params = {"query": query, "display": 100, "start": start, "sort": "date"}
        try:
            resp = requests.get(SEARCH_URL, headers=HEADERS_NAVER, params=params, timeout=15)
            if resp.status_code != 200:
                break
            items = resp.json().get("items", [])
            if not items:
                break
            for item in items:
                title = re.sub(r'<[^>]+>', '', item.get("title", ""))
                desc = re.sub(r'<[^>]+>', '', item.get("description", ""))
                out.append({
                    "title": title, "description": desc,
                    "link": item.get("link", ""),
                    "originallink": item.get("originallink", ""),
                    "pub_date": item.get("pubDate", ""),
                    "query": query,
                })
            time.sleep(0.1)
        except Exception as e:
            print(f"  error: {e}", flush=True)
            break
    return out


def extract_body(html):
    for pat in [
        r'<article[^>]*id="dic_area"[^>]*>(.*?)</article>',
        r'<div[^>]*id="newsct_article"[^>]*>(.*?)</div>',
    ]:
        m = re.search(pat, html, re.DOTALL)
        if m:
            body = re.sub(r'<[^>]+>', ' ', m.group(1))
            body = re.sub(r'\s+', ' ', body).strip()
            if len(body) > 100:
                return body
    return ""


def count_ai(text):
    return (len(re.findall(r'\bAI\b', text)) +
            len(re.findall(r'인공지능', text)) +
            len(re.findall(r'A\.I\.', text)))


def fetch_body_cached(item, cache):
    key = item.get("link", "")
    if key in cache:
        return cache[key]
    if "n.news.naver.com" not in key:
        return {"url": key, "ai_count": 0, "status": "no_naver", "body_len": 0}
    try:
        r = requests.get(key, headers=HEADERS_HTTP, timeout=15)
        if r.status_code != 200:
            return {"url": key, "ai_count": 0, "status": f"http_{r.status_code}", "body_len": 0}
        body = extract_body(r.text)
        if not body:
            return {"url": key, "ai_count": 0, "status": "no_body", "body_len": 0}
        return {"url": key, "ai_count": count_ai(body), "status": "ok", "body_len": len(body)}
    except Exception as e:
        return {"url": key, "ai_count": 0, "status": f"err:{str(e)[:30]}", "body_len": 0}


def main():
    print(f"=== 세부 주제 확장 수집 ===", flush=True)
    print(f"  매체: {len(PUBLISHERS)}, 주제: {len(SUBTOPICS)}, 총 {len(QUERIES)} 쿼리", flush=True)

    # 기존 데이터 로드
    with open(SRC_V3, encoding="utf-8") as f:
        existing_v3 = json.load(f)
    existing_urls = set()
    for a in existing_v3:
        k = a.get("originallink") or a.get("link", "")
        if k:
            existing_urls.add(k)
        l = a.get("link", "")
        if l:
            existing_urls.add(l)
    print(f"  기존 v3_clean: {len(existing_v3)}건 (URL {len(existing_urls)})", flush=True)

    # raw 캐시
    if os.path.exists(OUT_RAW):
        with open(OUT_RAW, encoding="utf-8") as f:
            subtopic_raw = json.load(f)
        print(f"  subtopic raw 캐시: {len(subtopic_raw)}건", flush=True)
    else:
        subtopic_raw = []
        for i, q in enumerate(QUERIES, 1):
            pub = q.split()[0]
            items = fetch_naver(q)
            kept = [it for it in items if match_pub(it.get("originallink") or it.get("link", ""), pub)]
            subtopic_raw.extend(kept)
            print(f"  [{i}/{len(QUERIES)}] {q} → {len(items)}/{len(kept)} pub_match", flush=True)
        with open(OUT_RAW, "w", encoding="utf-8") as f:
            json.dump(subtopic_raw, f, ensure_ascii=False)
        print(f"\n  subtopic raw 저장: {len(subtopic_raw)}건", flush=True)

    # 2년 필터 + 기존 URL 제외
    new_only = []
    for a in subtopic_raw:
        d = parse_date(a.get("pub_date", ""))
        if not d or d < CUTOFF:
            continue
        k1 = a.get("originallink", "")
        k2 = a.get("link", "")
        if k1 in existing_urls or k2 in existing_urls:
            continue
        new_only.append(a)
    print(f"\n  2년 필터 + 기존 제외: {len(subtopic_raw)} → {len(new_only)}", flush=True)

    # URL dedup within new
    seen = set()
    deduped = []
    for a in new_only:
        k = a.get("originallink") or a.get("link", "")
        if k and k not in seen:
            seen.add(k)
            deduped.append(a)
    print(f"  신규 URL dedup: {len(new_only)} → {len(deduped)}", flush=True)

    # 제목 dedup within new
    seen_t = set()
    new_final = []
    for a in deduped:
        t = a["title"].strip()
        if t and t not in seen_t:
            seen_t.add(t)
            new_final.append(a)
    print(f"  제목 dedup: {len(deduped)} → {len(new_final)}", flush=True)

    # 본문 fetch (캐시 사용)
    if os.path.exists(CACHE_BODIES):
        with open(CACHE_BODIES, encoding="utf-8") as f:
            cache = json.load(f)
    else:
        cache = {}

    todo = [a for a in new_final if a.get("link", "") not in cache]
    print(f"\n  본문 fetch 필요: {len(todo)}건 (캐시 히트: {len(new_final)-len(todo)})", flush=True)

    if todo:
        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(fetch_body_cached, a, cache): a for a in todo}
            for fut in as_completed(futs):
                r = fut.result()
                cache[r["url"]] = r
                done += 1
                if done % 200 == 0:
                    print(f"    {done}/{len(todo)}...", flush=True)
                    with open(CACHE_BODIES, "w", encoding="utf-8") as f:
                        json.dump(cache, f, ensure_ascii=False)
        with open(CACHE_BODIES, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    # 3회 이상 필터 (새 데이터)
    THRESHOLD = 3
    new_filtered = []
    for a in new_final:
        c = cache.get(a.get("link", ""), {})
        n = c.get("ai_count", 0)
        if n >= THRESHOLD:
            a["body_ai_count"] = n
            a["body_len"] = c.get("body_len", 0)
            new_filtered.append(a)
    print(f"\n  신규 3회 이상 필터: {len(new_final)} → {len(new_filtered)}", flush=True)

    # 기존 filtered 로드해서 병합
    with open(SRC_FILTERED, encoding="utf-8") as f:
        old_filtered = json.load(f)

    # URL 기준 dedup 병합
    seen_urls = set()
    merged = []
    for a in old_filtered + new_filtered:
        k = a.get("originallink") or a.get("link", "")
        if k and k not in seen_urls:
            seen_urls.add(k)
            merged.append(a)
    print(f"\n  병합: 기존 {len(old_filtered)} + 신규 {len(new_filtered)} → 최종 {len(merged)}", flush=True)

    with open(OUT_FINAL, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    print(f"\n  저장: {OUT_FINAL}", flush=True)

    # 최종 분포
    pub_dist = Counter()
    for a in merged:
        url = a.get("originallink") or a.get("link", "")
        d = domain(url)
        for p in PUBLISHERS:
            if match_pub(url, p):
                pub_dist[p] += 1
                break
    print(f"\n  === 최종 {len(merged)}건 매체 분포 ===", flush=True)
    for p, c in pub_dist.most_common():
        print(f"    {p:<20} {c:>5}건 ({c/len(merged)*100:.1f}%)", flush=True)

    dates = sorted(parse_date(a["pub_date"]) for a in merged if parse_date(a["pub_date"]))
    if dates:
        print(f"\n  기간: {dates[0].date()} ~ {dates[-1].date()}", flush=True)
    year_dist = Counter(d.year for d in dates)
    print(f"  연도별: {dict(sorted(year_dist.items()))}", flush=True)


if __name__ == "__main__":
    main()
