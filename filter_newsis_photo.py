"""뉴시스 포토 기사 제외하고 재필터링."""
import json
import os
import sys
from collections import Counter
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SRC = os.path.join(DATA_DIR, "naver_articles_v3.json")
OUT = os.path.join(DATA_DIR, "naver_articles_v3_clean.json")


def is_newsis_photo(url: str) -> bool:
    return "newsis.com" in url and "/view/NISI" in url


def domain(url):
    return url.split('//')[1].split('/')[0].removeprefix('www.') if '//' in url else ''


with open(SRC, encoding="utf-8") as f:
    arts = json.load(f)
print(f"before: {len(arts)}")

clean = []
removed = 0
for a in arts:
    url = a.get("originallink") or a.get("link", "")
    if is_newsis_photo(url):
        removed += 1
        continue
    clean.append(a)

print(f"removed (newsis photo): {removed}")
print(f"after: {len(clean)}")

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(clean, f, indent=2, ensure_ascii=False)
print(f"saved: {OUT}")

# 최종 분포
pub_dist = Counter()
PUBLISHERS = [
    "chosun.com", "joongang.co.kr", "donga.com", "hani.co.kr", "khan.co.kr",
    "mk.co.kr", "hankyung.com", "heraldcorp.com", "edaily.co.kr",
    "zdnet.co.kr", "etnews.com", "bloter.net", "ddaily.co.kr",
    "yna.co.kr", "news1.kr", "newsis.com",
]
for a in clean:
    url = a.get("originallink") or a.get("link", "")
    d = domain(url)
    for p in PUBLISHERS:
        if p in d:
            pub_dist[p] += 1
            break

print()
print("언론사별 분포:")
for p, c in pub_dist.most_common():
    print(f"  {p:<20} {c:>5}건 ({c/len(clean)*100:.1f}%)")
