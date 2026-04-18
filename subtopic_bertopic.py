"""BERTopic 기반 소주제 추출 — 뉴스 데이터 3개 소스.

GPT 기반 소주제 추출의 재현성 문제를 보완하기 위해
BERTopic(SBERT + UMAP + HDBSCAN + c-TF-IDF)으로 결정론적 토픽 추출.
GPT는 토픽 라벨링에만 사용.

Usage:
    python subtopic_bertopic.py
"""
import json
import os
import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
from sentence_transformers import SentenceTransformer
from bertopic import BERTopic
from umap import UMAP
from hdbscan import HDBSCAN
from sklearn.feature_extraction.text import CountVectorizer
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT = os.path.join(DATA_DIR, "subtopics_bertopic.json")

client = OpenAI()

# 10개 정책 속성
ATTRS_EN = [
    'Safety', 'Responsible and ethical AI', 'Industrial policy',
    'Public interest', 'National security', 'Elections',
    'Market efficiency and power concentration (antitrust)',
    'Labor', 'Copyright', 'International collaboration',
]
ATTRS_KR = [
    'AI안전', '책임/윤리AI', '산업정책', '공익/소비자보호', '국가안보',
    '선거/민주주의', '시장경쟁/독과점', '노동/고용', '저작권/지식재산', '국제협력',
]
EN_TO_KR = dict(zip(ATTRS_EN, ATTRS_KR))
KR_TO_EN = dict(zip(ATTRS_KR, ATTRS_EN))

# 뉴스 분류 결과에서 한영 통합
NORMALIZE = {
    'Market efficiency and power concentration (antitrust)': '시장경쟁/독과점',
    'Safety': 'AI안전', 'Responsible and ethical AI': '책임/윤리AI',
    'National security': '국가안보', 'Industrial policy': '산업정책',
    'Public interest': '공익/소비자보호', 'Labor': '노동/고용',
    'Copyright': '저작권/지식재산', 'International collaboration': '국제협력',
    'Elections': '선거/민주주의',
}


def load_news():
    """3개 소스 뉴스 로드 + 정책 속성 매핑."""
    articles = []

    import html as html_mod

    def clean_text(text):
        """HTML 엔티티 제거, 태그 제거, 정규화."""
        import re
        text = html_mod.unescape(text)  # &quot; → "
        text = re.sub(r'<[^>]+>', '', text)  # HTML 태그
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    print("Loading raw texts...")

    # Guardian: 원본 키는 id(경로), title, trail_text
    with open(os.path.join(DATA_DIR, "guardian_articles_raw.json"), encoding="utf-8") as f:
        guardian_raw = json.load(f)
    guardian_map = {}
    for a in guardian_raw:
        title = a.get("title", "") or a.get("webTitle", "")
        trail = a.get("trail_text", "") or (a.get("fields", {}) or {}).get("trailText", "") or ""
        text = clean_text(f"{title}. {trail}")
        # 경로 ID와 전체 URL 모두 매핑
        aid_short = a.get("id", "")
        url = a.get("url", "")
        if aid_short:
            guardian_map[aid_short] = text
            guardian_map[f"https://www.theguardian.com/{aid_short}"] = text
        if url:
            guardian_map[url] = text

    # NYT: 원본 키는 url, title, abstract
    with open(os.path.join(DATA_DIR, "nyt_articles_raw.json"), encoding="utf-8") as f:
        nyt_raw = json.load(f)
    nyt_map = {}
    for a in nyt_raw:
        title = a.get("title", "")
        if not title:
            headline = a.get("headline", {})
            title = headline.get("main", "") if isinstance(headline, dict) else str(headline)
        abstract = a.get("abstract", "") or a.get("lead_paragraph", "") or ""
        text = clean_text(f"{title}. {abstract}")
        for key in ["url", "web_url", "_id", "uri"]:
            aid = a.get(key, "")
            if aid:
                nyt_map[aid] = text

    # Naver
    with open(os.path.join(DATA_DIR, "naver_articles_raw.json"), encoding="utf-8") as f:
        naver_raw = json.load(f)
    naver_map = {}
    for a in naver_raw:
        for key in ["link", "originallink"]:
            aid = a.get(key, "")
            if aid:
                title = a.get("title", "")
                desc = a.get("description", "")
                naver_map[aid] = clean_text(f"{title}. {desc}")

    print(f"  Guardian texts: {len(guardian_map)}, NYT: {len(nyt_map)}, Naver: {len(naver_map)}")

    # 분류 결과 + 원본 텍스트 결합
    articles = []

    with open(os.path.join(DATA_DIR, "news_guardian_classified.json"), encoding="utf-8") as f:
        guardian_cls = json.load(f)
    for a in guardian_cls:
        attr = NORMALIZE.get(a.get("primary", ""), "")
        if attr not in ATTRS_KR:
            continue
        aid = a.get("article_id", "")
        text = guardian_map.get(aid, "")
        if len(text) > 20:
            articles.append({"text": text, "source": "guardian", "attr": attr, "lang": "en"})

    with open(os.path.join(DATA_DIR, "news_nyt_classified.json"), encoding="utf-8") as f:
        nyt_cls = json.load(f)
    for a in nyt_cls:
        attr = NORMALIZE.get(a.get("primary", ""), "")
        if attr not in ATTRS_KR:
            continue
        aid = a.get("article_id", "")
        text = nyt_map.get(aid, "")
        if len(text) > 20:
            articles.append({"text": text, "source": "nyt", "attr": attr, "lang": "en"})

    with open(os.path.join(DATA_DIR, "news_naver_classified.json"), encoding="utf-8") as f:
        naver_cls = json.load(f)
    for a in naver_cls:
        attr = a.get("primary", "")
        if attr not in ATTRS_KR:
            continue
        aid = a.get("article_id", "")
        text = naver_map.get(aid, "")
        if len(text) > 20:
            articles.append({"text": text, "source": "naver", "attr": attr, "lang": "ko"})

    return articles


def run_bertopic_per_attr(articles):
    """정책 속성별로 BERTopic 실행."""
    print("\nLoading multilingual SBERT model...")
    model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

    all_results = {}

    for attr in ATTRS_KR:
        subset = [a for a in articles if a["attr"] == attr]
        if len(subset) < 20:
            print(f"\n[{attr}] {len(subset)}건 — 너무 적어 스킵")
            continue

        texts = [a["text"] for a in subset]
        sources = [a["source"] for a in subset]
        print(f"\n[{attr}] {len(texts)}건 임베딩 중...", flush=True)

        # 임베딩
        embeddings = model.encode(texts, show_progress_bar=False, batch_size=64)

        # BERTopic 설정 (결정론적)
        umap_model = UMAP(
            n_neighbors=15, n_components=5, min_dist=0.0,
            metric='cosine', random_state=42
        )
        hdbscan_model = HDBSCAN(
            min_cluster_size=max(5, len(texts) // 30),
            min_samples=3,
            metric='euclidean',
            prediction_data=True
        )
        custom_stopwords = [
            # 영어 일반
            'ai', 'artificial', 'intelligence', 'the', 'and', 'for', 'that', 'with',
            'has', 'have', 'are', 'was', 'were', 'will', 'its', 'but', 'not', 'from',
            'this', 'can', 'new', 'more', 'also', 'been', 'could', 'would', 'about',
            'says', 'said', 'one', 'two', 'three', 'year', 'years', 'use', 'used',
            'using', 'make', 'like', 'just', 'get', 'how', 'what', 'than', 'into',
            'over', 'such', 'after', 'before', 'other', 'most', 'some', 'only',
            'first', 'last', 'people', 'world', 'company', 'companies', 'technology',
            # 한국어 일반
            '인공지능', '기술', '이용', '활용', '관련', '위한', '대한', '통해',
            '있는', '하는', '것으로', '이번', '대해', '따르면', '것이', '밝혔다',
            '했다', '된다', '있다', '한다', '등의', '수행', '제공', '기반',
            '에서', '으로', '까지', '부터', '에게', '라고', '지난', '최근',
            '또한', '이를', '이에', '이후', '가능', '확대', '강화', '추진',
            '것으로', '나타났다', '전했다', '보도했다', '보도',
            # 한국어 기업/약어
            '삼성', '네이버', '카카오', '현대', '한국',
            # 노이즈
            'quot', 'amp', 'lt', 'gt', 'nbsp', 'hd',
        ]
        vectorizer = CountVectorizer(
            stop_words=custom_stopwords,
            min_df=2,
            ngram_range=(1, 2),
            token_pattern=r'(?u)\b[a-zA-Z가-힣]{2,}\b',  # 숫자 제외, 2자 이상
        )

        topic_model = BERTopic(
            umap_model=umap_model,
            hdbscan_model=hdbscan_model,
            vectorizer_model=vectorizer,
            nr_topics="auto",
            verbose=False,
        )

        topics, probs = topic_model.fit_transform(texts, embeddings)

        # 토픽 정보 추출
        topic_info = topic_model.get_topic_info()
        n_topics = len([t for t in topic_info["Topic"] if t != -1])
        outliers = sum(1 for t in topics if t == -1)

        print(f"  토픽 {n_topics}개 발견, 아웃라이어 {outliers}건 ({outliers/len(texts)*100:.0f}%)")

        topics_data = []
        for topic_id in sorted(set(topics)):
            if topic_id == -1:
                continue
            words = topic_model.get_topic(topic_id)
            top_words = [w[0] for w in words[:10]]
            count = sum(1 for t in topics if t == topic_id)
            # 소스별 분포
            source_dist = {}
            for t, s in zip(topics, sources):
                if t == topic_id:
                    source_dist[s] = source_dist.get(s, 0) + 1

            topics_data.append({
                "topic_id": topic_id,
                "keywords": top_words,
                "count": count,
                "source_dist": source_dist,
            })

            print(f"  Topic {topic_id} ({count}건): {', '.join(top_words[:5])}")

        all_results[attr] = {
            "n_articles": len(texts),
            "n_topics": n_topics,
            "n_outliers": outliers,
            "topics": topics_data,
        }

    return all_results


def label_topics_with_gpt(results):
    """GPT로 토픽 라벨링 (키워드 → 소주제 이름)."""
    print("\n=== GPT 토픽 라벨링 ===")

    for attr, data in results.items():
        attr_en = KR_TO_EN.get(attr, attr)
        for topic in data["topics"]:
            keywords = ", ".join(topic["keywords"][:8])
            prompt = f"""Given these keywords from news articles about AI policy ({attr_en}):
Keywords: {keywords}

Provide a concise subtopic label (3-6 words) in both English and Korean.
Respond ONLY with JSON: {{"en": "English label", "ko": "Korean label"}}"""

            try:
                response = client.chat.completions.create(
                    model="gpt-4.1-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                label = json.loads(response.choices[0].message.content)
                topic["label_en"] = label.get("en", "")
                topic["label_ko"] = label.get("ko", "")
                print(f"  [{attr}] Topic {topic['topic_id']}: {topic['label_ko']} / {topic['label_en']}")
            except Exception as e:
                topic["label_en"] = ", ".join(topic["keywords"][:3])
                topic["label_ko"] = topic["label_en"]
                print(f"  [{attr}] Topic {topic['topic_id']}: ERROR {e}")

            time.sleep(0.2)

    return results


def main():
    print("=== BERTopic 소주제 추출 ===\n")

    # 1. 뉴스 로드
    articles = load_news()
    print(f"전체 기사: {len(articles)}건")
    from collections import Counter
    attr_dist = Counter(a["attr"] for a in articles)
    source_dist = Counter(a["source"] for a in articles)
    print(f"소스별: {dict(source_dist)}")
    for attr, cnt in attr_dist.most_common():
        print(f"  {attr}: {cnt}")

    # 2. 속성별 BERTopic
    results = run_bertopic_per_attr(articles)

    # 3. GPT 라벨링
    results = label_topics_with_gpt(results)

    # 4. 저장
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n저장: {OUTPUT}")

    # 요약
    total_topics = sum(d["n_topics"] for d in results.values())
    print(f"\n총 소주제: {total_topics}개 (10개 정책 속성)")


if __name__ == "__main__":
    main()
