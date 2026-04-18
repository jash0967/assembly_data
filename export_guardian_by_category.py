"""Guardian 기사 정책 속성별 제목 리스트."""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ART_PATH = os.path.join(DATA_DIR, "guardian_articles_raw.json")
CLS_PATH = os.path.join(DATA_DIR, "news_guardian_classified.json")
OUT = os.path.join(DATA_DIR, "titles_guardian_by_category.md")


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


with open(ART_PATH, encoding="utf-8") as f:
    articles = json.load(f)
with open(CLS_PATH, encoding="utf-8") as f:
    classified = json.load(f)

# URL → article (id 경로형 + 전체 URL 둘 다 매핑)
by_url = {}
for a in articles:
    by_url[a.get("url", "")] = a
    by_url[a.get("id", "")] = a

# 속성 → 기사
by_cat = defaultdict(list)
for c in classified:
    attr = c.get("primary", "?")
    key = c.get("article_id", "")
    art = by_url.get(key)
    if not art:
        continue
    art["_sec"] = c.get("secondary", "")
    art["_ter"] = c.get("tertiary", "")
    by_cat[attr].append(art)

for cat in by_cat:
    by_cat[cat].sort(key=lambda a: parse_date(a.get("pub_date", "")) or datetime.min)

order = sorted(by_cat.keys(), key=lambda k: -len(by_cat[k]))

lines = []
total = sum(len(v) for v in by_cat.values())
lines.append("# Guardian AI 기사 정책 속성별 제목 리스트")
lines.append("")
lines.append(f"- 수집 기간: 2016-03 ~ 2026-04")
lines.append(f"- 쿼리: \"artificial intelligence\", \"A.I.\", AI")
lines.append(f"- 섹션 필터: technology, business, politics, world, science, law 등 14개")
lines.append(f"- 총 {total:,}건")
lines.append(f"- 출처: news_guardian_classified.json")
lines.append("")

lines.append("## 목차")
lines.append("")
for cat in order:
    anchor = cat.lower().replace("/", "").replace(" ", "-").replace("(", "").replace(")", "")
    lines.append(f"- {cat} — {len(by_cat[cat]):,}건")
lines.append("")

for cat in order:
    items = by_cat[cat]
    lines.append(f"## {cat} ({len(items):,}건)")
    lines.append("")

    # 섹션별 소분류
    by_sec = defaultdict(list)
    for a in items:
        by_sec[a.get("section", "?")].append(a)
    sec_order = sorted(by_sec.keys(), key=lambda s: -len(by_sec[s]))

    for sec in sec_order:
        lines.append(f"### section: {sec} ({len(by_sec[sec])}건)")
        lines.append("")
        for a in by_sec[sec]:
            d = parse_date(a.get("pub_date", ""))
            dstr = d.strftime("%Y-%m-%d") if d else "?"
            title = a.get("title", "").strip()
            url = a.get("url", "")
            sec_attr = a.get("_sec", "")
            sec_str = f" [2nd: {sec_attr}]" if sec_attr and sec_attr not in ("", "none", "?") else ""
            lines.append(f"- [{dstr}]{sec_str} {title}  \n  <{url}>")
        lines.append("")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"저장: {OUT}")
print(f"총 {total:,}건, {len(by_cat)}개 속성")
for cat in order:
    print(f"  {cat}: {len(by_cat[cat]):,}건")
