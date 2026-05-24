"""Unified GPT classification for AI policy news articles (Guardian / NYT).

Companion to classify_bills.py (same SYSTEM_PROMPT, same --force / stale-drop semantics).

Usage:
    python classify_articles.py guardian
    python classify_articles.py all
    python classify_articles.py all --force        # delete outputs, reclassify everything

Both sources use the same English v2 prompt from prompts.py. Articles are
title-filtered (must contain AI / artificial intelligence / A.I.) before
classification.

Outputs:
    data/analysis/articles_classified_{guardian,nyt}.json

(Korean domestic news lives in data/news/news.duckdb and is classified by a
separate pipeline — not handled here.)
"""
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from openai import OpenAI
from dotenv import load_dotenv

import _bootstrap  # noqa: F401  -- adds repo root to sys.path

import config
from prompts import SYSTEM_PROMPT, ATTRIBUTES

load_dotenv()
client = OpenAI()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (analyze/ 위)

os.makedirs(config.ANALYSIS_DIR, exist_ok=True)

SOURCE_CONFIG = {
    "guardian": {
        "raw": "guardian_articles_raw.json",
        "title_key": "title",
        "desc_key": "trail_text",
        "url_key": "url",
    },
    "nyt": {
        "raw": "nyt_articles_raw.json",
        "title_key": "title",
        "desc_key": "abstract",
        "url_key": "url",
    },
}


def output_path(source: str) -> str:
    return os.path.join(config.ANALYSIS_DIR, f"articles_classified_{source}.json")


def title_has_kw(title):
    """Title keyword filter: AI / artificial intelligence / A.I. / 인공지능."""
    if re.search(r'\bAI\b', title):
        return True
    if re.search(r'artificial intelligence', title, re.IGNORECASE):
        return True
    if "A.I." in title or re.search(r'\bA\.I\b', title):
        return True
    if "인공지능" in title or "인공 지능" in title:
        return True
    return False


def get_url(article, cfg):
    if cfg["url_key"]:
        return article.get(cfg["url_key"], "")
    return article.get("originallink") or article.get("link", "")


def classify_one(article, cfg, max_retries=5):
    """Classify a single article. Retries on 429 with exponential backoff."""
    title = article.get(cfg["title_key"], "")
    desc = article.get(cfg["desc_key"], "") or ""
    url = get_url(article, cfg)
    user_msg = f"Title: {title}\n\nDescription: {desc[:800]}"

    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg[:3000]},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)
            result["article_id"] = url
            result["title"] = title
            return result
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower():
                wait = 2 ** attempt + 1
                time.sleep(wait)
                continue
            return {"primary": "error", "article_id": url, "title": title, "error": err_str[:150]}
    return {"primary": "error", "article_id": url, "title": title, "error": "max retries exceeded (rate limit)"}


def filter_and_classify(source):
    cfg = SOURCE_CONFIG[source]
    src_path = os.path.join(config.NEWS_DIR, cfg["raw"])
    out_path = output_path(source)

    with open(src_path, encoding="utf-8") as f:
        articles = json.load(f)
    print(f"=== {source.upper()} classification ===", flush=True)
    print(f"  raw: {len(articles)}", flush=True)

    filtered = [a for a in articles if title_has_kw(a.get(cfg["title_key"], ""))]
    print(f"  title-filtered: {len(filtered)}", flush=True)

    current_urls = {get_url(a, cfg) for a in filtered}

    existing = []
    done_urls = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)
        err_count = sum(1 for a in existing if a.get("primary") == "error")
        kept = [a for a in existing if a.get("primary") != "error" and a.get("article_id") in current_urls]
        stale_count = len(existing) - err_count - len(kept)
        existing = kept
        done_urls = {a["article_id"] for a in existing}
        print(f"  cached: {len(existing)} success, {err_count} errors dropped, {stale_count} stale dropped", flush=True)

    todo = [a for a in filtered if get_url(a, cfg) not in done_urls]
    print(f"  to classify: {len(todo)}", flush=True)

    if not todo:
        print(f"  nothing new to do", flush=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        return existing

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(classify_one, a, cfg): a for a in todo}
        for fut in as_completed(futs):
            result = fut.result()
            existing.append(result)
            done += 1
            if done % 100 == 0:
                print(f"    {done}/{len(todo)}...", flush=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    print(f"  saved: {out_path} ({len(existing)} total)", flush=True)
    return existing


def summarize(source, results):
    print(f"\n  === {source.upper()} distribution ===", flush=True)
    dist = Counter(r.get("primary", "?") for r in results)
    total = len(results) or 1
    for attr, c in dist.most_common():
        print(f"    {attr:<55} {c:>5} ({c/total*100:.1f}%)", flush=True)


def purge_outputs(sources: list[str]) -> None:
    removed = []
    for s in sources:
        p = output_path(s)
        if os.path.exists(p):
            os.remove(p)
            removed.append(p)
    if removed:
        print("--force: deleted outputs:", flush=True)
        for p in removed:
            print(f"  - {os.path.relpath(p, ROOT)}", flush=True)
    else:
        print("--force: no outputs found to delete.", flush=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    force = "--force" in flags

    if not args:
        print(f"Usage: python classify.py {{{' | '.join(SOURCE_CONFIG.keys())} | all}} [--force]", flush=True)
        sys.exit(1)

    if "all" in args:
        targets = list(SOURCE_CONFIG.keys())
    else:
        targets = [t for t in args if t in SOURCE_CONFIG]
        if not targets:
            print(f"Unknown source. Options: {list(SOURCE_CONFIG.keys())} or 'all'", flush=True)
            sys.exit(1)

    if force:
        purge_outputs(targets)

    for src in targets:
        results = filter_and_classify(src)
        summarize(src, results)


if __name__ == "__main__":
    main()
