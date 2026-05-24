"""Treemap 시각화용 데이터 생성.

subtopics_final.json + 분류 결과를 결합하여 treemap JSON 생성.
소주제별 기사 수는 seed 실험의 article assignment 비율로 추정.

Usage:
    python build_treemap_data.py
"""
import json
import os
import sys
from collections import Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

EN_ATTRS = [
    'Safety', 'Responsible and ethical AI',
    'Market efficiency and power concentration (antitrust)',
    'Industrial policy', 'National security', 'Public interest',
    'Labor', 'Copyright', 'International collaboration', 'Elections',
]
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


def load_classified(path):
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, dict):
        return [{'primary': v.get('primary', '?')} for v in data.values()]
    return data


def normalize_case(primary):
    canonical = {a.lower(): a for a in EN_ATTRS}
    p = primary.strip()
    if p and p[0].isdigit():
        p = p.split('.', 1)[-1].strip()
    return canonical.get(p.lower(), KR_TO_EN.get(p, p))


def count_by_attr(classified_list):
    c = Counter()
    for a in classified_list:
        p = normalize_case(a.get('primary', ''))
        if p in EN_ATTRS:
            c[p] += 1
    return c


def main():
    # Load all classified data
    guardian_cls = load_classified(os.path.join(DATA_DIR, 'processed/articles_classified_guardian.json'))
    nyt_cls = load_classified(os.path.join(DATA_DIR, 'processed/articles_classified_nyt.json'))
    naver_cls = load_classified(os.path.join(DATA_DIR, 'processed/articles_classified_naver.json'))

    us118_cls = load_classified(os.path.join('replicate_carvao', 'data', 'gpt_classification.json'))
    us119_cls = load_classified(os.path.join('replicate_carvao', 'data', 'us119_gpt.json'))
    kr_cls = load_classified(os.path.join('replicate_carvao', 'data', 'kr_gpt_classification.json'))

    with open(os.path.join(DATA_DIR, 'eu_ai_act_classified.json'), encoding='utf-8') as f:
        eu_act = [a for a in json.load(f) if a.get('primary') != 'Procedural/Administrative']
    with open(os.path.join(DATA_DIR, 'eu_amendments_classified.json'), encoding='utf-8') as f:
        eu_amd = [a for a in json.load(f) if a.get('primary') != 'Procedural/Administrative']

    # Count per attribute
    counts = {
        'guardian': count_by_attr(guardian_cls),
        'nyt': count_by_attr(nyt_cls),
        'naver': count_by_attr(naver_cls),
        'us118': count_by_attr(us118_cls),
        'us119': count_by_attr(us119_cls),
        'kr22': count_by_attr(kr_cls),
        'eu_act': count_by_attr(eu_act),
        'eu_amd': count_by_attr(eu_amd),
    }

    # Totals for percentages
    totals = {k: sum(v.values()) for k, v in counts.items()}

    # Load subtopics
    with open(os.path.join(DATA_DIR, 'subtopics_final.json'), encoding='utf-8') as f:
        subtopics_data = json.load(f)

    # Load discovered subtopics to estimate per-subtopic proportions
    with open(os.path.join(DATA_DIR, 'subtopics_discovered.json'), encoding='utf-8') as f:
        discovered = json.load(f)

    # Estimate subtopic proportions from seed article assignments
    def estimate_proportions(attr_key, lang):
        seeds = discovered.get(attr_key, {}).get(lang, {})
        topic_article_counts = Counter()
        total_assigned = 0
        for seed, result in seeds.items():
            for s in result.get('subtopics', []):
                n = len(s.get('articles', []))
                topic_article_counts[s['name']] += n
                total_assigned += n
        if total_assigned == 0:
            return {}
        return {name: cnt / total_assigned for name, cnt in topic_article_counts.items()}

    # Build treemap data
    treemap = {"name": "AI Policy", "children": []}

    for attr_data in subtopics_data.get('attributes', []):
        attr_name = attr_data['name']
        # Find matching full attr name
        full_attr = None
        for a in EN_ATTRS:
            if SHORT.get(a, a) == attr_name or a == attr_name:
                full_attr = a
                break
        if not full_attr:
            full_attr = attr_name

        # News counts for this attribute
        news_total = (counts['guardian'].get(full_attr, 0) +
                      counts['nyt'].get(full_attr, 0) +
                      counts['naver'].get(full_attr, 0))

        # Legislation counts
        leg_total = (counts['us118'].get(full_attr, 0) +
                     counts['us119'].get(full_attr, 0) +
                     counts['kr22'].get(full_attr, 0))

        # EU counts
        eu_total = counts['eu_act'].get(full_attr, 0) + counts['eu_amd'].get(full_attr, 0)

        # News percentages per source
        news_pct = {
            'guardian': round(counts['guardian'].get(full_attr, 0) / totals['guardian'] * 100, 1) if totals['guardian'] else 0,
            'nyt': round(counts['nyt'].get(full_attr, 0) / totals['nyt'] * 100, 1) if totals['nyt'] else 0,
            'naver': round(counts['naver'].get(full_attr, 0) / totals['naver'] * 100, 1) if totals['naver'] else 0,
        }
        leg_pct = {
            'us118': round(counts['us118'].get(full_attr, 0) / totals['us118'] * 100, 1) if totals['us118'] else 0,
            'us119': round(counts['us119'].get(full_attr, 0) / totals['us119'] * 100, 1) if totals['us119'] else 0,
            'kr22': round(counts['kr22'].get(full_attr, 0) / totals['kr22'] * 100, 1) if totals['kr22'] else 0,
        }

        # Gap: average news% - average legislation%
        avg_news = (news_pct['guardian'] + news_pct['nyt'] + news_pct['naver']) / 3
        avg_leg = (leg_pct['us118'] + leg_pct['us119'] + leg_pct['kr22']) / 3
        gap = round(avg_news - avg_leg, 1)

        # Subtopic children
        n_subtopics = len(attr_data.get('subtopics', []))
        children = []
        for sub in attr_data.get('subtopics', []):
            sub_news = max(1, news_total // n_subtopics)  # equal split approximation
            children.append({
                "name": sub['name_kr'],
                "name_en": sub['name_en'],
                "source": sub.get('source', 'both'),
                "definition": sub['definition'],
                "value": sub_news,
                "attribute": attr_name,
            })

        attr_node = {
            "name": attr_name,
            "news_total": news_total,
            "leg_total": leg_total,
            "eu_total": eu_total,
            "news_pct": news_pct,
            "leg_pct": leg_pct,
            "gap": gap,
            "children": children,
        }
        treemap["children"].append(attr_node)

    # Year-over-year data for temporal layer
    # Guardian yearly
    with open(os.path.join(DATA_DIR, 'guardian_articles_raw.json'), encoding='utf-8') as f:
        guardian_raw = json.load(f)
    guardian_yearly = Counter(a['pub_date'][:4] for a in guardian_raw)

    with open(os.path.join(DATA_DIR, 'nyt_articles_raw.json'), encoding='utf-8') as f:
        nyt_raw = json.load(f)
    nyt_yearly = Counter(a['pub_date'][:4] for a in nyt_raw)

    treemap["meta"] = {
        "totals": totals,
        "guardian_yearly": dict(sorted(guardian_yearly.items())),
        "nyt_yearly": dict(sorted(nyt_yearly.items())),
        "generated": "2026-04-09",
    }

    output = os.path.join(DATA_DIR, 'treemap_data.json')
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(treemap, f, indent=2, ensure_ascii=False)

    print(f"Saved: {output}", flush=True)
    print(f"Attributes: {len(treemap['children'])}", flush=True)
    total_subtopics = sum(len(a['children']) for a in treemap['children'])
    print(f"Subtopics: {total_subtopics}", flush=True)

    # Print gap summary
    print(f"\nGap (News% - Legislation%):", flush=True)
    for a in sorted(treemap['children'], key=lambda x: -abs(x['gap'])):
        direction = "뉴스>입법" if a['gap'] > 0 else "입법>뉴스"
        print(f"  {a['name']:25s} gap={a['gap']:+.1f}  ({direction})", flush=True)


if __name__ == "__main__":
    main()
