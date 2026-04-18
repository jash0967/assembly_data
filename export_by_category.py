"""정책 속성별로 기사 제목 묶어서 파일로 저장."""
import json
import os
import sys
from collections import defaultdict, Counter
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ART_PATH = os.path.join(DATA_DIR, "naver_articles_title_filtered.json")
CLS_PATH = os.path.join(DATA_DIR, "news_naver_title_filtered_classified_v2.json")
OUT = os.path.join(DATA_DIR, "titles_by_category_v2.md")


def parse_date(s):
    try:
        return datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
    except Exception:
        return None


with open(ART_PATH, encoding="utf-8") as f:
    articles = json.load(f)
with open(CLS_PATH, encoding="utf-8") as f:
    classified = json.load(f)

# URL → 기사 매핑
by_url = {}
for a in articles:
    url = a.get("originallink") or a.get("link", "")
    by_url[url] = a

# 속성 → 기사 묶음
by_cat = defaultdict(list)
for c in classified:
    attr = c.get("primary", "?")
    url = c.get("article_id", "")
    if url in by_url:
        art = by_url[url]
        art["_secondary"] = c.get("secondary", "")
        art["_tertiary"] = c.get("tertiary", "")
        by_cat[attr].append(art)

# 각 카테고리 내 pub_date 정렬
for cat in by_cat:
    by_cat[cat].sort(key=lambda a: parse_date(a.get("pub_date", "")) or datetime.min.replace(tzinfo=None))

# 속성 순서 (건수 많은 순)
order = sorted(by_cat.keys(), key=lambda k: -len(by_cat[k]))

lines = []
lines.append("# 정책 속성별 AI 기사 제목 리스트 (v2 강화 프롬프트)")
lines.append("")
lines.append(f"- 수집 기간: 2024-04-17 ~ 2026-04-17 (2년)")
lines.append(f"- 8개 언론사 × 제목 키워드 필터")
lines.append(f"- Carvão et al. (2025) 프레임워크 정의 기반 GPT 분류")
lines.append(f"- 총 {sum(len(v) for v in by_cat.values()):,}건")
lines.append("")

# 목차
lines.append("## 목차")
lines.append("")
for cat in order:
    lines.append(f"- [{cat}](#{cat.replace('/', '').replace(' ', '-').lower()}) — {len(by_cat[cat]):,}건")
lines.append("")

for cat in order:
    items = by_cat[cat]
    lines.append(f"## {cat} ({len(items):,}건)")
    lines.append("")

    # 매체별 소분류
    by_pub = defaultdict(list)
    for a in items:
        by_pub[a.get("publisher", "?")].append(a)
    pub_order = sorted(by_pub.keys(), key=lambda p: -len(by_pub[p]))

    for pub in pub_order:
        lines.append(f"### {pub} ({len(by_pub[pub])}건)")
        lines.append("")
        for a in by_pub[pub]:
            d = parse_date(a.get("pub_date", ""))
            dstr = d.strftime("%Y-%m-%d") if d else "?"
            title = a.get("title", "").strip()
            url = a.get("originallink") or a.get("link", "")
            sec = a.get("_secondary", "")
            sec_str = f" [2차: {sec}]" if sec and sec not in ("", "none", "?") else ""
            lines.append(f"- [{dstr}]{sec_str} {title}  \n  <{url}>")
        lines.append("")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"저장: {OUT}")
print(f"총 {sum(len(v) for v in by_cat.values()):,}건, {len(by_cat)}개 속성")
for cat in order:
    print(f"  {cat}: {len(by_cat[cat]):,}건")
