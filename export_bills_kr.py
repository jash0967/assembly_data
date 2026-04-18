"""한국 국회 AI 법안 (19~22대) 정책 속성별 리스트.

각 대수별로 별도 마크다운 파일 + 통합 1개.
"""
import json
import os
import sys
from collections import defaultdict, Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

AGES = [19, 20, 21, 22]
AGE_LABEL = {19: "19대 (2012-2016)", 20: "20대 (2016-2020)", 21: "21대 (2020-2024)", 22: "22대 (2024-)"}


def load_raw_meta(age: int, wanted_ids: set) -> dict:
    """raw 법안 파일에서 wanted_ids만 찾아서 {proposer, propose_date, committee} 매핑.

    bill_id는 PRC_XXX 형식이고 파일명도 PRC_XXX.json이므로 파일명 매칭이 더 빠름.
    """
    txt_dir = os.path.join(DATA_DIR, f"bill_txt_{age}")
    meta = {}
    if not os.path.isdir(txt_dir):
        return meta
    for bill_id in wanted_ids:
        fpath = os.path.join(txt_dir, f"{bill_id}.json")
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                b = json.load(f)
            meta[bill_id] = {
                "proposer": b.get("proposer", ""),
                "propose_date": b.get("propose_date", ""),
                "committee": b.get("committee", ""),
            }
        except Exception:
            continue
    return meta


def export_age(age: int) -> dict:
    cls_path = os.path.join(DATA_DIR, f"bills_classified_kr_{age}.json")
    if not os.path.exists(cls_path):
        print(f"  WARN: {cls_path} not found")
        return {}

    with open(cls_path, encoding="utf-8") as f:
        classified = json.load(f)
    wanted_ids = {c.get("id", "") for c in classified if c.get("id")}
    meta = load_raw_meta(age, wanted_ids)

    by_cat = defaultdict(list)
    for c in classified:
        attr = c.get("primary", "?")
        bill_id = c.get("id", "")
        m = meta.get(bill_id, {})
        c["_proposer"] = m.get("proposer", "")
        c["_propose_date"] = m.get("propose_date", "")
        c["_committee"] = m.get("committee", "")
        by_cat[attr].append(c)

    for cat in by_cat:
        by_cat[cat].sort(key=lambda c: c.get("_propose_date", ""))

    return by_cat


def write_age_markdown(age: int, by_cat: dict, outdir: str):
    out_path = os.path.join(outdir, f"bills_kr_{age}_by_category.md")
    total = sum(len(v) for v in by_cat.values())
    order = sorted(by_cat.keys(), key=lambda k: -len(by_cat[k]))

    lines = []
    lines.append(f"# 한국 국회 {AGE_LABEL[age]} AI 법안 정책 속성별 리스트")
    lines.append("")
    lines.append(f"- 총 {total}건 (AI 키워드 3회 이상 + 발의자·제목 dedup)")
    lines.append(f"- 분류: Carvão 10속성 (통일 영문 v2 프롬프트)")
    lines.append("")
    lines.append("## 목차")
    for cat in order:
        lines.append(f"- {cat} — {len(by_cat[cat])}건")
    lines.append("")

    for cat in order:
        items = by_cat[cat]
        lines.append(f"## {cat} ({len(items)}건)")
        lines.append("")
        for c in items:
            date = c.get("_propose_date", "?")
            proposer = c.get("_proposer", "?")
            title = c.get("title", "")
            sec = c.get("secondary", "")
            ter = c.get("tertiary", "")
            extras = []
            if sec and sec not in ("", "none", "?"):
                extras.append(f"2nd: {sec}")
            if ter and ter not in ("", "none", "?"):
                extras.append(f"3rd: {ter}")
            extra_str = f" [{' | '.join(extras)}]" if extras else ""
            lines.append(f"- [{date}]{extra_str} **{title}** — {proposer}")
        lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  저장: {out_path} — {total}건, {len(by_cat)}개 속성")


def write_combined(all_data: dict, outdir: str):
    out_path = os.path.join(outdir, "bills_kr_all_by_category.md")
    grand_total = sum(sum(len(v) for v in ad.values()) for ad in all_data.values())

    # cat → age → items
    cat_age = defaultdict(lambda: defaultdict(list))
    for age, by_cat in all_data.items():
        for cat, items in by_cat.items():
            cat_age[cat][age] = items

    # 카테고리 순서: 전체 합계 기준
    order = sorted(cat_age.keys(), key=lambda k: -sum(len(v) for v in cat_age[k].values()))

    lines = []
    lines.append(f"# 한국 국회 19~22대 AI 법안 통합 (정책 속성별)")
    lines.append("")
    lines.append(f"- 총 {grand_total}건")
    lines.append("")

    # 요약 테이블
    lines.append("## 대수별 속성 분포")
    lines.append("")
    header = "| 속성 | " + " | ".join(f"{AGE_LABEL[a]}" for a in AGES) + " | 합계 |"
    sep = "|---|" + "---:|" * (len(AGES) + 1)
    lines.append(header)
    lines.append(sep)
    for cat in order:
        row = [cat]
        tot = 0
        for a in AGES:
            n = len(cat_age[cat].get(a, []))
            row.append(str(n) if n else "-")
            tot += n
        row.append(str(tot))
        lines.append("| " + " | ".join(row) + " |")
    totals_row = ["**계**"] + [str(sum(len(v) for v in all_data.get(a, {}).values())) for a in AGES] + [str(grand_total)]
    lines.append("| " + " | ".join(totals_row) + " |")
    lines.append("")

    # 각 속성별 법안 나열
    for cat in order:
        cat_total = sum(len(v) for v in cat_age[cat].values())
        lines.append(f"## {cat} ({cat_total}건)")
        lines.append("")
        for age in AGES:
            items = cat_age[cat].get(age, [])
            if not items:
                continue
            lines.append(f"### {AGE_LABEL[age]} — {len(items)}건")
            lines.append("")
            for c in items:
                date = c.get("_propose_date", "?")
                proposer = c.get("_proposer", "?")
                title = c.get("title", "")
                sec = c.get("secondary", "")
                sec_str = f" [2nd: {sec}]" if sec and sec not in ("", "none", "?") else ""
                lines.append(f"- [{date}]{sec_str} **{title}** — {proposer}")
            lines.append("")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  통합 저장: {out_path} — {grand_total}건")


def main():
    outdir = DATA_DIR
    all_data = {}
    for age in AGES:
        print(f"\n[{age}대]")
        by_cat = export_age(age)
        if by_cat:
            write_age_markdown(age, by_cat, outdir)
            all_data[age] = by_cat

    if all_data:
        write_combined(all_data, outdir)


if __name__ == "__main__":
    main()
