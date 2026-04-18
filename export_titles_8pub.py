"""8개 언론사 기사 제목 리스트 파일로 저장."""
import json
import os
import sys
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SRC = os.path.join(DATA_DIR, "naver_articles_v3_clean.json")
OUT = os.path.join(DATA_DIR, "titles_8pub.md")

TARGETS = {
    "조선일보": "chosun.com",
    "중앙일보": "joongang.co.kr",
    "동아일보": "donga.com",
    "한겨레":   "hani.co.kr",
    "경향신문": "khan.co.kr",
    "연합뉴스": "yna.co.kr",
    "뉴스1":    "news1.kr",
    "뉴시스":   "newsis.com",
}


def domain(url):
    return url.split("//")[1].split("/")[0].removeprefix("www.") if "//" in url else ""


def parse_date(s):
    try:
        return datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
    except Exception:
        return None


def match(url, pub):
    d = domain(url)
    if pub == "yna.co.kr":
        return d == "yna.co.kr" or d.startswith("m.yna.co.kr")
    return pub in d


with open(SRC, encoding="utf-8") as f:
    arts = json.load(f)

by_pub = {name: [] for name in TARGETS}
for a in arts:
    url = a.get("originallink") or a.get("link", "")
    for name, pub in TARGETS.items():
        if match(url, pub):
            by_pub[name].append(a)
            break

# 매체별 pub_date 기준 정렬 (오래된 순)
for name in by_pub:
    by_pub[name].sort(key=lambda a: parse_date(a.get("pub_date", "")) or datetime.min.replace(tzinfo=None))

# 마크다운으로 저장
lines = []
total = sum(len(v) for v in by_pub.values())
lines.append(f"# 8개 언론사 AI 기사 제목 리스트")
lines.append(f"")
lines.append(f"- 수집 기간: 2024-04-18 ~ 2026-04-17")
lines.append(f"- 총 {total:,}건")
lines.append(f"- 출처: naver_articles_v3_clean.json")
lines.append(f"")

# 순위 순으로
order = sorted(TARGETS.keys(), key=lambda n: -len(by_pub[n]))
for name in order:
    items = by_pub[name]
    lines.append(f"## {name} ({TARGETS[name]}) — {len(items):,}건")
    lines.append(f"")
    for a in items:
        d = parse_date(a.get("pub_date", ""))
        dstr = d.strftime("%Y-%m-%d") if d else "?"
        title = a.get("title", "").strip()
        url = a.get("originallink") or a.get("link", "")
        lines.append(f"- [{dstr}] {title}  \n  <{url}>")
    lines.append(f"")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"저장: {OUT}")
print(f"총 {total:,}건, 매체 {len(TARGETS)}개")
for name in order:
    print(f"  {name}: {len(by_pub[name]):,}건")
