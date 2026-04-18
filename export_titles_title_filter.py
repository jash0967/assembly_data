"""제목 키워드 필터 적용한 기사 리스트.

소스: broad (v3_clean) + subtopic (raw) 병합
필터: 제목에 AI / 인공지능 / 인공 지능 / A.I. 중 하나 포함
"""
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SRC_V3 = os.path.join(DATA_DIR, "naver_articles_v3_clean.json")
SRC_SUB = os.path.join(DATA_DIR, "naver_articles_subtopic_raw.json")
OUT_JSON = os.path.join(DATA_DIR, "naver_articles_title_filtered.json")
OUT_MD = os.path.join(DATA_DIR, "titles_title_filtered.md")

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


def title_has_kw(title):
    """제목에 4개 키워드 중 하나 포함 여부."""
    if re.search(r'\bAI\b', title):
        return "AI"
    if "인공지능" in title:
        return "인공지능"
    if "인공 지능" in title:
        return "인공 지능"
    if "A.I." in title or re.search(r'\bA\.I\b', title):
        return "A.I."
    return None


def is_newsis_photo(url):
    return "newsis.com" in url and "/view/NISI" in url


# 데이터 병합
with open(SRC_V3, encoding="utf-8") as f:
    v3 = json.load(f)
with open(SRC_SUB, encoding="utf-8") as f:
    sub = json.load(f)
print(f"v3_clean: {len(v3)} / subtopic_raw: {len(sub)}")

merged_raw = v3 + sub

# 언론사 매치, 2년 필터, 뉴시스 포토 제외, 제목 필터, dedup
seen_urls = set()
seen_titles = set()
final = []
for a in merged_raw:
    url = a.get("originallink") or a.get("link", "")
    # 뉴시스 포토 제외
    if is_newsis_photo(url):
        continue
    # 8개 매체 매치
    pub_match = None
    for name, pub in TARGETS.items():
        if match_pub(url, pub):
            pub_match = name
            break
    if not pub_match:
        continue
    # 2년 필터
    d = parse_date(a.get("pub_date", ""))
    if not d or d < CUTOFF:
        continue
    # 제목 필터
    kw = title_has_kw(a.get("title", ""))
    if not kw:
        continue
    # dedup (URL)
    k = a.get("originallink") or a.get("link", "")
    if k in seen_urls:
        continue
    seen_urls.add(k)
    # dedup (제목)
    t = a["title"].strip()
    if t in seen_titles:
        continue
    seen_titles.add(t)

    a["publisher"] = pub_match
    a["title_kw"] = kw
    final.append(a)

print(f"최종: {len(final)}건")

# 매체별 분포
pub_dist = Counter(a["publisher"] for a in final)
print()
print("매체별 분포:")
for name in sorted(TARGETS.keys(), key=lambda n: -pub_dist[n]):
    c = pub_dist[name]
    print(f"  {name}: {c}건 ({c/len(final)*100:.1f}%)")

# 키워드 분포
kw_dist = Counter(a["title_kw"] for a in final)
print()
print("키워드 분포:")
for k, c in kw_dist.most_common():
    print(f"  {k}: {c}건")

# JSON 저장
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(final, f, indent=2, ensure_ascii=False)
print(f"\n저장: {OUT_JSON}")

# 마크다운 저장
by_pub = {name: [] for name in TARGETS}
for a in final:
    by_pub[a["publisher"]].append(a)

for name in by_pub:
    by_pub[name].sort(key=lambda a: parse_date(a.get("pub_date", "")) or datetime.min.replace(tzinfo=None))

lines = []
lines.append("# 8개 언론사 AI 기사 제목 리스트 (제목 키워드 필터)")
lines.append("")
lines.append(f"- 수집 기간: 2024-04-17 ~ 2026-04-17 (정확히 2년)")
lines.append(f"- 쿼리: broad(인공지능, AI) + 세부 10개 주제")
lines.append(f"- 필터: 제목에 AI / 인공지능 / 인공 지능 / A.I. 중 하나 포함")
lines.append(f"- 총 {len(final):,}건")
lines.append(f"- 출처: naver_articles_title_filtered.json")
lines.append("")

order = sorted(TARGETS.keys(), key=lambda n: -len(by_pub[n]))
for name in order:
    items = by_pub[name]
    lines.append(f"## {name} ({TARGETS[name]}) — {len(items):,}건")
    lines.append("")
    for a in items:
        d = parse_date(a.get("pub_date", ""))
        dstr = d.strftime("%Y-%m-%d") if d else "?"
        title = a.get("title", "").strip()
        url = a.get("originallink") or a.get("link", "")
        lines.append(f"- [{dstr}] {title}  \n  <{url}>")
    lines.append("")

with open(OUT_MD, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print(f"저장: {OUT_MD}")
