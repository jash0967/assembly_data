"""TF-IDF 결과를 LLM으로 교차 매핑 + 의미 해석.

영어/한국어 TF-IDF 키워드를 GPT에 넣어서:
1. 공통 소주제 도출
2. 한쪽에만 있는 소주제 식별

Usage:
    python subtopic_mapping.py
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


def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else 'tfidf_subtopics_bilingual.json'
    print(f"Input: {input_file}", flush=True)
    with open(os.path.join(DATA_DIR, input_file), encoding='utf-8') as f:
        data = json.load(f)

    results = {}

    for attr, langs in data.items():
        en_terms = [t[0] for t in langs.get('en', {}).get('terms', [])]
        kr_terms = [t[0] for t in langs.get('kr', {}).get('terms', [])]
        en_n = langs.get('en', {}).get('n', 0)
        kr_n = langs.get('kr', {}).get('n', 0)

        if not en_terms and not kr_terms:
            continue

        prompt = f"""You are an AI policy research assistant.

Below are TF-IDF top keywords extracted from news articles classified under the policy attribute "{attr}".
English keywords come from Guardian + NYT ({en_n} articles, 2016-2026).
Korean keywords come from Naver News ({kr_n} articles, recent ~6 weeks).

English top terms: {', '.join(en_terms)}
Korean top terms: {', '.join(kr_terms)}

Tasks:
1. Identify 4-6 meaningful subtopics that emerge from BOTH languages combined.
2. For each subtopic, list the English and Korean keywords that map to it.
3. Identify subtopics that appear in ONLY ONE language (not the other). These are important cross-cultural findings.
4. Note: ignore noise terms like "있다", "통해", "quot", "strong", person names (journalists), newsletter artifacts.

Respond in JSON:
{{
  "shared_subtopics": [
    {{"name": "...", "en_keywords": [...], "kr_keywords": [...], "description": "..."}}
  ],
  "english_only": [
    {{"name": "...", "keywords": [...], "description": "..."}}
  ],
  "korean_only": [
    {{"name": "...", "keywords": [...], "description": "..."}}
  ]
}}"""

        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "You are a policy research analyst. Be precise and concise."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content)
        results[attr] = result

        short = SHORT.get(attr, attr)
        print(f"\n{'='*60}", flush=True)
        print(f"  {short} (EN:{en_n} | KR:{kr_n})", flush=True)
        print(f"{'='*60}", flush=True)

        print(f"\n  Shared subtopics:", flush=True)
        for s in result.get('shared_subtopics', []):
            print(f"    * {s['name']}", flush=True)
            print(f"      EN: {', '.join(s.get('en_keywords', []))}", flush=True)
            print(f"      KR: {', '.join(s.get('kr_keywords', []))}", flush=True)

        if result.get('english_only'):
            print(f"\n  English ONLY:", flush=True)
            for s in result['english_only']:
                print(f"    * {s['name']}: {', '.join(s.get('keywords', []))}", flush=True)

        if result.get('korean_only'):
            print(f"\n  Korean ONLY:", flush=True)
            for s in result['korean_only']:
                print(f"    * {s['name']}: {', '.join(s.get('keywords', []))}", flush=True)

    out_name = input_file.replace('tfidf_', 'mapped_').replace('.json', '_mapped.json')
    output = os.path.join(DATA_DIR, out_name)
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n\nSaved: {output}", flush=True)


if __name__ == "__main__":
    main()
