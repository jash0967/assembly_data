"""Guardian 제목에 AI 키워드 있는 기사만 정책 속성별 제목 리스트.

키워드: AI / artificial intelligence / A.I.
(네이버와 동일 원칙)
"""
import json
import os
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ART_PATH = os.path.join(DATA_DIR, "guardian_articles_raw.json")
CLS_PATH = os.path.join(DATA_DIR, "news_guardian_classified.json")
OUT = os.path.join(DATA_DIR, "titles_guardian_title_filtered.md")


def parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def title_has_kw(title):
    """제목에 AI 키워드 포함?"""
    if re.search(r'\bAI\b', title):
        return "AI"
    if re.search(r'artificial intelligence', title, re.IGNORECASE):
        return "artificial intelligence"
    if "A.I." in title or re.search(r'\bA\.I\b', title):
        return "A.I."
    return None


# 라벨 정규화
def normalize_label(label):
    if not label or label == "?":
        return "?"
    # 번호 제거 (1. X, 5. Y 등)
    s = re.sub(r'^\d+\.\s*', '', label).strip()
    # 소문자 통일을 영문 정규 라벨로 매핑
    mapping = {
        "national security": "National security",
        "public interest": "Public interest",
        "market efficiency and power concentration (antitrust)": "Market efficiency and power concentration (antitrust)",
        "industrial policy": "Industrial policy",
        "safety": "Safety",
        "responsible and ethical ai": "Responsible and ethical AI",
        "international collaboration": "International collaboration",
        "labor": "Labor",
        "copyright": "Copyright",
        "elections": "Elections",
        "political influence and elections": "Elections",
        "privacy and data protection": "Public interest",
        "privacy": "Public interest",
        "human rights": "Responsible and ethical AI",
        "education": "Labor",
    }
    return mapping.get(s.lower(), s)


with open(ART_PATH, encoding="utf-8") as f:
    articles = json.load(f)
with open(CLS_PATH, encoding="utf-8") as f:
    classified = json.load(f)

# URL → article (id 경로형 + 전체 URL 둘 다 매핑)
by_url = {}
for a in articles:
    by_url[a.get("url", "")] = a
    by_url[a.get("id", "")] = a

# 제목 필터 통과 + 분류 결과 결합
by_cat = defaultdict(list)
kw_dist = Counter()
total_filtered = 0

for c in classified:
    key = c.get("article_id", "")
    art = by_url.get(key)
    if not art:
        continue
    title = art.get("title", "")
    kw = title_has_kw(title)
    if not kw:
        continue
    total_filtered += 1
    kw_dist[kw] += 1

    attr = normalize_label(c.get("primary", "?"))
    art["_sec"] = normalize_label(c.get("secondary", ""))
    art["_ter"] = normalize_label(c.get("tertiary", ""))
    art["_kw"] = kw
    by_cat[attr].append(art)

for cat in by_cat:
    by_cat[cat].sort(key=lambda a: parse_date(a.get("pub_date", "")) or datetime.min)

order = sorted(by_cat.keys(), key=lambda k: -len(by_cat[k]))

lines = []
lines.append("# Guardian AI 기사 정책 속성별 제목 리스트 (제목 키워드 필터)")
lines.append("")
lines.append(f"- 수집 기간: 2016-03 ~ 2026-04")
lines.append(f"- 필터: 제목에 AI / artificial intelligence / A.I. 중 하나 포함")
lines.append(f"- 라벨 정규화 적용 (대소문자·번호 변이 통일)")
lines.append(f"- 총 {total_filtered:,}건")
lines.append("")

lines.append(f"## 키워드 매치 분포")
for k, c in kw_dist.most_common():
    lines.append(f"- {k}: {c:,}건")
lines.append("")

lines.append("## 목차")
for cat in order:
    lines.append(f"- {cat} — {len(by_cat[cat]):,}건")
lines.append("")

for cat in order:
    items = by_cat[cat]
    lines.append(f"## {cat} ({len(items):,}건)")
    lines.append("")

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
print(f"총 {total_filtered:,}건 (제목 필터), {len(by_cat)}개 속성")
print()
print("키워드 매치:")
for k, c in kw_dist.most_common():
    print(f"  {k}: {c:,}건")
print()
print("속성별:")
for cat in order:
    print(f"  {cat}: {len(by_cat[cat]):,}건")
