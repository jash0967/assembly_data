"""소스별 정책 속성별 기사 제목 리스트 (Guardian / NYT / Naver).

Usage:
    python export_titles.py guardian
    python export_titles.py nyt
    python export_titles.py naver
    python export_titles.py all
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

SOURCES = {
    "guardian": {
        "raw": "guardian_articles_raw.json",
        "cls": "news_guardian_classified.json",
        "out": "titles_guardian_by_category.md",
        "title_key": "title",
        "url_key": "url",
        "date_key": "pub_date",
        "date_fmt": "%Y-%m-%dT%H:%M:%SZ",
        "group_key": "section",
        "group_label": "section",
    },
    "nyt": {
        "raw": "nyt_articles_raw.json",
        "cls": "news_nyt_classified.json",
        "out": "titles_nyt_by_category.md",
        "title_key": "title",
        "url_key": "url",
        "date_key": "pub_date",
        "date_fmt": "%Y-%m-%dT%H:%M:%S%z",
        "group_key": "desk",
        "group_label": "desk",
    },
    "naver": {
        "raw": "naver_articles_title_filtered.json",
        "cls": "news_naver_classified.json",
        "out": "titles_naver_by_category.md",
        "title_key": "title",
        "url_key": None,  # originallink or link
        "date_key": "pub_date",
        "date_fmt": "%a, %d %b %Y %H:%M:%S %z",
        "group_key": "publisher",
        "group_label": "매체",
    },
}


def title_has_kw(title):
    if re.search(r'\bAI\b', title): return True
    if re.search(r'artificial intelligence', title, re.IGNORECASE): return True
    if "A.I." in title or re.search(r'\bA\.I\b', title): return True
    if "인공지능" in title or "인공 지능" in title: return True
    return False


def get_url(a, cfg):
    if cfg["url_key"]:
        return a.get(cfg["url_key"], "")
    return a.get("originallink") or a.get("link", "")


def parse_date(s, fmt):
    if not s:
        return None
    try:
        d = datetime.strptime(s, fmt)
        return d.replace(tzinfo=None) if d.tzinfo else d
    except Exception:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except Exception:
            return None


def export(source):
    cfg = SOURCES[source]
    raw_path = os.path.join(DATA_DIR, cfg["raw"])
    cls_path = os.path.join(DATA_DIR, cfg["cls"])
    out_path = os.path.join(DATA_DIR, cfg["out"])

    with open(raw_path, encoding="utf-8") as f:
        articles = json.load(f)
    with open(cls_path, encoding="utf-8") as f:
        classified = json.load(f)

    # URL → article
    by_url = {}
    for a in articles:
        by_url[get_url(a, cfg)] = a
        # Guardian id 경로형도 매핑
        if source == "guardian" and "id" in a:
            by_url[a["id"]] = a

    # 제목 필터 + 카테고리 그룹핑
    by_cat = defaultdict(list)
    total = 0
    for c in classified:
        url = c.get("article_id", "")
        art = by_url.get(url)
        if not art:
            continue
        title = art.get(cfg["title_key"], "")
        if not title_has_kw(title):
            continue
        attr = c.get("primary", "?")
        art["_sec"] = c.get("secondary", "")
        art["_ter"] = c.get("tertiary", "")
        by_cat[attr].append(art)
        total += 1

    # 각 카테고리 내 pub_date 정렬
    for cat in by_cat:
        by_cat[cat].sort(key=lambda a: parse_date(a.get(cfg["date_key"], ""), cfg["date_fmt"]) or datetime.min)

    order = sorted(by_cat.keys(), key=lambda k: -len(by_cat[k]))

    lines = []
    lines.append(f"# {source.upper()} AI 기사 정책 속성별 제목 리스트")
    lines.append("")
    lines.append(f"- 소스: {source}")
    lines.append(f"- 제목 필터: AI / artificial intelligence / A.I. / 인공지능")
    lines.append(f"- 분류: Carvão et al. (2025) 10-attribute framework (통일 영문 v2 프롬프트)")
    lines.append(f"- 총 {total:,}건")
    lines.append("")

    lines.append("## 목차")
    for cat in order:
        lines.append(f"- {cat} — {len(by_cat[cat]):,}건")
    lines.append("")

    for cat in order:
        items = by_cat[cat]
        lines.append(f"## {cat} ({len(items):,}건)")
        lines.append("")

        # 그룹 기준 (section / desk / publisher)
        by_grp = defaultdict(list)
        for a in items:
            grp = a.get(cfg["group_key"], "?") or "?"
            by_grp[grp].append(a)
        grp_order = sorted(by_grp.keys(), key=lambda g: -len(by_grp[g]))

        for grp in grp_order:
            lines.append(f"### {cfg['group_label']}: {grp} ({len(by_grp[grp])}건)")
            lines.append("")
            for a in by_grp[grp]:
                d = parse_date(a.get(cfg["date_key"], ""), cfg["date_fmt"])
                dstr = d.strftime("%Y-%m-%d") if d else "?"
                title = a.get(cfg["title_key"], "").strip()
                url = get_url(a, cfg)
                sec = a.get("_sec", "")
                sec_str = f" [2nd: {sec}]" if sec and sec not in ("", "none", "?") else ""
                lines.append(f"- [{dstr}]{sec_str} {title}  \n  <{url}>")
            lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"저장: {out_path}")
    print(f"  총 {total:,}건, {len(by_cat)}개 속성")
    for cat in order:
        print(f"  {cat}: {len(by_cat[cat]):,}건")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in (*SOURCES.keys(), "all"):
        print(f"Usage: python export_titles.py {{{' | '.join(SOURCES.keys())} | all}}")
        sys.exit(1)
    targets = list(SOURCES.keys()) if sys.argv[1] == "all" else [sys.argv[1]]
    for src in targets:
        print(f"\n=== {src.upper()} ===")
        export(src)


if __name__ == "__main__":
    main()
