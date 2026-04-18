"""BERTopic v2: KeyBERTInspired + Hierarchical + Guided + 대표문서 라벨링.

4가지 개선을 모두 적용하여 소주제 추출 품질을 비교.

Usage:
    python subtopic_bertopic_v2.py
"""
import json
import os
import sys
import time
import html as html_mod
import re

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
from sentence_transformers import SentenceTransformer
from bertopic import BERTopic
from bertopic.representation import KeyBERTInspired
from umap import UMAP
from hdbscan import HDBSCAN
from sklearn.feature_extraction.text import CountVectorizer
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
OUTPUT = os.path.join(DATA_DIR, "subtopics_bertopic_v2.json")

client = OpenAI()

ATTRS_KR = [
    'AI안전', '책임/윤리AI', '산업정책', '공익/소비자보호', '국가안보',
    '선거/민주주의', '시장경쟁/독과점', '노동/고용', '저작권/지식재산', '국제협력',
]
ATTRS_EN = [
    'Safety', 'Responsible and ethical AI', 'Industrial policy',
    'Public interest', 'National security', 'Elections',
    'Market efficiency and power concentration (antitrust)',
    'Labor', 'Copyright', 'International collaboration',
]
KR_TO_EN = dict(zip(ATTRS_KR, ATTRS_EN))

NORMALIZE = {
    'Market efficiency and power concentration (antitrust)': '시장경쟁/독과점',
    'Safety': 'AI안전', 'Responsible and ethical AI': '책임/윤리AI',
    'National security': '국가안보', 'Industrial policy': '산업정책',
    'Public interest': '공익/소비자보호', 'Labor': '노동/고용',
    'Copyright': '저작권/지식재산', 'International collaboration': '국제협력',
    'Elections': '선거/민주주의',
}

# ── Guided BERTopic용 시드 키워드 ──
SEED_TOPICS = {
    'AI안전': [
        ["deepfake", "synthetic", "fake", "딥페이크", "가짜"],
        ["autonomous", "self-driving", "vehicle", "자율주행", "사고"],
        ["cybersecurity", "hacking", "vulnerability", "사이버", "보안"],
        ["bias", "discrimination", "fairness", "편향", "차별"],
        ["child", "children", "safety", "아동", "청소년"],
        ["medical", "healthcare", "diagnosis", "의료", "오진"],
    ],
    '책임/윤리AI': [
        ["transparency", "explainability", "black box", "투명성", "설명가능"],
        ["accountability", "liability", "responsibility", "책임", "배상"],
        ["governance", "oversight", "regulation", "거버넌스", "감독"],
        ["audit", "assessment", "impact", "감사", "영향평가"],
        ["hiring", "recruitment", "employment", "채용", "공정"],
    ],
    '산업정책': [
        ["chip", "semiconductor", "nvidia", "반도체", "칩"],
        ["datacenter", "infrastructure", "cloud", "데이터센터", "인프라"],
        ["startup", "investment", "funding", "스타트업", "투자"],
        ["education", "training", "workforce", "교육", "인력"],
        ["healthcare", "biotech", "drug", "바이오", "신약"],
        ["energy", "power", "grid", "에너지", "전력"],
    ],
    '공익/소비자보호': [
        ["privacy", "data protection", "personal", "개인정보", "프라이버시"],
        ["health", "patient", "medical", "건강", "환자"],
        ["education", "school", "student", "교육", "학생"],
        ["consumer", "fraud", "scam", "소비자", "사기"],
        ["environment", "climate", "energy", "환경", "기후"],
        ["disinformation", "misinformation", "fake news", "허위정보", "가짜뉴스"],
    ],
    '국가안보': [
        ["china", "chinese", "beijing", "중국", "미중"],
        ["military", "defense", "pentagon", "군사", "국방"],
        ["cyber", "attack", "espionage", "사이버", "해킹"],
        ["nuclear", "weapons", "missile", "핵", "무기"],
        ["surveillance", "intelligence", "spy", "감시", "정보"],
    ],
    '선거/민주주의': [
        ["election", "voting", "voter", "선거", "투표"],
        ["deepfake", "political", "campaign", "딥페이크", "정치광고"],
        ["democracy", "manipulation", "interference", "민주주의", "조작"],
    ],
    '시장경쟁/독과점': [
        ["antitrust", "monopoly", "competition", "독점", "독과점"],
        ["platform", "gatekeeper", "market power", "플랫폼", "시장지배"],
        ["pricing", "algorithm", "collusion", "가격", "담합"],
        ["merger", "acquisition", "big tech", "인수", "빅테크"],
    ],
    '노동/고용': [
        ["job", "employment", "automation", "일자리", "자동화"],
        ["worker", "labor", "workplace", "노동자", "근로"],
        ["gig", "freelance", "platform work", "플랫폼노동", "프리랜서"],
        ["reskilling", "training", "displacement", "재교육", "전환"],
    ],
    '저작권/지식재산': [
        ["copyright", "author", "creator", "저작권", "저작물"],
        ["music", "art", "creative", "음악", "창작"],
        ["training data", "scraping", "fair use", "학습데이터", "크롤링"],
        ["likeness", "voice", "deepfake", "초상", "퍼블리시티"],
    ],
    '국제협력': [
        ["treaty", "agreement", "bilateral", "조약", "협정"],
        ["standard", "harmonization", "interoperability", "표준", "호환"],
        ["united nations", "oecd", "g7", "유엔", "다자"],
        ["summit", "partnership", "alliance", "정상회담", "동맹"],
    ],
}


def clean_text(text):
    text = html_mod.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def load_news():
    """3개 소스 뉴스 로드."""
    print("Loading raw texts...")

    # Guardian
    with open(os.path.join(DATA_DIR, "guardian_articles_raw.json"), encoding="utf-8") as f:
        guardian_raw = json.load(f)
    guardian_map = {}
    for a in guardian_raw:
        title = a.get("title", "") or a.get("webTitle", "")
        trail = a.get("trail_text", "") or (a.get("fields", {}) or {}).get("trailText", "") or ""
        text = clean_text(f"{title}. {trail}")
        for key in ["id", "url"]:
            aid = a.get(key, "")
            if aid:
                guardian_map[aid] = text
                if key == "id":
                    guardian_map[f"https://www.theguardian.com/{aid}"] = text

    # NYT
    with open(os.path.join(DATA_DIR, "nyt_articles_raw.json"), encoding="utf-8") as f:
        nyt_raw = json.load(f)
    nyt_map = {}
    for a in nyt_raw:
        title = a.get("title", "")
        if not title:
            h = a.get("headline", {})
            title = h.get("main", "") if isinstance(h, dict) else str(h)
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
        title = a.get("title", "")
        desc = a.get("description", "")
        text = clean_text(f"{title}. {desc}")
        for key in ["link", "originallink"]:
            aid = a.get(key, "")
            if aid:
                naver_map[aid] = text

    print(f"  Guardian: {len(guardian_map)}, NYT: {len(nyt_map)}, Naver: {len(naver_map)}")

    # 분류 결과 + 원본 텍스트 결합
    articles = []
    for cls_file, source, lang, text_map, normalize in [
        ("news_guardian_classified.json", "guardian", "en", guardian_map, True),
        ("news_nyt_classified.json", "nyt", "en", nyt_map, True),
        ("news_naver_classified.json", "naver", "ko", naver_map, False),
    ]:
        with open(os.path.join(DATA_DIR, cls_file), encoding="utf-8") as f:
            cls_data = json.load(f)
        for a in cls_data:
            attr = a.get("primary", "")
            if normalize:
                attr = NORMALIZE.get(attr, attr)
            if attr not in ATTRS_KR:
                continue
            aid = a.get("article_id", "")
            text = text_map.get(aid, "")
            if len(text) > 20:
                articles.append({"text": text, "source": source, "attr": attr, "lang": lang})

    return articles


def run_bertopic_v2(articles):
    """개선된 BERTopic: KeyBERTInspired + Guided + Hierarchical."""
    print("\nLoading multilingual SBERT model...")
    sbert = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

    stopwords = [
        'ai', 'artificial', 'intelligence', 'the', 'and', 'for', 'that', 'with',
        'has', 'have', 'are', 'was', 'were', 'will', 'its', 'but', 'not', 'from',
        'this', 'can', 'new', 'more', 'also', 'been', 'could', 'would', 'about',
        'says', 'said', 'use', 'used', 'using', 'make', 'like', 'just',
        'people', 'world', 'company', 'companies', 'technology',
        '인공지능', '기술', '이용', '활용', '관련', '위한', '대한', '통해',
        '있는', '하는', '것으로', '밝혔다', '했다', '있다', '한다', '나타났다',
        'quot', 'amp', 'lt', 'gt', 'nbsp',
    ]

    all_results = {}

    for attr in ATTRS_KR:
        subset = [a for a in articles if a["attr"] == attr]
        if len(subset) < 20:
            print(f"\n[{attr}] {len(subset)}건 — 스킵")
            continue

        texts = [a["text"] for a in subset]
        sources = [a["source"] for a in subset]
        print(f"\n{'='*60}")
        print(f"[{attr}] {len(texts)}건 (guardian:{sum(1 for s in sources if s=='guardian')}, nyt:{sum(1 for s in sources if s=='nyt')}, naver:{sum(1 for s in sources if s=='naver')})")

        # 임베딩
        print(f"  임베딩 중...", flush=True)
        embeddings = sbert.encode(texts, show_progress_bar=False, batch_size=64)

        # ── KeyBERTInspired representation ──
        representation = KeyBERTInspired()

        # ── Guided seeds ──
        seeds = SEED_TOPICS.get(attr, None)

        # ── 모델 설정 ──
        umap_model = UMAP(
            n_neighbors=15, n_components=5, min_dist=0.0,
            metric='cosine', random_state=42
        )
        hdbscan_model = HDBSCAN(
            min_cluster_size=max(5, len(texts) // 40),
            min_samples=3,
            metric='euclidean',
            prediction_data=True
        )
        vectorizer = CountVectorizer(
            stop_words=stopwords,
            min_df=2,
            ngram_range=(1, 2),
            token_pattern=r'(?u)\b[a-zA-Z가-힣]{2,}\b',
        )

        topic_model = BERTopic(
            embedding_model=sbert,  # KeyBERTInspired에 필요
            umap_model=umap_model,
            hdbscan_model=hdbscan_model,
            vectorizer_model=vectorizer,
            representation_model=representation,  # KeyBERTInspired
            seed_topic_list=seeds,  # Guided
            nr_topics="auto",
            verbose=False,
        )

        topics, probs = topic_model.fit_transform(texts, embeddings)

        # ── 토픽 정보 ──
        topic_info = topic_model.get_topic_info()
        n_topics = len([t for t in topic_info["Topic"] if t != -1])
        outliers = sum(1 for t in topics if t == -1)
        print(f"  토픽 {n_topics}개, 아웃라이어 {outliers}건 ({outliers/len(texts)*100:.0f}%)")

        # ── 계층적 토픽 ──
        try:
            hierarchical = topic_model.hierarchical_topics(texts)
            has_hierarchy = True
            print(f"  계층적 토픽 트리 생성 완료")
        except Exception as e:
            has_hierarchy = False
            print(f"  계층적 토픽 실패: {e}")

        # ── 토픽별 정보 수집 ──
        topics_data = []
        for topic_id in sorted(set(topics)):
            if topic_id == -1:
                continue

            # KeyBERTInspired 키워드
            words = topic_model.get_topic(topic_id)
            top_words = [w[0] for w in words[:10] if w[0]]
            count = sum(1 for t in topics if t == topic_id)

            # 소스별 분포
            source_dist = {}
            for t, s in zip(topics, sources):
                if t == topic_id:
                    source_dist[s] = source_dist.get(s, 0) + 1

            # 대표 문서 추출 (centroid에 가장 가까운 5건)
            topic_indices = [i for i, t in enumerate(topics) if t == topic_id]
            topic_embeddings = embeddings[topic_indices]
            centroid = topic_embeddings.mean(axis=0)
            distances = np.linalg.norm(topic_embeddings - centroid, axis=1)
            closest_indices = np.argsort(distances)[:5]
            representative_docs = [texts[topic_indices[i]][:200] for i in closest_indices]

            topics_data.append({
                "topic_id": topic_id,
                "keywords": top_words,
                "count": count,
                "source_dist": source_dist,
                "representative_docs": representative_docs,
            })

            print(f"  Topic {topic_id} ({count}건): {', '.join(top_words[:5])}")

        all_results[attr] = {
            "n_articles": len(texts),
            "n_topics": n_topics,
            "n_outliers": outliers,
            "has_hierarchy": has_hierarchy,
            "topics": topics_data,
        }

    return all_results


def label_with_representative_docs(results):
    """대표 문서 기반 GPT 라벨링."""
    print(f"\n{'='*60}")
    print("=== 대표 문서 기반 GPT 라벨링 ===")

    for attr, data in results.items():
        attr_en = KR_TO_EN.get(attr, attr)
        for topic in data["topics"]:
            docs = topic.get("representative_docs", [])
            keywords = ", ".join(topic["keywords"][:8])

            if docs:
                docs_text = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(docs))
                prompt = f"""You are analyzing news articles about AI policy, specifically in the area of "{attr_en}".

Here are the top keywords for this topic cluster: {keywords}

Here are 5 representative articles from this cluster:
{docs_text}

Based on the keywords AND the article content, provide a concise subtopic label (3-6 words) that captures what specifically this cluster is about within {attr_en}.

Respond ONLY with JSON: {{"en": "English label", "ko": "Korean label"}}"""
            else:
                prompt = f"""Keywords from AI policy news about {attr_en}: {keywords}
Provide a concise subtopic label (3-6 words).
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
                print(f"  [{attr}] Topic {topic['topic_id']} ({topic['count']}건): {topic['label_ko']} / {topic['label_en']}")
            except Exception as e:
                topic["label_en"] = ", ".join(topic["keywords"][:3])
                topic["label_ko"] = topic["label_en"]
                print(f"  [{attr}] Topic {topic['topic_id']}: ERROR {e}")

            time.sleep(0.2)

    return results


def main():
    print("=== BERTopic v2: 개선된 소주제 추출 ===\n")

    # 1. 뉴스 로드
    articles = load_news()
    from collections import Counter
    source_dist = Counter(a["source"] for a in articles)
    attr_dist = Counter(a["attr"] for a in articles)
    print(f"\n전체: {len(articles)}건 — {dict(source_dist)}")
    for attr, cnt in attr_dist.most_common():
        print(f"  {attr}: {cnt}")

    # 2. BERTopic v2
    results = run_bertopic_v2(articles)

    # 3. 대표 문서 기반 GPT 라벨링
    results = label_with_representative_docs(results)

    # 4. 저장 (대표 문서는 용량 줄이기 위해 제거)
    for attr, data in results.items():
        for topic in data["topics"]:
            topic.pop("representative_docs", None)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    total_topics = sum(d["n_topics"] for d in results.values())
    print(f"\n{'='*60}")
    print(f"저장: {OUTPUT}")
    print(f"총 소주제: {total_topics}개 (10개 정책 속성)")

    # 요약 표
    print(f"\n{'속성':20s} {'기사':>6s} {'토픽':>4s} {'아웃라이어':>8s}")
    print("-" * 42)
    for attr in ATTRS_KR:
        if attr in results:
            d = results[attr]
            print(f"{attr:20s} {d['n_articles']:>6d} {d['n_topics']:>4d} {d['n_outliers']:>6d} ({d['n_outliers']/d['n_articles']*100:.0f}%)")


if __name__ == "__main__":
    main()
