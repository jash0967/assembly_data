"""Compare gpt-4.1-mini vs gpt-4.1 on bill classification.

Samples N bills from an existing classified JSON, re-classifies with gpt-4.1 (full),
reports per-attribute agreement and mismatches.

Usage:
    python compare_models.py kr_22 30
"""
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from openai import OpenAI
from dotenv import load_dotenv

import _bootstrap  # noqa: F401  -- adds repo root to sys.path

from prompts import SYSTEM_PROMPT
from classify_bills import TARGETS, output_path

load_dotenv()
client = OpenAI()

SEED = 42


def classify_with(item, model: str, max_retries=5):
    text = item["text"][:3500]
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            r = json.loads(resp.choices[0].message.content)
            r["id"] = item["id"]
            r["title"] = item["title"]
            return r
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower():
                time.sleep(2 ** attempt + 1)
                continue
            return {"primary": "error", "id": item["id"], "title": item["title"], "error": err[:150]}
    return {"primary": "error", "id": item["id"], "title": item["title"], "error": "rate-limit"}


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "kr_22"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 30

    # Load existing mini results
    mini_path = output_path(target)
    with open(mini_path, encoding="utf-8") as f:
        mini_all = json.load(f)
    mini_all = [r for r in mini_all if r.get("primary") != "error"]
    mini_by_id = {r["id"]: r for r in mini_all}

    # Load source items (applies cached Stage-2 filter without re-hitting API)
    print(f"Loading items for {target}...", flush=True)
    items = TARGETS[target]()
    items_by_id = {i["id"]: i for i in items}

    # Pick intersection, sample
    common_ids = [i for i in items_by_id if i in mini_by_id]
    random.seed(SEED)
    sampled_ids = random.sample(common_ids, min(n, len(common_ids)))
    print(f"Sampled {len(sampled_ids)} bills from {len(common_ids)} common\n", flush=True)

    # Re-classify with gpt-4.1
    full_results = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(classify_with, items_by_id[bid], "gpt-4.1"): bid for bid in sampled_ids}
        for fut in as_completed(futs):
            r = fut.result()
            full_results[r["id"]] = r
            print(f"  [{len(full_results)}/{len(sampled_ids)}] {r['id']}: {r.get('primary','?')[:40]}", flush=True)

    # Compare
    primary_match = 0
    secondary_match = 0
    all_match = 0
    mismatches = []
    for bid in sampled_ids:
        m = mini_by_id[bid]
        f_ = full_results[bid]
        if f_.get("primary") == "error":
            continue
        p_match = m.get("primary") == f_.get("primary")
        s_match = set(m.get("secondary", []) or []) == set(f_.get("secondary", []) or [])
        if p_match:
            primary_match += 1
        if s_match:
            secondary_match += 1
        if p_match and s_match:
            all_match += 1
        if not p_match:
            mismatches.append({
                "id": bid,
                "title": m.get("title", "")[:80],
                "mini_primary": m.get("primary"),
                "full_primary": f_.get("primary"),
                "mini_secondary": m.get("secondary"),
                "full_secondary": f_.get("secondary"),
            })

    n_ok = sum(1 for bid in sampled_ids if full_results[bid].get("primary") != "error")
    print(f"\n=== Comparison: gpt-4.1-mini vs gpt-4.1 on {target} (n={n_ok}) ===")
    print(f"  Primary agreement:   {primary_match}/{n_ok} ({primary_match/n_ok*100:.1f}%)")
    print(f"  Secondary agreement: {secondary_match}/{n_ok} ({secondary_match/n_ok*100:.1f}%)")
    print(f"  Both match:          {all_match}/{n_ok} ({all_match/n_ok*100:.1f}%)")

    print(f"\n=== Primary mismatches ({len(mismatches)}) ===")
    for mm in mismatches:
        print(f"\n  [{mm['id']}] {mm['title']}")
        print(f"    mini: {mm['mini_primary']}")
        print(f"          sec={mm['mini_secondary']}")
        print(f"    full: {mm['full_primary']}")
        print(f"          sec={mm['full_secondary']}")

    # Save for inspection
    out = os.path.join("data", f"compare_{target}_mini_vs_full.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "target": target,
            "n": n_ok,
            "primary_match": primary_match,
            "secondary_match": secondary_match,
            "mismatches": mismatches,
            "full_results": list(full_results.values()),
        }, f, indent=2, ensure_ascii=False)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
