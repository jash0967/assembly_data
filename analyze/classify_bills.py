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
    python classify_bills.py kr_22 --force   # delete caches and re-classify everything
    python classify_bills.py all --force

Outputs:
    cache/kr_{age}_ai_filtered.json                       # Stage 2 intermediate cache
    data/processed/bills_classified_kr_{19,20,21,22}.json # Final classification
    data/processed/bills_classified_us_{118,119}.json
    data/processed/bills_classified_eu_{act,amendments}.json
"""
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import duckdb
from openai import OpenAI
from dotenv import load_dotenv

import _bootstrap  # noqa: F401  -- adds repo root to sys.path

import config
from prompts import SYSTEM_PROMPT

load_dotenv()
client = OpenAI()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (analyze/ 위)
REP_DIR = os.path.join(ROOT, "replicate_carvao", "data")

# Phase 4: classification + Stage-2 filter results live in DuckDB.
# Bump PROMPT_VERSION when AI_FILTER_PROMPT or SYSTEM_PROMPT changes.
PROMPT_VERSION = "v2_en_20260418"

# Single shared write connection + lock — DuckDB serialises writes per process.
_db_lock = threading.Lock()
_db_con: duckdb.DuckDBPyConnection | None = None


def _open_reader() -> duckdb.DuckDBPyConnection:
    """Open analysis DB read-only and ATTACH raw read-only.

    Used as fallback when the shared writer connection (_db_con) is not open.
    """
    con = duckdb.connect(config.ANALYSIS_DB_PATH, read_only=True)
    con.execute(f"ATTACH '{config.RAW_DB_PATH}' AS raw (READ_ONLY)")
    return con

# Stage 1 (keyword) — broad AI keyword set, 3+ mentions required
AI_KEYWORDS = re.compile(r"인공\s*지능|(?<![A-Za-z])AI(?![A-Za-z])|A\.I\.?")
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
    """Stage 1: keyword filter + dedup. Reads from bill_text JOIN v_bill."""
    con = _db_con if _db_con is not None else _open_reader()
    must_close = _db_con is None
    try:
        rows = con.execute(
            """
            SELECT t.bill_id,
                   COALESCE(b.bill_name, '')           AS bill_name,
                   COALESCE(b.lead_proposer, '')        AS lead_proposer,
                   COALESCE(CAST(b.propose_date AS VARCHAR), '') AS propose_date,
                   COALESCE(t.reason_and_content, '')   AS reason_and_content,
                   COALESCE(t.full_text, '')            AS full_text
            FROM raw.bill_text t
            LEFT JOIN raw.v_bill b USING (bill_id)
            WHERE t.age = ?
            """,
            [age],
        ).fetchall()
    finally:
        if must_close:
            con.close()

    candidates: list[dict] = []
    for bill_id, bill_name, lead, propose_date, reason, full_text in rows:
        text_for_kw = f"{bill_name} {reason or full_text}"
        if len(AI_KEYWORDS.findall(text_for_kw)) < MIN_AI_MENTIONS:
            continue
        body = reason or full_text
        candidates.append({
            "id": bill_id,
            "title": bill_name,
            "proposer": lead,
            "propose_date": propose_date,
            "reason_snippet": body[:500],
            "text": f"법안명: {bill_name}\n발의자: {lead}\n\n{body[:2000]}",
        })

    # Dedup: (bill_name, lead proposer) — keep latest
    groups: dict[tuple[str, str], dict] = {}
    for c in sorted(candidates, key=lambda x: x["propose_date"]):
        key = (c["title"], c["proposer"])
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


_AI_FILTER_UPSERT = """
INSERT INTO bill_ai_filter
    (bill_id, age, classification, is_ai_bill, gpt_reason, ai_provisions)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (bill_id) DO UPDATE SET
    age            = EXCLUDED.age,
    classification = EXCLUDED.classification,
    is_ai_bill     = EXCLUDED.is_ai_bill,
    gpt_reason     = EXCLUDED.gpt_reason,
    ai_provisions  = EXCLUDED.ai_provisions,
    filtered_at    = now();
"""


def _save_ai_filter(rec: dict, age: int) -> None:
    if rec.get("classification") is None:
        return  # don't persist GPT errors
    with _db_lock:
        _db_con.execute(_AI_FILTER_UPSERT, [
            rec["id"], age, rec.get("classification"),
            bool(rec.get("is_ai_bill", False)),
            rec.get("gpt_reason"), rec.get("ai_provisions"),
        ])


def _load_ai_filter_cache(age: int) -> dict[str, dict]:
    con = _db_con if _db_con is not None else _open_reader()
    must_close = _db_con is None
    try:
        rows = con.execute(
            "SELECT bill_id, classification, is_ai_bill, gpt_reason, ai_provisions "
            "FROM bill_ai_filter WHERE age = ?",
            [age],
        ).fetchall()
    finally:
        if must_close:
            con.close()
    return {
        bid: {"id": bid, "classification": cls, "is_ai_bill": bool(is_ai),
              "gpt_reason": reason, "ai_provisions": prov}
        for bid, cls, is_ai, reason, prov in rows
    }


def stage2_gpt_filter_kr(age: int, candidates: list[dict]) -> list[dict]:
    """Stage 2: GPT core/adjacent/unrelated; returns only core+adjacent.

    Cache lives in DuckDB.bill_ai_filter; partial results are persisted as
    each future completes so a Ctrl-C is safe.
    """
    cache = _load_ai_filter_cache(age)
    todo = [c for c in candidates if c["id"] not in cache]
    print(f"  Stage 2 GPT filter: {len(candidates)} candidates, {len(todo)} to classify",
          flush=True)

    if todo:
        done = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_gpt_filter_one, c): c for c in todo}
            for fut in as_completed(futs):
                r = fut.result()
                cache[r["id"]] = r
                _save_ai_filter(r, age)
                done += 1
                if done % 30 == 0:
                    print(f"    {done}/{len(todo)}...", flush=True)

    dist = Counter(r.get("classification", "?") for r in cache.values())
    err = sum(1 for r in cache.values() if r.get("classification") is None)
    print(
        f"  Stage 2 result: core={dist.get('core', 0)}, adjacent={dist.get('adjacent', 0)}, "
        f"unrelated={dist.get('unrelated', 0)}, error={err}",
        flush=True,
    )

    ai_only = [cache[c["id"]] for c in candidates
               if cache.get(c["id"], {}).get("is_ai_bill") is True]
    # Re-attach the candidate's text/title for downstream classification
    by_id = {c["id"]: c for c in candidates}
    for r in ai_only:
        if r["id"] in by_id:
            r.setdefault("title", by_id[r["id"]]["title"])
            r.setdefault("text", by_id[r["id"]]["text"])
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
    p = os.path.join(config.BILLS_EU_DIR, "eu_ai_act_articles.json")
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
    p = os.path.join(config.BILLS_EU_DIR, "eu_amendments.json")
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


_CLASSIFICATION_UPSERT = """
INSERT INTO bill_classifications
    (bill_id, source, prompt_version, primary_attr, secondary_attr, tertiary_attr, title)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (bill_id, source, prompt_version) DO UPDATE SET
    primary_attr   = EXCLUDED.primary_attr,
    secondary_attr = EXCLUDED.secondary_attr,
    tertiary_attr  = EXCLUDED.tertiary_attr,
    title          = EXCLUDED.title,
    classified_at  = now();
"""


def _save_classification(rec: dict, source: str) -> None:
    if rec.get("primary") == "error":
        return  # don't persist API errors — re-runs will retry
    with _db_lock:
        _db_con.execute(_CLASSIFICATION_UPSERT, [
            rec["id"], source, PROMPT_VERSION,
            rec.get("primary"), rec.get("secondary"), rec.get("tertiary"),
            rec.get("title"),
        ])


def _load_existing_classification(source: str) -> dict[str, dict]:
    con = _db_con if _db_con is not None else _open_reader()
    must_close = _db_con is None
    try:
        rows = con.execute(
            "SELECT bill_id, primary_attr, secondary_attr, tertiary_attr, title "
            "FROM bill_classifications WHERE source = ? AND prompt_version = ?",
            [source, PROMPT_VERSION],
        ).fetchall()
    finally:
        if must_close:
            con.close()
    return {
        bid: {"id": bid, "primary": p, "secondary": s, "tertiary": t, "title": title}
        for bid, p, s, t, title in rows
    }


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

    cache = _load_existing_classification(target)
    current_ids = {i["id"] for i in items}
    stale_in_db = [bid for bid in cache if bid not in current_ids]
    print(f"  cached: {len(cache)} success in DB, {len(stale_in_db)} stale (not re-fetched)",
          flush=True)

    todo = [i for i in items if i["id"] not in cache]
    print(f"  to classify: {len(todo)}", flush=True)

    if todo:
        done = 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(classify_one, i): i for i in todo}
            for fut in as_completed(futs):
                rec = fut.result()
                cache[rec["id"]] = rec
                _save_classification(rec, target)
                done += 1
                if done % 50 == 0:
                    print(f"    {done}/{len(todo)}...", flush=True)

    print(f"  saved to bill_classifications (source={target}, version={PROMPT_VERSION}): "
          f"{len(cache)} total", flush=True)
    summarize(list(cache.values()))


def summarize(results):
    dist = Counter(r.get("primary", "?") for r in results)
    total = len(results)
    if not total:
        return
    print(f"  distribution:", flush=True)
    for attr, c in dist.most_common():
        print(f"    {attr:<55} {c:>5} ({c/total*100:.1f}%)", flush=True)


def purge_caches(targets: list[str]) -> None:
    """Delete Stage 2 AI filter and 10-attribute classification rows for given targets.

    Reuses the shared write connection (`_init_db()` runs first in `main()`).
    Opening a second connection here was pointless — DuckDB hands back the same
    instance for the same file+config — and the old `finally: if must_close:`
    referenced a name that was never defined in this function, so `--force`
    crashed with NameError right after the DELETEs committed.
    """
    con = _db_con if _db_con is not None else duckdb.connect(config.ANALYSIS_DB_PATH)
    must_close = _db_con is None
    try:
        cls_total = 0
        ai_total = 0
        for t in targets:
            # DuckDB의 DELETE 는 삭제 행 수를 결과로 돌려준다.
            cls_total += con.execute(
                "DELETE FROM bill_classifications WHERE source = ? AND prompt_version = ?",
                [t, PROMPT_VERSION],
            ).fetchone()[0]
            if t.startswith("kr_"):
                age = int(t.split("_")[1])
                ai_total += con.execute(
                    "DELETE FROM bill_ai_filter WHERE age = ?", [age]
                ).fetchone()[0]
        con.commit()
    finally:
        if must_close:
            con.close()
    print(f"--force: deleted {cls_total:,} classification + {ai_total:,} AI-filter rows "
          f"for {len(targets)} target(s) (version={PROMPT_VERSION})", flush=True)


_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS prompt_versions (
    version     VARCHAR PRIMARY KEY,
    description TEXT,
    released_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bill_classifications (
    bill_id        VARCHAR NOT NULL,
    source         VARCHAR NOT NULL,
    prompt_version VARCHAR NOT NULL,
    primary_attr   VARCHAR,
    secondary_attr VARCHAR,
    tertiary_attr  VARCHAR,
    title          VARCHAR,
    classified_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bill_id, source, prompt_version)
);
CREATE TABLE IF NOT EXISTS bill_ai_filter (
    bill_id        VARCHAR PRIMARY KEY,
    age            INTEGER NOT NULL,
    classification VARCHAR,
    is_ai_bill     BOOLEAN,
    gpt_reason     TEXT,
    ai_provisions  TEXT,
    filtered_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _init_db() -> None:
    """Open the shared analysis-DB write connection, ATTACH raw read-only, and ensure schema."""
    global _db_con
    _db_con = duckdb.connect(config.ANALYSIS_DB_PATH)
    _db_con.execute(f"ATTACH '{config.RAW_DB_PATH}' AS raw (READ_ONLY)")
    _db_con.execute(_SCHEMA_DDL)
    _db_con.execute(
        "INSERT INTO prompt_versions (version, description) VALUES (?, ?) "
        "ON CONFLICT (version) DO NOTHING",
        [PROMPT_VERSION, "10-attribute SYSTEM_PROMPT (English labels)"],
    )


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    force = "--force" in flags

    if not args:
        print(f"Usage: python classify_bills.py {{{' | '.join(TARGETS)} | all}}+ [--force]")
        sys.exit(1)

    if "all" in args:
        targets = list(TARGETS.keys())
    else:
        targets = [t for t in args if t in TARGETS]
        if not targets:
            print(f"No valid targets. Options: {list(TARGETS.keys())} or 'all'")
            sys.exit(1)

    _init_db()

    if force:
        purge_caches(targets)

    try:
        for t in targets:
            run_target(t)
    finally:
        if _db_con is not None:
            _db_con.close()


if __name__ == "__main__":
    import db_audit
    with db_audit.audit_run(__file__, config.ANALYSIS_DB_PATH, argv=sys.argv[1:]):
        main()
