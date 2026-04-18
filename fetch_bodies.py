"""네이버 뉴스 페이지에서 본문 추출 후 AI 키워드 카운트.

- 대상: 8개 매체 총 2,181건
- 소스: n.news.naver.com (사이드바 없는 깨끗한 페이지)
- 캐시: 실패/중단 시 resume 가능
- 필터: 본문 내 AI + 인공지능 등장 >= 3회
"""
import json
import os
import re
import sys
import time
from collections import Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SRC = os.path.join(DATA_DIR, "naver_articles_v3_clean.json")
CACHE = os.path.join(DATA_DIR, "naver_bodies_cache.json")
OUT_FILTERED = os.path.join(DATA_DIR, "naver_articles_filtered.json")

TARGETS = {
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
    "yna.co.kr", "news1.kr", "newsis.com",
}
THRESHOLD = 3
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def domain(url):
    return url.split("//")[1].split("/")[0].removeprefix("www.") if "//" in url else ""


def match_pub(url):
    d = domain(url)
    for t in TARGETS:
        if t == "yna.co.kr":
            if d == "yna.co.kr" or d.startswith("m.yna.co.kr"):
                return True
        elif t in d:
            return True
    return False


def extract_body(html: str) -> str:
    """네이버 뉴스 본문 추출."""
    for pat in [
        r'<article[^>]*id="dic_area"[^>]*>(.*?)</article>',
        r'<div[^>]*id="newsct_article"[^>]*>(.*?)</div>\s*<div[^>]*class="[^"]*media_end',
        r'<div[^>]*id="newsct_article"[^>]*>(.*?)</div>',
    ]:
        m = re.search(pat, html, re.DOTALL)
        if m:
            body = re.sub(r'<[^>]+>', ' ', m.group(1))
            body = re.sub(r'\s+', ' ', body).strip()
            if len(body) > 100:
                return body
    return ""


def count_ai(text: str) -> int:
    return len(re.findall(r'\bAI\b', text)) + len(re.findall(r'인공지능', text)) + len(re.findall(r'A\.I\.', text))


def fetch_body(item):
    """단일 기사 본문 fetch 및 카운트."""
    naver_link = item.get("link", "")
    if "n.news.naver.com" not in naver_link:
        return {"url": naver_link, "body_len": 0, "ai_count": 0, "status": "no_naver"}
    try:
        r = requests.get(naver_link, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return {"url": naver_link, "body_len": 0, "ai_count": 0, "status": f"http_{r.status_code}"}
        body = extract_body(r.text)
        if not body:
            return {"url": naver_link, "body_len": 0, "ai_count": 0, "status": "no_body"}
        n = count_ai(body)
        return {"url": naver_link, "body_len": len(body), "ai_count": n, "status": "ok"}
    except Exception as e:
        return {"url": naver_link, "body_len": 0, "ai_count": 0, "status": f"err:{str(e)[:50]}"}


def main():
    with open(SRC, encoding="utf-8") as f:
        all_arts = json.load(f)

    # 8개 매체 필터
    targets = [a for a in all_arts if match_pub(a.get("originallink") or a.get("link", ""))]
    print(f"대상: {len(targets)}건", flush=True)

    # 캐시 로드
    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            cache = json.load(f)
        print(f"캐시: {len(cache)}건", flush=True)

    # 미처리만
    todo = []
    for a in targets:
        key = a.get("link", "")
        if key and key not in cache:
            todo.append(a)
    print(f"미처리: {len(todo)}건", flush=True)

    if todo:
        print(f"병렬 fetch 시작 (workers=8)...", flush=True)
        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(fetch_body, a): a for a in todo}
            for fut in as_completed(futs):
                a = futs[fut]
                key = a.get("link", "")
                cache[key] = fut.result()
                done += 1
                if done % 100 == 0:
                    print(f"  {done}/{len(todo)}...", flush=True)
                    with open(CACHE, "w", encoding="utf-8") as f:
                        json.dump(cache, f, ensure_ascii=False)

        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    # 통계
    status_dist = Counter(v["status"] for v in cache.values())
    print(f"\n상태 분포: {dict(status_dist)}", flush=True)

    # 필터 적용
    filtered = []
    ai_dist = Counter()
    for a in targets:
        c = cache.get(a.get("link", ""), {})
        n = c.get("ai_count", 0)
        ai_dist[n] += 1
        if n >= THRESHOLD:
            a["body_ai_count"] = n
            a["body_len"] = c.get("body_len", 0)
            filtered.append(a)

    print(f"\n본문 내 AI 등장 횟수 분포 (상위 10):", flush=True)
    for n in sorted(ai_dist.keys())[:15]:
        print(f"  {n}회: {ai_dist[n]}건", flush=True)

    print(f"\n{THRESHOLD}회 이상 필터: {len(targets)} → {len(filtered)}", flush=True)

    # 매체별 분포
    print(f"\n필터 후 매체별 분포:", flush=True)
    pub_dist = Counter()
    for a in filtered:
        url = a.get("originallink") or a.get("link", "")
        d = domain(url)
        for t in TARGETS:
            if t == "yna.co.kr":
                if d == "yna.co.kr" or d.startswith("m.yna.co.kr"):
                    pub_dist[t] += 1
                    break
            elif t in d:
                pub_dist[t] += 1
                break
    for p, c in pub_dist.most_common():
        pct = c/len(filtered)*100 if filtered else 0
        print(f"  {p:<20} {c:>5}건 ({pct:.1f}%)", flush=True)

    with open(OUT_FILTERED, "w", encoding="utf-8") as f:
        json.dump(filtered, f, indent=2, ensure_ascii=False)
    print(f"\n저장: {OUT_FILTERED}", flush=True)


if __name__ == "__main__":
    main()
