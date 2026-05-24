"""한·미·EU 법안 분류 결과를 정책 속성별 마크다운으로 내보내기.

bill_loaders를 경유하여 분류+메타데이터 조인.

Usage:
    python export_bills.py kr            # 한국 19~22대
    python export_bills.py us            # 미국 118·119대
    python export_bills.py eu            # EU AI Act + 수정안
    python export_bills.py all           # 전부 + 통합

Outputs (모두 data/exports/ 안):
    bills_kr_{19,20,21,22}_by_category.md
    bills_kr_all_by_category.md
    bills_us_{118,119}_by_category.md
    bills_us_all_by_category.md
    bills_eu_{act,amendments}_by_category.md
    bills_eu_all_by_category.md
"""
import os
import sys
from collections import defaultdict

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import _bootstrap  # noqa: F401  -- adds repo root to sys.path

import config
from bill_loaders import load_kr_bills, load_us_bills, load_eu_bills

OUT_DIR = config.EXPORTS_DIR
os.makedirs(OUT_DIR, exist_ok=True)

AGE_LABEL = {
    19: "19대 (2012-2016)", 20: "20대 (2016-2020)",
    21: "21대 (2020-2024)", 22: "22대 (2024-)",
}
CONGRESS_LABEL = {118: "118th (2023-2025)", 119: "119th (2025-)"}
EU_TYPE_LABEL = {"article": "AI Act Articles", "amendment": "EP Amendments"}


def _fmt_extras(c):
    sec = c.get("secondary", "") or ""
    ter = c.get("tertiary", "") or ""
    parts = []
    if sec and sec not in ("", "none", "?"):
        parts.append(f"2nd: {sec}")
    if ter and ter not in ("", "none", "?"):
        parts.append(f"3rd: {ter}")
    return f" [{' | '.join(parts)}]" if parts else ""


def _group_by_primary(bills):
    g = defaultdict(list)
    for b in bills:
        g[b.get("primary", "?")].append(b)
    return g


def _cat_order(by_cat):
    return sorted(by_cat.keys(), key=lambda k: -len(by_cat[k]))


# =============================================================================
# KR
# =============================================================================

def _render_kr_item(b):
    date = b.get("propose_date", "?") or "?"
    proposer = b.get("proposer", "?") or "?"
    return f"- [{date}]{_fmt_extras(b)} **{b.get('title','')}** — {proposer}"


def export_kr():
    print("\n[KR 19~22대]", flush=True)
    bills = load_kr_bills(enrich=True)
    by_age = defaultdict(list)
    for b in bills:
        by_age[b["age"]].append(b)

    # 대수별 마크다운
    for age, items in sorted(by_age.items()):
        items.sort(key=lambda b: b.get("propose_date", ""))
        by_cat = _group_by_primary(items)
        order = _cat_order(by_cat)
        total = len(items)

        lines = [
            f"# 한국 국회 {AGE_LABEL[age]} AI 법안 정책 속성별 리스트",
            "",
            f"- 총 {total}건 (AI 키워드 3회 이상 + GPT core/adjacent 필터 통과)",
            f"- 분류: Carvão 10속성 (통일 영문 v2 프롬프트)",
            "",
            "## 목차",
        ]
        for cat in order:
            lines.append(f"- {cat} — {len(by_cat[cat])}건")
        lines.append("")

        for cat in order:
            lines.append(f"## {cat} ({len(by_cat[cat])}건)")
            lines.append("")
            for b in by_cat[cat]:
                lines.append(_render_kr_item(b))
            lines.append("")

        out = os.path.join(OUT_DIR, f"bills_kr_{age}_by_category.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"  {out} — {total}건, {len(by_cat)}개 속성", flush=True)

    # 통합
    cat_age = defaultdict(lambda: defaultdict(list))
    for b in bills:
        cat_age[b.get("primary", "?")][b["age"]].append(b)
    all_cats = sorted(cat_age.keys(), key=lambda k: -sum(len(v) for v in cat_age[k].values()))
    ages_present = sorted(by_age.keys())
    grand = len(bills)

    lines = [f"# 한국 국회 19~22대 AI 법안 통합 (정책 속성별)", "", f"- 총 {grand}건", ""]
    # 분포표
    lines += ["## 대수별 속성 분포", ""]
    lines.append("| 속성 | " + " | ".join(AGE_LABEL[a] for a in ages_present) + " | 합계 |")
    lines.append("|---|" + "---:|" * (len(ages_present) + 1))
    for cat in all_cats:
        row = [cat]
        tot = 0
        for a in ages_present:
            n = len(cat_age[cat].get(a, []))
            row.append(str(n) if n else "-")
            tot += n
        row.append(str(tot))
        lines.append("| " + " | ".join(row) + " |")
    totals = ["**계**"] + [str(len(by_age[a])) for a in ages_present] + [str(grand)]
    lines.append("| " + " | ".join(totals) + " |")
    lines.append("")

    for cat in all_cats:
        cat_tot = sum(len(v) for v in cat_age[cat].values())
        lines.append(f"## {cat} ({cat_tot}건)")
        lines.append("")
        for a in ages_present:
            items = sorted(cat_age[cat].get(a, []), key=lambda b: b.get("propose_date", ""))
            if not items:
                continue
            lines.append(f"### {AGE_LABEL[a]} — {len(items)}건")
            lines.append("")
            for b in items:
                lines.append(_render_kr_item(b))
            lines.append("")

    out = os.path.join(OUT_DIR, "bills_kr_all_by_category.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  {out} — {grand}건 통합", flush=True)


# =============================================================================
# US
# =============================================================================

def _render_us_item(b):
    date = b.get("introduced_date", "?") or "?"
    sp = b.get("sponsor_name", "?")
    party = b.get("sponsor_party", "")
    state = b.get("sponsor_state", "")
    sp_str = f"{sp}" + (f" ({party}-{state})" if party or state else "")
    return f"- [{date}]{_fmt_extras(b)} `{b.get('id','')}` **{b.get('title','')}** — {sp_str}"


def export_us():
    print("\n[US 118·119대]", flush=True)
    bills = load_us_bills(enrich=True)
    by_cg = defaultdict(list)
    for b in bills:
        by_cg[b["congress"]].append(b)

    for cg, items in sorted(by_cg.items()):
        items.sort(key=lambda b: b.get("introduced_date", ""))
        by_cat = _group_by_primary(items)
        order = _cat_order(by_cat)
        total = len(items)

        lines = [
            f"# US Congress {CONGRESS_LABEL[cg]} AI Bills by Policy Attribute",
            "",
            f"- 총 {total}건 (Brennan Center 선별)",
            f"- 분류: Carvão 10속성 (통일 영문 v2 프롬프트)",
            "",
            "## 목차",
        ]
        for cat in order:
            lines.append(f"- {cat} — {len(by_cat[cat])}건")
        lines.append("")
        for cat in order:
            lines.append(f"## {cat} ({len(by_cat[cat])}건)")
            lines.append("")
            for b in by_cat[cat]:
                lines.append(_render_us_item(b))
            lines.append("")

        out = os.path.join(OUT_DIR, f"bills_us_{cg}_by_category.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"  {out} — {total}건, {len(by_cat)}개 속성", flush=True)

    # 통합
    cat_cg = defaultdict(lambda: defaultdict(list))
    for b in bills:
        cat_cg[b.get("primary", "?")][b["congress"]].append(b)
    all_cats = sorted(cat_cg.keys(), key=lambda k: -sum(len(v) for v in cat_cg[k].values()))
    cgs = sorted(by_cg.keys())
    grand = len(bills)

    lines = [f"# US Congress 118·119 AI Bills 통합 (정책 속성별)", "", f"- 총 {grand}건", ""]
    lines += ["## Congress별 속성 분포", ""]
    lines.append("| 속성 | " + " | ".join(CONGRESS_LABEL[c] for c in cgs) + " | 합계 |")
    lines.append("|---|" + "---:|" * (len(cgs) + 1))
    for cat in all_cats:
        row = [cat]
        tot = 0
        for c in cgs:
            n = len(cat_cg[cat].get(c, []))
            row.append(str(n) if n else "-")
            tot += n
        row.append(str(tot))
        lines.append("| " + " | ".join(row) + " |")
    totals = ["**계**"] + [str(len(by_cg[c])) for c in cgs] + [str(grand)]
    lines.append("| " + " | ".join(totals) + " |")
    lines.append("")

    for cat in all_cats:
        cat_tot = sum(len(v) for v in cat_cg[cat].values())
        lines.append(f"## {cat} ({cat_tot}건)")
        lines.append("")
        for c in cgs:
            items = sorted(cat_cg[cat].get(c, []), key=lambda b: b.get("introduced_date", ""))
            if not items:
                continue
            lines.append(f"### {CONGRESS_LABEL[c]} — {len(items)}건")
            lines.append("")
            for b in items:
                lines.append(_render_us_item(b))
            lines.append("")

    out = os.path.join(OUT_DIR, "bills_us_all_by_category.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  {out} — {grand}건 통합", flush=True)


# =============================================================================
# EU
# =============================================================================

def _render_eu_item(b):
    t = b.get("type", "")
    if t == "article":
        marker = f"Art. {b.get('article_num','')}"
        title = b.get("title", "")
        return f"- {marker}{_fmt_extras(b)} **{title}**"
    else:
        marker = f"Am. {b.get('amendment_num','')}"
        target = b.get("target", "") or b.get("title", "")
        return f"- {marker}{_fmt_extras(b)} target: {target}"


def export_eu():
    print("\n[EU AI Act + Amendments]", flush=True)
    bills = load_eu_bills(enrich=True)
    by_type = defaultdict(list)
    for b in bills:
        by_type[b["type"]].append(b)

    for t in ("article", "amendment"):
        items = by_type.get(t, [])
        if not items:
            continue
        # sort: article_num or amendment_num numerically
        def _k(b):
            v = b.get("article_num" if t == "article" else "amendment_num", "")
            try:
                return (0, int(v))
            except (ValueError, TypeError):
                return (1, str(v))
        items.sort(key=_k)
        by_cat = _group_by_primary(items)
        order = _cat_order(by_cat)
        total = len(items)
        name = "act" if t == "article" else "amendments"

        lines = [
            f"# EU {EU_TYPE_LABEL[t]} by Policy Attribute",
            "",
            f"- 총 {total}건",
            f"- 분류: Carvão 10속성 (통일 영문 v2 프롬프트)",
            "",
            "## 목차",
        ]
        for cat in order:
            lines.append(f"- {cat} — {len(by_cat[cat])}건")
        lines.append("")
        for cat in order:
            lines.append(f"## {cat} ({len(by_cat[cat])}건)")
            lines.append("")
            for b in by_cat[cat]:
                lines.append(_render_eu_item(b))
            lines.append("")

        out = os.path.join(OUT_DIR, f"bills_eu_{name}_by_category.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"  {out} — {total}건, {len(by_cat)}개 속성", flush=True)

    # 통합
    cat_type = defaultdict(lambda: defaultdict(list))
    for b in bills:
        cat_type[b.get("primary", "?")][b["type"]].append(b)
    all_cats = sorted(cat_type.keys(), key=lambda k: -sum(len(v) for v in cat_type[k].values()))
    types_present = [t for t in ("article", "amendment") if by_type.get(t)]
    grand = len(bills)

    lines = [f"# EU AI Act 통합 (조문 + 수정안, 정책 속성별)", "", f"- 총 {grand}건", ""]
    lines += ["## 유형별 속성 분포", ""]
    lines.append("| 속성 | " + " | ".join(EU_TYPE_LABEL[t] for t in types_present) + " | 합계 |")
    lines.append("|---|" + "---:|" * (len(types_present) + 1))
    for cat in all_cats:
        row = [cat]
        tot = 0
        for t in types_present:
            n = len(cat_type[cat].get(t, []))
            row.append(str(n) if n else "-")
            tot += n
        row.append(str(tot))
        lines.append("| " + " | ".join(row) + " |")
    totals = ["**계**"] + [str(len(by_type[t])) for t in types_present] + [str(grand)]
    lines.append("| " + " | ".join(totals) + " |")
    lines.append("")

    for cat in all_cats:
        cat_tot = sum(len(v) for v in cat_type[cat].values())
        lines.append(f"## {cat} ({cat_tot}건)")
        lines.append("")
        for t in types_present:
            items = cat_type[cat].get(t, [])
            if not items:
                continue
            def _k(b):
                v = b.get("article_num" if t == "article" else "amendment_num", "")
                try:
                    return (0, int(v))
                except (ValueError, TypeError):
                    return (1, str(v))
            items = sorted(items, key=_k)
            lines.append(f"### {EU_TYPE_LABEL[t]} — {len(items)}건")
            lines.append("")
            for b in items:
                lines.append(_render_eu_item(b))
            lines.append("")

    out = os.path.join(OUT_DIR, "bills_eu_all_by_category.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  {out} — {grand}건 통합", flush=True)


# =============================================================================
# Main
# =============================================================================

HANDLERS = {"kr": export_kr, "us": export_us, "eu": export_eu}


def main():
    if len(sys.argv) < 2:
        print("Usage: python export_bills.py {kr | us | eu | all}")
        sys.exit(1)
    target = sys.argv[1].lower()
    if target == "all":
        for h in HANDLERS.values():
            h()
    elif target in HANDLERS:
        HANDLERS[target]()
    else:
        print(f"Unknown target: {target}. Use kr | us | eu | all")
        sys.exit(1)


if __name__ == "__main__":
    main()
