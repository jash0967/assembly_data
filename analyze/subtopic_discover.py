"""LLM 직접 소주제 도출 — 기사 샘플 기반.

각 attribute × language × 5 seeds로 소주제를 귀납적으로 추출.
seed별 결과를 비교해 robustness 검증.

Usage:
    python subtopic_discover.py
"""
import json
import os
import random
import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

import _bootstrap  # noqa: F401
import config

OUTPUT = os.path.join(config.ANALYSIS_DIR, "subtopics_discovered.json")

client = OpenAI()

EN_ATTRS = [
    'Safety', 'Responsible and ethical AI',
    'Market efficiency and power concentration (antitrust)',
    'Industrial policy', 'National security', 'Public interest',
    'Labor', 'Copyright', 'International collaboration', 'Elections',
]
EN_CANONICAL = {a.lower(): a for a in EN_ATTRS}
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

SEEDS = [42, 123, 456, 789, 2026]

SYSTEM_EN = """You are an AI policy research analyst.
You will receive a list of news article titles and summaries, all classified under one policy attribute.
Your task: identify 5-7 recurring SUBTOPICS from these articles.

Rules:
- A subtopic is a specific policy issue (e.g. "autonomous vehicle safety"), NOT a person, organization, or reporting style.
- Do NOT use person names, media outlets, or generic phrases as subtopics.
- Each subtopic name should be 3-6 words.
- Provide a 1-sentence definition for each.
- List which article numbers belong to each subtopic.

Respond ONLY with JSON:
{"subtopics": [{"name": "...", "definition": "...", "articles": [1, 5, 23]}]}"""

SYSTEM_KR = """You are an AI policy research analyst.
다음은 하나의 정책 속성으로 분류된 뉴스 기사 제목과 요약 목록입니다.
이 기사들에서 반복적으로 등장하는 소주제를 5~7개 도출하세요.

규칙:
- 소주제는 구체적인 정책 이슈여야 합니다 (예: "자율주행 안전"). 인명, 기관명, 보도 관용구는 소주제가 아닙니다.
- 소주제 이름은 3~6 단어.
- 각 소주제의 정의를 1문장으로.
- 해당 기사 번호를 나열하세요.

JSON만 응답:
{"subtopics": [{"name": "...", "definition": "...", "articles": [1, 5, 23]}]}"""


def load_data():
    """Load and group articles by attribute and language."""
    def load_paired(raw_path, cls_path, id_field, text_fn):
        with open(raw_path, encoding='utf-8') as f:
            raw = json.load(f)
        with open(cls_path, encoding='utf-8') as f:
            cls = json.load(f)
        raw_by_id = {a[id_field]: a for a in raw}
        paired = []
        for c in cls:
            r = raw_by_id.get(c['article_id'])
            if r:
                paired.append({'text': text_fn(r), 'primary': c.get('primary', 'none')})
        return paired

    guardian = load_paired(
        os.path.join(config.NEWS_DIR, 'guardian_articles_raw.json'),
        os.path.join(config.ANALYSIS_DIR, 'articles_classified_guardian.json'),
        'url', lambda r: f"Title: {r.get('title','')}\nSummary: {r.get('trail_text','')}")
    nyt = load_paired(
        os.path.join(config.NEWS_DIR, 'nyt_articles_raw.json'),
        os.path.join(config.ANALYSIS_DIR, 'articles_classified_nyt.json'),
        'url', lambda r: f"Title: {r.get('title','')}\nSummary: {r.get('abstract','')}")
    all_en = guardian + nyt

    from collections import defaultdict
    en_groups = defaultdict(list)
    for a in all_en:
        p = a['primary'].strip()
        if p and p[0].isdigit():
            p = p.split('.', 1)[-1].strip()
        canon = EN_CANONICAL.get(p.lower())
        if canon:
            en_groups[canon].append(a['text'])

    # Korean domestic news (Naver) was previously paired here; the active KR
    # pipeline now lives in data/news/news.duckdb and is classified separately,
    # so subtopic_discover currently runs against English sources only.
    kr_groups = defaultdict(list)

    return en_groups, kr_groups


def discover_subtopics(articles: list[str], system_prompt: str, seed: int) -> dict:
    """100건 샘플에서 소주제 도출."""
    rng = random.Random(seed)
    sample = rng.sample(articles, min(100, len(articles)))

    article_list = "\n\n".join(f"[{i+1}] {a}" for i, a in enumerate(sample))
    user_msg = f"Articles ({len(sample)} total):\n\n{article_list}"

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg[:60000]},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


def main():
    print("=== Loading data ===", flush=True)
    en_groups, kr_groups = load_data()

    results = {}
    call_count = 0

    for attr in EN_ATTRS:
        short = SHORT.get(attr, attr)
        results[attr] = {"en": {}, "kr": {}}

        # English
        en_articles = en_groups.get(attr, [])
        if len(en_articles) >= 30:
            print(f"\n{'='*60}", flush=True)
            print(f"  {short} EN ({len(en_articles)} articles)", flush=True)

            for seed in SEEDS:
                print(f"    seed={seed}...", end=" ", flush=True)
                try:
                    result = discover_subtopics(en_articles, SYSTEM_EN, seed)
                    results[attr]["en"][str(seed)] = result
                    names = [s['name'] for s in result.get('subtopics', [])]
                    print(f"{len(names)} subtopics: {', '.join(names[:3])}...", flush=True)
                    call_count += 1
                except Exception as e:
                    print(f"ERROR: {e}", flush=True)
                    results[attr]["en"][str(seed)] = {"error": str(e)}
                time.sleep(0.5)

        # Korean
        kr_articles = kr_groups.get(attr, [])
        if len(kr_articles) >= 30:
            print(f"  {short} KR ({len(kr_articles)} articles)", flush=True)

            for seed in SEEDS:
                print(f"    seed={seed}...", end=" ", flush=True)
                try:
                    result = discover_subtopics(kr_articles, SYSTEM_KR, seed)
                    results[attr]["kr"][str(seed)] = result
                    names = [s['name'] for s in result.get('subtopics', [])]
                    print(f"{len(names)} subtopics: {', '.join(names[:3])}...", flush=True)
                    call_count += 1
                except Exception as e:
                    print(f"ERROR: {e}", flush=True)
                    results[attr]["kr"][str(seed)] = {"error": str(e)}
                time.sleep(0.5)

    with open(OUTPUT, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}", flush=True)
    print(f"Done. {call_count} API calls.", flush=True)
    print(f"Saved: {OUTPUT}", flush=True)


if __name__ == "__main__":
    main()
