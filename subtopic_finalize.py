"""소주제 최종 통합 — 영어/한국어 5 seed 결과를 LLM으로 통합.

Usage:
    python subtopic_finalize.py
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
    with open(os.path.join(DATA_DIR, 'subtopics_discovered.json'), encoding='utf-8') as f:
        data = json.load(f)

    summary = {}
    for attr, langs in data.items():
        summary[attr] = {'en': [], 'kr': []}
        for lang in ['en', 'kr']:
            seeds = langs.get(lang, {})
            for seed, result in seeds.items():
                for s in result.get('subtopics', []):
                    summary[attr][lang].append(s['name'])

    prompt = """아래는 10개 Policy Attribute별로 영어(Guardian+NYT)와 한국어(Naver) 뉴스에서 5번의 반복 실험으로 도출된 소주제 후보 목록입니다.

각 attribute에 대해:
1. 영어와 한국어 소주제를 통합하여 최종 소주제 5~7개를 확정하세요.
2. 소주제 이름은 한국어로 작성하되, 영어 명칭도 병기하세요.
3. 양쪽에서 공통으로 등장하는 소주제와, 한쪽에서만 등장하는 소주제를 구분하세요.
4. 노이즈(보도 관용구, 인명, 매체명)는 제거하세요.
5. 5번 실험 중 3번 이상 등장한 소주제만 포함하세요.

JSON으로 응답:
{
  "attributes": [
    {
      "name": "Policy Attribute 이름",
      "subtopics": [
        {
          "name_kr": "한국어 소주제명",
          "name_en": "English subtopic name",
          "source": "both" | "en_only" | "kr_only",
          "definition": "1문장 정의"
        }
      ]
    }
  ]
}

데이터:
"""

    for attr in data:
        short = SHORT.get(attr, attr)
        en_names = summary[attr]['en']
        kr_names = summary[attr]['kr']
        prompt += f"\n### {short}\nEN subtopics (5 seeds): {' | '.join(en_names)}\n"
        prompt += f"KR subtopics (5 seeds): {' | '.join(kr_names)}\n"

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": "You are an AI policy research analyst. Be precise."},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    result = json.loads(resp.choices[0].message.content)

    output = os.path.join(DATA_DIR, 'subtopics_final.json')
    with open(output, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    for attr_data in result.get('attributes', []):
        print(f"\n{'='*60}", flush=True)
        print(f"  {attr_data['name']}", flush=True)
        print(f"{'='*60}", flush=True)
        for s in attr_data.get('subtopics', []):
            flag = {'both': '[공통]', 'en_only': '[EN]', 'kr_only': '[KR]'}.get(s.get('source', ''), '')
            print(f"  {flag:6s} {s['name_kr']} ({s['name_en']})", flush=True)
            print(f"         {s['definition']}", flush=True)

    print(f"\nSaved: {output}", flush=True)


if __name__ == "__main__":
    main()
