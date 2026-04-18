"""EU AI Act (Regulation 2024/1689) 조문 수집 및 Policy Attribute 분류.

1. EUR-Lex에서 AI Act 전문 HTML 다운로드
2. 각 Article 파싱 (번호, 제목, 본문)
3. GPT-4.1-mini로 10개 Policy Attribute 분류

Usage:
    python replicate_carvao/eu_01_collect_ai_act.py
"""
import json
import os
import re
import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT_ARTICLES = os.path.join(DATA_DIR, "eu_ai_act_articles.json")
OUTPUT_CLASSIFIED = os.path.join(DATA_DIR, "eu_ai_act_classified.json")
HTML_CACHE = os.path.join(DATA_DIR, "eu_ai_act_raw.html")

client = OpenAI()

# ─── EUR-Lex에서 AI Act HTML 다운로드 ───

EURLEX_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32024R1689"


def download_html() -> str:
    """EUR-Lex에서 전체 HTML 다운로드 (캐시)."""
    if os.path.exists(HTML_CACHE):
        print(f"  캐시 사용: {HTML_CACHE}")
        with open(HTML_CACHE, encoding="utf-8") as f:
            return f.read()

    print("  EUR-Lex에서 다운로드 중...")
    resp = requests.get(EURLEX_URL, headers={
        "User-Agent": "Mozilla/5.0 (research project)",
        "Accept": "text/html",
    }, timeout=60)
    resp.raise_for_status()
    html = resp.text

    with open(HTML_CACHE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  저장: {len(html):,} bytes")
    return html


# ─── HTML → Article 파싱 ───

def parse_articles(html: str) -> list[dict]:
    """HTML에서 각 Article 추출."""
    soup = BeautifulSoup(html, "html.parser")

    articles = []
    # EUR-Lex HTML에서 Article은 보통 <p> 또는 <div> 안에
    # "Article N" 패턴으로 시작
    # 먼저 전체 텍스트에서 Article 위치 찾기

    # 방법 1: id 기반 (EUR-Lex는 article에 id를 부여)
    # 방법 2: 텍스트 패턴 기반
    all_text = soup.get_text()

    # Article 패턴 찾기
    pattern = re.compile(
        r'Article\s+(\d+)\s*\n\s*(.+?)(?=\nArticle\s+\d+\s*\n|\nCHAPTER\s|\nTITLE\s|\nSECTION\s|\nANNEX|\Z)',
        re.DOTALL
    )

    matches = list(pattern.finditer(all_text))
    if not matches:
        # 대체 패턴 시도
        pattern = re.compile(
            r'Article\s+(\d+)\s*\r?\n\s*([^\n]+)',
            re.MULTILINE
        )
        matches = list(pattern.finditer(all_text))

    print(f"  Article 패턴 매칭: {len(matches)}건")

    if not matches:
        # 직접 텍스트 분할 시도
        print("  패턴 매칭 실패, 수동 분할 시도...")
        return parse_articles_manual(all_text)

    for m in matches:
        num = int(m.group(1))
        body = m.group(2).strip()
        # 첫 줄이 제목
        lines = body.split('\n')
        title = lines[0].strip() if lines else ""
        content = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""

        articles.append({
            "article_num": num,
            "title": title,
            "text": content[:5000],  # GPT 입력 제한
        })

    return articles


def parse_articles_manual(text: str) -> list[dict]:
    """수동 Article 분할."""
    articles = []
    # "Article N" 으로 시작하는 위치 찾기
    positions = [(m.start(), int(m.group(1)))
                 for m in re.finditer(r'\bArticle\s+(\d+)\b', text)]

    for i, (pos, num) in enumerate(positions):
        # 다음 Article까지의 텍스트 추출
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        chunk = text[pos:end].strip()

        # 첫 줄 제거 (Article N 자체)
        lines = chunk.split('\n')
        title = lines[1].strip() if len(lines) > 1 else ""
        content = '\n'.join(lines[2:]).strip() if len(lines) > 2 else ""

        # 중복 Article 번호 제거 (recital에서도 Article 언급)
        # → Article 본문은 보통 제목이 짧고 내용이 법률 조항
        if len(content) < 20:
            continue

        articles.append({
            "article_num": num,
            "title": title,
            "text": content[:5000],
        })

    # 중복 번호 제거 (가장 긴 것 유지)
    by_num = {}
    for a in articles:
        n = a["article_num"]
        if n not in by_num or len(a["text"]) > len(by_num[n]["text"]):
            by_num[n] = a
    return sorted(by_num.values(), key=lambda x: x["article_num"])


# ─── GPT 분류 ───

SYSTEM_PROMPT = """You are an expert AI policy analyst.
Classify this EU AI Act article into the top 3 policy attributes below.

Policy Attributes:
1. Market efficiency and power concentration (antitrust)
2. Safety
3. Responsible and ethical AI
4. National security
5. Industrial policy
6. Public interest
7. Labor
8. Copyright
9. International collaboration
10. Elections

If the article is procedural/administrative (definitions, timelines, penalties, etc.)
and doesn't map to a specific policy area, respond with "Procedural/Administrative" as primary.

Respond ONLY with JSON: {"primary": "...", "secondary": "...", "tertiary": "..."}"""


def classify_articles(articles: list[dict]) -> list[dict]:
    """GPT로 각 Article 분류."""
    print(f"\n=== GPT Classification ({len(articles)} articles) ===")

    if os.path.exists(OUTPUT_CLASSIFIED):
        with open(OUTPUT_CLASSIFIED, encoding="utf-8") as f:
            existing = json.load(f)
        done_nums = {a["article_num"] for a in existing}
        print(f"  기존: {len(existing)}건")
    else:
        existing = []
        done_nums = set()

    for i, art in enumerate(articles, 1):
        if art["article_num"] in done_nums:
            continue

        user_msg = f"Article {art['article_num']}: {art['title']}\n\n{art['text'][:3000]}"

        try:
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            result = json.loads(resp.choices[0].message.content)
            result["article_num"] = art["article_num"]
            result["title"] = art["title"]
            existing.append(result)
            print(f"  [{i}/{len(articles)}] Art.{art['article_num']}: {art['title'][:40]} → {result.get('primary', '?')[:30]}")
        except Exception as e:
            print(f"  [{i}/{len(articles)}] Art.{art['article_num']}: ERROR {e}")
            existing.append({
                "article_num": art["article_num"],
                "title": art["title"],
                "primary": "error",
                "error": str(e),
            })

        time.sleep(0.1)

    with open(OUTPUT_CLASSIFIED, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    return existing


def print_summary(classified: list[dict]):
    """분류 결과 요약."""
    from collections import Counter
    print(f"\n{'='*60}")
    print(f"EU AI Act: {len(classified)} articles classified")
    primary = Counter(a.get("primary", "?") for a in classified)
    for attr, cnt in primary.most_common():
        print(f"  {attr}: {cnt} ({cnt/len(classified)*100:.1f}%)")


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # 1. HTML 다운로드
    print("=== EU AI Act 수집 ===")
    html = download_html()

    # 2. Article 파싱
    print("\n=== Article 파싱 ===")
    articles = parse_articles(html)
    print(f"  파싱 완료: {len(articles)}개 조항")

    if not articles:
        print("ERROR: Article 파싱 실패")
        return

    # Article 번호 범위 출력
    nums = [a["article_num"] for a in articles]
    print(f"  범위: Article {min(nums)} ~ {max(nums)}")

    # 저장
    with open(OUTPUT_ARTICLES, "w", encoding="utf-8") as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)
    print(f"  저장: {OUTPUT_ARTICLES}")

    # 3. GPT 분류
    classified = classify_articles(articles)
    print_summary(classified)


if __name__ == "__main__":
    main()
