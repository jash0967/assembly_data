"""한국 전용 Treemap 데이터 생성 — Naver 뉴스 + 한국 법안.

Usage:
    python build_treemap_kr.py
"""
import json
import os
import sys
from collections import Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

ATTR_KR = {
    'Safety': 'AI 안전',
    'Responsible and ethical AI': '책임/윤리 AI',
    'Market efficiency and power concentration (antitrust)': '시장경쟁/독과점',
    'Industrial policy': '산업정책',
    'National security': '국가안보',
    'Public interest': '공익/소비자보호',
    'Labor': '노동/고용',
    'Copyright': '저작권/지식재산',
    'International collaboration': '국제협력',
    'Elections': '선거/민주주의',
}
EN_ATTRS = list(ATTR_KR.keys())
KR_TO_EN = {
    'AI안전': 'Safety', '책임/윤리AI': 'Responsible and ethical AI',
    '시장경쟁/독과점': 'Market efficiency and power concentration (antitrust)',
    '산업정책': 'Industrial policy', '국가안보': 'National security',
    '공익/소비자보호': 'Public interest', '노동/고용': 'Labor',
    '저작권/지식재산': 'Copyright', '국제협력': 'International collaboration',
    '선거/민주주의': 'Elections',
}
SHORT = {
    'Safety': 'Safety', 'Responsible and ethical AI': 'Responsible AI',
    'Market efficiency and power concentration (antitrust)': 'Market/Antitrust',
    'Industrial policy': 'Industrial policy', 'National security': 'National security',
    'Public interest': 'Public interest', 'Labor': 'Labor', 'Copyright': 'Copyright',
    'International collaboration': 'Intl collaboration', 'Elections': 'Elections',
}


def normalize_case(primary):
    canonical = {a.lower(): a for a in EN_ATTRS}
    p = primary.strip()
    if p and p[0].isdigit():
        p = p.split('.', 1)[-1].strip()
    return canonical.get(p.lower(), KR_TO_EN.get(p, p))


def count_by_attr(cls_list):
    c = Counter()
    for a in cls_list:
        p = normalize_case(a.get('primary', ''))
        if p in EN_ATTRS:
            c[p] += 1
    return c


def main():
    # Naver
    with open(os.path.join(DATA_DIR, 'processed/articles_classified_naver.json'), encoding='utf-8') as f:
        naver_cls = json.load(f)
    naver_counts = count_by_attr(naver_cls)
    naver_total = sum(naver_counts.values())

    # Korean bills
    with open(os.path.join('replicate_carvao', 'data', 'kr_gpt_classification.json'), encoding='utf-8') as f:
        kr_data = json.load(f)
    kr_cls = [{'primary': v.get('primary', '?')} for v in kr_data.values()]
    kr_counts = count_by_attr(kr_cls)
    kr_total = sum(kr_counts.values())

    # Subtopics
    with open(os.path.join(DATA_DIR, 'subtopics_final.json'), encoding='utf-8') as f:
        subtopics_data = json.load(f)

    treemap = {"name": "AI 정책 공론-입법 분석", "children": []}

    for attr_data in subtopics_data.get('attributes', []):
        attr_short = attr_data['name']
        full_attr = None
        for a in EN_ATTRS:
            if SHORT.get(a, a) == attr_short or a == attr_short:
                full_attr = a
                break
        if not full_attr:
            full_attr = attr_short

        attr_kr = ATTR_KR.get(full_attr, attr_short)
        news_count = naver_counts.get(full_attr, 0)
        leg_count = kr_counts.get(full_attr, 0)
        news_pct = round(news_count / naver_total * 100, 1) if naver_total else 0
        leg_pct = round(leg_count / kr_total * 100, 1) if kr_total else 0
        gap = round(news_pct - leg_pct, 1)

        n_sub = len(attr_data.get('subtopics', []))
        children = []
        for sub in attr_data.get('subtopics', []):
            # Clean Korean name (remove English in parentheses)
            name_kr = sub['name_kr']
            if '(' in name_kr:
                name_kr = name_kr[:name_kr.index('(')].strip()

            sub_value = max(1, news_count // n_sub) if n_sub else 1
            children.append({
                "name": name_kr,
                "name_en": sub['name_en'],
                "source": sub.get('source', 'both'),
                "definition": sub['definition'],
                "value": sub_value,
                "attribute": attr_kr,
            })

        treemap["children"].append({
            "name": attr_kr,
            "name_en": full_attr,
            "news_count": news_count,
            "leg_count": leg_count,
            "news_pct": news_pct,
            "leg_pct": leg_pct,
            "gap": gap,
            "children": children,
        })

    treemap["meta"] = {
        "naver_total": naver_total,
        "kr_bills_total": kr_total,
        "generated": "2026-04-09",
    }

    output = os.path.join(DATA_DIR, 'treemap_kr.json')
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(treemap, f, indent=2, ensure_ascii=False)

    print(f"Saved: {output}", flush=True)
    for a in sorted(treemap['children'], key=lambda x: -abs(x['gap'])):
        print(f"  {a['name']:15s} 뉴스={a['news_pct']}% 법안={a['leg_pct']}% 갭={a['gap']:+.1f}", flush=True)


if __name__ == "__main__":
    main()
