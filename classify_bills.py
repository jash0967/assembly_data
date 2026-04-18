"""Unified GPT classification for AI policy bills (KR / US / EU).

Uses the same prompts.SYSTEM_PROMPT as news classification for cross-comparability.

For KR bills, applies a 2-stage filter before 10-attribute classification:
  Stage 1 (keyword): AI/인공지능/A.I 3회 이상 언급 + 발의자·제목 dedup
  Stage 2 (GPT):     core / adjacent / unrelated 재분류; core+adjacent만 유지

For US/EU bills, skip the filter (already pre-filtered by source: Brennan Center,
EU AI Act publication). Only 10-attribute classification runs.

Usage:
    python classify_bills.py kr_22
    python classify_bills.py kr_19 kr_20 kr_21 kr_22
    python classify_bills.py us_118 us_119
    python classify_bills.py eu_act eu_amendments
    python classify_bills.py all

Outputs:
    data/kr_{age}_ai_filtered.json              # Stage 2 output (AI bills only)
    data/bills_classified_kr_{19,20,21,22}.json # Final classification
    data/bills_classified_us_{118,119}.json
    data/bills_classified_eu_{act,amendments}.json
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

from prompts import SYSTEM_PROMPT

load_dotenv()
client = OpenAI()

ROOT = os.path.dirname(__file__)
DATA_DIR = os.path.join(ROOT, "data")
REP_DIR = os.path.join(ROOT, "replicate_carvao", "data")

# Stage 1 (keyword) — broad AI keyword set, 3+ mentions required
AI_KEYWORDS = re.compile(r"인공지능|AI|A\.I")
MIN_AI_MENTIONS = 3

# Stage 2 (GPT filter) — core / adjacent / unrelated
AI_FILTER_PROMPT = """You are an expert Korean legislative analyst.
You will be given the title and proposal reason (제안이유) of a Korean National Assembly bill.
Classify this bill's relationship to AI (artificial intelligence).

Classification:
1. "core" — AI가 법안의 주된 목적 (AI 기본법, AI 산업육성법, AI 책임법 등)
2. "adjacent" — AI가 법안의 핵심 trigger이거나, AI 관련 실질적 조항을 포함
   - AI 기술이 야기한 문제를 해결하기 위한 법안 (딥페이크 규제, AI 허위광고 등)
   - AI 인프라/생태계 조성을 위한 법안 (데이터센터 전력, AI 학습데이터 개방 등)
   - AI 활용에 대한 구체적 규정을 신설하는 법안 (AI 채용시스템 투명성 등)
3. "unrelated" — AI가 배경으로만 언급되거나, AI를 제거해도 법안의 본질이 변하지 않음
   - "AI 시대에...", "인공지능 등 첨단기술의 발전으로..." 식의 배경 언급
   - 지역발전/행정구역 법안에서 AI를 미래 비전으로 나열
   - 세법/에너지법 등에서 AI를 여러 기술 중 하나로 언급

핵심 판단 기준: "이 법안에서 AI 관련 내용을 삭제하면 법안의 존재 이유가 사라지는가?"
- 사라진다 → core 또는 adjacent
- 사라지지 않는다 → unrelated

Respond ONLY with valid JSON:
{"classification": "core|adjacent|unrelated", "reason": "한국어 한 문장 설명", "ai_provisions": "adjacent일 경우 AI 관련 구체 조항 요약 (core/unrelated는 빈 문자열)"}"""


# =============================================================================
# Loaders — each returns a list of {"id", "title", "text"} dicts
# =============================================================================

def stage1_keyword_filter_kr(age: int) -> list[dict]:
    """Stage 1: keyword filter + dedup."""
    txt_dir = os.path.join(DATA_DIR, f"bill_txt_{age}")
    if not os.path.isdir(txt_dir):
        print(f"  WARN: {txt_dir} not found", flush=True)
        return []

    candidates = []
    for fname in os.listdir(txt_dir):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(txt_dir, fname), encoding="utf-8") as f:
            bill = json.load(f)
        reason = bill.get("reason_and_content", "") or bill.get("full_text", "") or ""
        text = bill.get("bill_name", "") + " " + reason
        if len(AI_KEYWORDS.findall(text)) < MIN_AI_MENTIONS:
            continue
        candidates.append({
            "id": bill.get("bill_id", fname.replace(".json", "")),
            "title": bill.get("bill_name", ""),
            "proposer": bill.get("proposer", ""),
            "propose_date": bill.get("propose_date", ""),
            "reason_snippet": reason[:500],
            "text": f"법안명: {bill.get('bill_name', '')}\n발의자: {bill.get('proposer', '')}\n\n{reason[:2000]}",
        })

    # Dedup: (bill_name, lead proposer) — keep latest
    groups = {}
    for c in sorted(candidates, key=lambda x: x["propose_date"]):
        lead = c["proposer"].split(",")[0].strip() if c["proposer"] else ""
        key = (c["title"], lead)
        if key not in groups or c["propose_date"] > groups[key]["propose_date"]:
            groups[key] = c
    return list(groups.values())


def _gpt_filter_one(c: dict, max_retries=5) -> dict:
    """Stage 2 worker: GPT classifies one candidate as core/adjacent/unrelated."""
    user_msg = f"법안 제목: {c['title']}\n\n제안이유 및 주요내용:\n{c.get('reason_snippet', '')}"
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": AI_FILTER_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            r = json.loads(resp.choices[0].message.content)
            cls = r.get("classification", "unrelated")
            return {
                **c,
                "classification": cls,
                "is_ai_bill": cls in ("core", "adjacent"),
                "gpt_reason": r.get("reason", ""),
                "ai_provisions": r.get("ai_provisions", ""),
            }
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower():
                time.sleep(2 ** attempt + 1)
                continue
            return {**c, "classification": None, "is_ai_bill": None, "gpt_reason": err[:150]}
    return {**c, "classification": None, "is_ai_bill": None, "gpt_reason": "max retries"}


def stage2_gpt_filter_kr(age: int, candidates: list[dict]) -> list[dict]:
    """Stage 2: GPT core/adjacent/unrelated; returns only core+adjacent, caches full results."""
    cache_path = os.path.join(DATA_DIR, f"kr_{age}_ai_filtered.json")
    # Load cache
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            for r in json.load(f):
                cache[r["id"]] = r
    # Find what's missing
    todo = [c for c in candidates if c["id"] not in cache or cache[c["id"]].get("classification") is None]
    print(f"  Stage 2 GPT filter: {len(candidates)} candidates, {len(todo)} to classify", flush=True)

    if todo:
        done = 0
        results_new = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_gpt_filter_one, c): c for c in todo}
            for fut in as_completed(futs):
                r = fut.result()
                cache[r["id"]] = r
                results_new[r["id"]] = r
                done += 1
                if done % 30 == 0:
                    print(f"    {done}/{len(todo)}...", flush=True)
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(list(cache.values()), f, ensure_ascii=False)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(list(cache.values()), f, indent=2, ensure_ascii=False)

    # Summary
    dist = Counter(r.get("classification", "?") for r in cache.values())
    print(f"  Stage 2 result: core={dist.get('core',0)}, adjacent={dist.get('adjacent',0)}, "
          f"unrelated={dist.get('unrelated',0)}, error={sum(1 for r in cache.values() if r.get('classification') is None)}", flush=True)

    ai_only = [cache[c["id"]] for c in candidates if cache.get(c["id"], {}).get("is_ai_bill") is True]
    print(f"  AI bills (core + adjacent): {len(ai_only)}", flush=True)
    return ai_only


def load_kr_bills(age: int) -> list[dict]:
    """KR bills full pipeline: keyword filter → dedup → GPT core/adjacent/unrelated."""
    print(f"  Stage 1 keyword filter + dedup...", flush=True)
    candidates = stage1_keyword_filter_kr(age)
    print(f"  Stage 1 candidates: {len(candidates)}", flush=True)
    if not candidates:
        return []
    ai_bills = stage2_gpt_filter_kr(age, candidates)
    return ai_bills


def _bill_id_to_filename(bill_id: str) -> str:
    """Convert bill_id like 'H.R. 10262' or 'S. 5379' → 'HR_10262' / 'S_5379'."""
    return bill_id.replace(".", "").replace(" ", "_")


def load_us_bills(congress: int) -> list[dict]:
    """US 118th or 119th Congress bills from replicate_carvao/data/."""
    if congress == 118:
        txt_dir = os.path.join(REP_DIR, "bills_text")
        meta_src = os.path.join(REP_DIR, "bills_processed.json")
    elif congress == 119:
        txt_dir = os.path.join(REP_DIR, "us119_bills_text")
        meta_src = os.path.join(REP_DIR, "us119_bills_processed.json")
    else:
        return []

    if not os.path.isfile(meta_src):
        print(f"  WARN: {meta_src} not found", flush=True)
        return []

    with open(meta_src, encoding="utf-8") as f:
        meta_list = json.load(f)

    out = []
    for bill in meta_list:
        bill_id = bill.get("bill_id", "")
        title = bill.get("title", "")
        summary = bill.get("brennan_summary") or bill.get("summary") or ""
        # Try to load full text file
        text = ""
        if os.path.isdir(txt_dir):
            fname = _bill_id_to_filename(bill_id) + ".txt"
            fpath = os.path.join(txt_dir, fname)
            if os.path.exists(fpath):
                with open(fpath, encoding="utf-8", errors="ignore") as f:
                    text = f.read()[:3000]
        content = text or summary or title
        out.append({
            "id": bill_id,
            "title": title,
            "text": f"Bill: {title}\nBill ID: {bill_id}\nCongress: {congress}th\n\n{content[:3000]}",
        })
    return out


def load_eu_act() -> list[dict]:
    """EU AI Act articles from data/eu_ai_act_articles.json."""
    p = os.path.join(DATA_DIR, "eu_ai_act_articles.json")
    if not os.path.exists(p):
        print(f"  WARN: {p} not found", flush=True)
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for a in data:
        num = a.get("article_num") or a.get("article_number") or a.get("id", "")
        title = a.get("title", "")
        text = a.get("text", "")
        out.append({
            "id": str(num),
            "title": title,
            "text": f"EU AI Act Article {num}: {title}\n\n{text[:2500]}",
        })
    return out


def load_eu_amendments() -> list[dict]:
    """EU AI Act amendments from data/eu_amendments.json."""
    p = os.path.join(DATA_DIR, "eu_amendments.json")
    if not os.path.exists(p):
        print(f"  WARN: {p} not found", flush=True)
        return []
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for am in data:
        amid = am.get("amendment_num") or am.get("amendment_id") or am.get("id", "")
        target = am.get("target") or am.get("subject") or am.get("title", "")
        text = am.get("text", "")
        out.append({
            "id": str(amid),
            "title": target,
            "text": f"EU AI Act Amendment {amid} (target: {target}):\n\n{text[:2500]}",
        })
    return out


# =============================================================================
# Unified registry
# =============================================================================

TARGETS = {
    "kr_19": lambda: load_kr_bills(19),
    "kr_20": lambda: load_kr_bills(20),
    "kr_21": lambda: load_kr_bills(21),
    "kr_22": lambda: load_kr_bills(22),
    "us_118": lambda: load_us_bills(118),
    "us_119": lambda: load_us_bills(119),
    "eu_act": load_eu_act,
    "eu_amendments": load_eu_amendments,
}


def output_path(target: str) -> str:
    return os.path.join(DATA_DIR, f"bills_classified_{target}.json")


# =============================================================================
# Classification worker
# =============================================================================

def classify_one(item, max_retries=5):
    """Classify single item with retry on 429."""
    text = item["text"][:3500]
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)
            result["id"] = item["id"]
            result["title"] = item["title"]
            return result
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower():
                time.sleep(2 ** attempt + 1)
                continue
            return {"primary": "error", "id": item["id"], "title": item["title"], "error": err[:150]}
    return {"primary": "error", "id": item["id"], "title": item["title"], "error": "rate-limit max retries"}


# =============================================================================
# Main pipeline per target
# =============================================================================

def run_target(target: str):
    print(f"\n=== {target} ===", flush=True)
    items = TARGETS[target]()
    print(f"  loaded: {len(items)}", flush=True)
    if not items:
        return

    out_path = output_path(target)
    existing = []
    done_ids = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            existing = json.load(f)
        err_cnt = sum(1 for r in existing if r.get("primary") == "error")
        existing = [r for r in existing if r.get("primary") != "error"]
        done_ids = {r["id"] for r in existing}
        print(f"  cached: {len(existing)} success, {err_cnt} errors dropped for retry", flush=True)

    todo = [i for i in items if i["id"] not in done_ids]
    print(f"  to classify: {len(todo)}", flush=True)

    if not todo:
        summarize(existing)
        return

    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(classify_one, i): i for i in todo}
        for fut in as_completed(futs):
            existing.append(fut.result())
            done += 1
            if done % 50 == 0:
                print(f"    {done}/{len(todo)}...", flush=True)
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    print(f"  saved: {out_path} ({len(existing)} total)", flush=True)
    summarize(existing)


def summarize(results):
    dist = Counter(r.get("primary", "?") for r in results)
    total = len(results)
    if not total:
        return
    print(f"  distribution:", flush=True)
    for attr, c in dist.most_common():
        print(f"    {attr:<55} {c:>5} ({c/total*100:.1f}%)", flush=True)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python classify_bills.py {{{' | '.join(TARGETS)} | all}}+")
        sys.exit(1)

    if "all" in sys.argv[1:]:
        targets = list(TARGETS.keys())
    else:
        targets = [t for t in sys.argv[1:] if t in TARGETS]
        if not targets:
            print(f"No valid targets. Options: {list(TARGETS.keys())} or 'all'")
            sys.exit(1)

    for t in targets:
        run_target(t)


if __name__ == "__main__":
    main()
