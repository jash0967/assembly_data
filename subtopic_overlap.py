"""소주제 반복 안정성 측정 — 5 seed 간 overlap rate 계산.

Usage:
    python subtopic_overlap.py
"""
import json
import os
import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
client = OpenAI()

SHORT = {
    'Safety': 'Safety', 'Responsible and ethical AI': 'Responsible AI',
    'Market efficiency and power concentration (antitrust)': 'Market/Antitrust',
    'Industrial policy': 'Industrial policy', 'National security': 'National security',
    'Public interest': 'Public interest', 'Labor': 'Labor', 'Copyright': 'Copyright',
    'International collaboration': 'Intl collaboration', 'Elections': 'Elections',
}
ATTR_KR = {
    'Safety': 'AI 안전', 'Responsible and ethical AI': '책임/윤리 AI',
    'Market efficiency and power concentration (antitrust)': '시장경쟁/독과점',
    'Industrial policy': '산업정책', 'National security': '국가안보',
    'Public interest': '공익/소비자보호', 'Labor': '노동/고용',
    'Copyright': '저작권/지식재산', 'International collaboration': '국제협력',
    'Elections': '선거/민주주의',
}


def main():
    with open(os.path.join(DATA_DIR, 'subtopics_discovered.json'), encoding='utf-8') as f:
        data = json.load(f)
    with open(os.path.join(DATA_DIR, 'subtopics_final.json'), encoding='utf-8') as f:
        final = json.load(f)

    results = {}

    for attr_final in final['attributes']:
        attr_name = attr_final['name']
        full_attr = None
        for key in data:
            if SHORT.get(key, key) == attr_name or key == attr_name:
                full_attr = key
                break
        if not full_attr:
            continue

        kr_name = ATTR_KR.get(full_attr, attr_name)
        final_subtopics = [s['name_kr'].split('(')[0].strip() for s in attr_final['subtopics']]
        final_en = [s['name_en'] for s in attr_final['subtopics']]

        for lang in ['en', 'kr']:
            seeds = data.get(full_attr, {}).get(lang, {})
            if not seeds:
                continue

            all_raw = []
            for seed, result in seeds.items():
                names = [s['name'] for s in result.get('subtopics', [])]
                all_raw.append(names)

            ref_list = final_en if lang == 'en' else final_subtopics

            prompt = f"""I have 7 final subtopics and 5 sets of raw subtopics from repeated experiments.
For each final subtopic, count how many of the 5 seeds contain a semantically equivalent raw subtopic.
Two subtopics are "equivalent" if they refer to the same policy issue, even if worded differently.

Final subtopics:
{chr(10).join(f'{i+1}. {s}' for i, s in enumerate(ref_list))}

Seed results:
{chr(10).join(f'Seed {i+1}: {", ".join(names)}' for i, names in enumerate(all_raw))}

Respond with JSON: {{"matches": [{{"subtopic": "...", "count": N, "matched_seeds": [1,2,3]}}]}}"""

            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": "Count semantic matches precisely."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)

            key = f"{kr_name} ({lang.upper()})"
            results[key] = result.get('matches', [])

            print(f"\n{kr_name} ({lang.upper()})", flush=True)
            for m in result.get('matches', []):
                rate = m['count'] / 5 * 100
                bar = '#' * m['count'] + '.' * (5 - m['count'])
                print(f"  [{bar}] {m['count']}/5 ({rate:3.0f}%) {m['subtopic']}", flush=True)

    output = os.path.join(DATA_DIR, 'subtopics_overlap_rate.json')
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved: {output}", flush=True)

    # Summary stats
    all_rates = []
    for matches in results.values():
        for m in matches:
            all_rates.append(m['count'] / 5 * 100)
    if all_rates:
        print(f"\nOverall: avg={sum(all_rates)/len(all_rates):.1f}%, "
              f"5/5={sum(1 for r in all_rates if r==100)}/{len(all_rates)}, "
              f">=3/5={sum(1 for r in all_rates if r>=60)}/{len(all_rates)}", flush=True)


if __name__ == "__main__":
    main()
