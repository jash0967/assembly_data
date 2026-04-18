"""BERTopic v3: 책임/윤리AI 소주제 문제 개선.

v2 대비 3가지 개선:
1. 카테고리 정의 키워드를 stopword에서 제거 (c-TF-IDF 변별력 향상)
2. min_cluster_size 완화 (n//40 → n//60): 작은 클러스터도 인식
3. 언어별 분리 실행 후 합산: 영어(Guardian+NYT) / 한국어(Naver) 따로 클러스터링

Usage:
    python subtopic_bertopic_v3.py [--attr 책임/윤리AI]  # 특정 속성만
    python subtopic_bertopic_v3.py                        # 전체
"""
import json
import os
import sys
import time
import html as html_mod
import re
import argparse

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
OUTPUT = os.path.join(DATA_DIR, "subtopics_bertopic_v3.json")

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

# ── 개선 1: 카테고리 정의 키워드를 각 속성별 stopword에 추가 ──
# 해당 카테고리 전체에 등장하는 단어는 소주제 변별력이 없으므로 제거
ATTR_EXTRA_STOPWORDS = {
    '책임/윤리AI': [
        'ethical', 'ethics', 'ethic', '윤리', '윤리적', '책임',
        'responsible', 'responsibility', 'algorithm', '알고리즘',
        'accountability', 'trustworthy', '신뢰', '신뢰할',
    ],
    'AI안전': [
        'safety', 'safe', '안전', '안전성', 'risk', '위험', '위해',
        'harm', 'harmful', 'dangerous', 'danger',
    ],
    '산업정책': [
        'policy', 'industrial', '산업', '정책', '지원', '육성', '진흥',
        'industry', 'development', '개발', '혁신', 'innovation',
    ],
    '공익/소비자보호': [
        'consumer', 'public', '소비자', '공익', '보호', '피해',
        'protection', 'protect', 'welfare',
    ],
    '국가안보': [
        'security', 'national', '안보', '국가', '보안', '국방',
        'defense', 'defence', 'threat', '위협',
    ],
    '선거/민주주의': [
        'election', 'democracy', '선거', '민주주의', '민주',
        'democratic', 'vote', 'voter', 'voting', '투표',
    ],
    '시장경쟁/독과점': [
        'market', 'competition', '시장', '경쟁', '독점',
        'antitrust', 'monopoly', 'competitive',
    ],
    '노동/고용': [
        'labor', 'labour', 'employment', '노동', '고용', '일자리',
        'worker', 'workers', '근로', '근로자', 'job', 'jobs',
    ],
    '저작권/지식재산': [
        'copyright', 'intellectual', '저작권', '지식재산', '저작물',
        'property', 'creator', 'creative', '창작',
    ],
    '국제협력': [
        'international', 'cooperation', '국제', '협력', '협정',
        'global', 'bilateral', 'multilateral', '다자', '글로벌',
    ],
}

SEED_TOPICS = {
    '책임/윤리AI': [
        ["transparency", "explainability", "black box", "투명성", "설명가능"],
        ["facial recognition", "bias", "discrimination", "안면인식", "편향", "차별"],
        ["governance", "oversight", "regulation", "거버넌스", "감독", "규제"],
        ["hiring", "recruitment", "résumé", "채용", "면접", "공정"],
        ["chatbot", "conversational", "챗봇", "대화형"],
        ["audit", "impact assessment", "영향평가", "감사"],
        ["education", "school", "student", "교육", "학교", "학생"],
        ["corporate", "company", "기업", "회사", "자체규제"],
    ],
    'AI안전': [
        ["deepfake", "synthetic", "fake", "딥페이크", "가짜"],
        ["autonomous", "self-driving", "vehicle", "자율주행", "사고"],
        ["cybersecurity", "hacking", "vulnerability", "사이버", "보안"],
        ["medical", "healthcare", "diagnosis", "의료", "오진"],
        ["child", "children", "아동", "청소년"],
    ],
    '산업정책': [
        ["chip", "semiconductor", "nvidia", "반도체", "칩"],
        ["datacenter", "infrastructure", "cloud", "데이터센터", "인프라"],
        ["startup", "investment", "funding", "스타트업", "투자"],
        ["healthcare", "biotech", "drug", "바이오", "신약"],
        ["energy", "power", "grid", "에너지", "전력"],
    ],
    '공익/소비자보호': [
        ["privacy", "data", "personal", "개인정보", "프라이버시"],
        ["health", "patient", "medical", "건강", "환자"],
        ["education", "school", "student", "교육", "학생"],
        ["fraud", "scam", "허위광고", "사기"],
        ["disinformation", "misinformation", "허위정보"],
    ],
    '국가안보': [
        ["china", "chinese", "beijing", "중국", "미중"],
        ["military", "defense", "pentagon", "군사", "국방"],
        ["cyber", "attack", "espionage", "사이버", "해킹"],
        ["surveillance", "intelligence", "spy", "감시", "정보"],
    ],
    '선거/민주주의': [
        ["deepfake", "political", "campaign", "딥페이크", "정치광고"],
        ["manipulation", "interference", "조작", "개입"],
        ["misinformation", "disinformation", "허위정보", "가짜뉴스"],
    ],
    '시장경쟁/독과점': [
        ["antitrust", "monopoly", "독점", "독과점"],
        ["platform", "gatekeeper", "플랫폼", "시장지배"],
        ["merger", "acquisition", "big tech", "인수", "빅테크"],
    ],
    '노동/고용': [
        ["automation", "displacement", "자동화", "일자리 감소"],
        ["gig", "freelance", "platform work", "플랫폼노동"],
        ["reskilling", "training", "재교육", "전환"],
    ],
    '저작권/지식재산': [
        ["music", "art", "creative", "음악", "창작"],
        ["training data", "scraping", "fair use", "학습데이터", "크롤링"],
        ["likeness", "voice", "deepfake", "초상", "퍼블리시티"],
    ],
    '국제협력': [
        ["treaty", "agreement", "조약", "협정"],
        ["standard", "harmonization", "표준", "호환"],
        ["united nations", "oecd", "g7", "유엔", "다자"],
    ],
}

BASE_STOPWORDS = [
    'ai', 'artificial', 'intelligence', 'the', 'and', 'for', 'that', 'with',
    'has', 'have', 'are', 'was', 'were', 'will', 'its', 'but', 'not', 'from',
    'this', 'can', 'new', 'more', 'also', 'been', 'could', 'would', 'about',
    'says', 'said', 'use', 'used', 'using', 'make', 'like', 'just',
    'people', 'world', 'company', 'companies', 'technology',
    'year', 'years', 'one', 'two', 'three', 'first', 'last',
    '인공지능', '기술', '이용', '활용', '관련', '위한', '대한', '통해',
    '있는', '하는', '것으로', '밝혔다', '했다', '있다', '한다', '나타났다',
    '지난', '최근', '또한', '이를', '이에', '이후', '가능', '확대', '강화', '추진',
    '제공', '기반', '에서', '으로', '까지', '부터', '라고',
    'quot', 'amp', 'lt', 'gt', 'nbsp',
]


def clean_text(text):
    text = html_mod.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def load_news():
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


def run_bertopic_single(texts, sources, langs, attr, sbert, lang_filter=None):
    """단일 언어 그룹에 대해 BERTopic 실행."""

    if lang_filter:
        idx = [i for i, l in enumerate(langs) if l == lang_filter]
        texts = [texts[i] for i in idx]
        sources = [sources[i] for i in idx]
        langs = [langs[i] for i in idx]

    n = len(texts)
    if n < 15:
        return None, None, None

    print(f"    임베딩 {n}건 ({lang_filter or 'all'})...", flush=True)
    embeddings = sbert.encode(texts, show_progress_bar=False, batch_size=64)

    # ── 개선 1: 카테고리 정의 stopwords 추가 ──
    stopwords = BASE_STOPWORDS + ATTR_EXTRA_STOPWORDS.get(attr, [])

    # ── 개선 2: min_cluster_size 완화 (n//60) ──
    mcs = max(5, n // 60)

    # ── 개선 3: 대규모 데이터에서 UMAP 차원 확대 ──
    n_components = 10 if n > 800 else 5
    n_neighbors = 20 if n > 800 else 15

    umap_model = UMAP(
        n_neighbors=n_neighbors,
        n_components=n_components,
        min_dist=0.0,
        metric='cosine',
        random_state=42
    )
    hdbscan_model = HDBSCAN(
        min_cluster_size=mcs,
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
    seeds = SEED_TOPICS.get(attr, None)

    topic_model = BERTopic(
        embedding_model=sbert,
        umap_model=umap_model,
        hdbscan_model=hdbscan_model,
        vectorizer_model=vectorizer,
        representation_model=KeyBERTInspired(),
        seed_topic_list=seeds,
        nr_topics="auto",
        verbose=False,
    )

    topics, probs = topic_model.fit_transform(texts, embeddings)
    return topics, embeddings, topic_model, texts, sources


def run_bertopic_v3(articles, target_attrs=None):
    """개선된 BERTopic v3: 언어 분리 + 카테고리 stopword + min_cluster_size 완화."""
    print("\nLoading multilingual SBERT model...")
    sbert = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")

    all_results = {}

    for attr in ATTRS_KR:
        if target_attrs and attr not in target_attrs:
            continue

        subset = [a for a in articles if a["attr"] == attr]
        if len(subset) < 20:
            print(f"\n[{attr}] {len(subset)}건 — 스킵")
            continue

        texts = [a["text"] for a in subset]
        sources = [a["source"] for a in subset]
        langs = [a["lang"] for a in subset]

        n_en = sum(1 for l in langs if l == "en")
        n_ko = sum(1 for l in langs if l == "ko")

        print(f"\n{'='*60}")
        print(f"[{attr}] 총 {len(texts)}건 — EN:{n_en} / KO:{n_ko}")
        print(f"  min_cluster_size = max(5, {len(texts)}//60) = {max(5, len(texts)//60)}")

        # ── 개선 4: 언어별 분리 실행 ──
        # 영어 (Guardian + NYT)와 한국어 (Naver)를 별도로 클러스터링
        run_separately = (n_en >= 30 and n_ko >= 30)

        if run_separately:
            print(f"  언어 분리 모드: EN {n_en}건 / KO {n_ko}건")
            combined_topics_data = []
            topic_id_offset = 0
            total_outliers = 0
            total_articles = len(texts)

            for lang_tag, lang_label in [("en", "영어"), ("ko", "한국어")]:
                lang_idx = [i for i, l in enumerate(langs) if l == lang_tag]
                lang_texts = [texts[i] for i in lang_idx]
                lang_sources = [sources[i] for i in lang_idx]
                n = len(lang_texts)
                if n < 15:
                    print(f"  [{lang_label}] {n}건 — 너무 적어 스킵")
                    continue

                print(f"\n  [{lang_label} {n}건]")
                embeddings = sbert.encode(lang_texts, show_progress_bar=False, batch_size=64)

                stopwords = BASE_STOPWORDS + ATTR_EXTRA_STOPWORDS.get(attr, [])
                mcs = max(5, n // 60)
                n_components = 10 if n > 800 else 5
                n_neighbors = 20 if n > 800 else 15

                print(f"    min_cluster_size={mcs}, n_components={n_components}, n_neighbors={n_neighbors}")

                umap_model = UMAP(n_neighbors=n_neighbors, n_components=n_components,
                                  min_dist=0.0, metric='cosine', random_state=42)
                hdbscan_model = HDBSCAN(min_cluster_size=mcs, min_samples=3,
                                        metric='euclidean', prediction_data=True)
                vectorizer = CountVectorizer(stop_words=stopwords, min_df=2,
                                             ngram_range=(1, 2),
                                             token_pattern=r'(?u)\b[a-zA-Z가-힣]{2,}\b')
                seeds = SEED_TOPICS.get(attr, None)

                topic_model = BERTopic(
                    embedding_model=sbert,
                    umap_model=umap_model,
                    hdbscan_model=hdbscan_model,
                    vectorizer_model=vectorizer,
                    representation_model=KeyBERTInspired(),
                    seed_topic_list=seeds,
                    nr_topics="auto",
                    verbose=False,
                )
                topics, probs = topic_model.fit_transform(lang_texts, embeddings)

                topic_info = topic_model.get_topic_info()
                n_topics = len([t for t in topic_info["Topic"] if t != -1])
                outliers = sum(1 for t in topics if t == -1)
                total_outliers += outliers
                print(f"    → 토픽 {n_topics}개, 아웃라이어 {outliers}건 ({outliers/n*100:.0f}%)")

                for topic_id in sorted(set(topics)):
                    if topic_id == -1:
                        continue
                    words = topic_model.get_topic(topic_id)
                    top_words = [w[0] for w in words[:10] if w[0]]
                    count = sum(1 for t in topics if t == topic_id)
                    source_dist = {}
                    for t, s in zip(topics, lang_sources):
                        if t == topic_id:
                            source_dist[s] = source_dist.get(s, 0) + 1

                    # 대표 문서
                    t_idx = [i for i, t in enumerate(topics) if t == topic_id]
                    t_emb = embeddings[t_idx]
                    centroid = t_emb.mean(axis=0)
                    dists = np.linalg.norm(t_emb - centroid, axis=1)
                    closest = np.argsort(dists)[:5]
                    rep_docs = [lang_texts[t_idx[i]][:200] for i in closest]

                    new_id = topic_id + topic_id_offset
                    combined_topics_data.append({
                        "topic_id": new_id,
                        "lang": lang_tag,
                        "keywords": top_words,
                        "count": count,
                        "source_dist": source_dist,
                        "representative_docs": rep_docs,
                    })
                    print(f"    Topic {new_id} [{lang_tag}] ({count}건): {', '.join(top_words[:5])}")

                # 다음 언어 topic_id가 겹치지 않도록 offset 조정
                topic_id_offset += (max((t for t in topics if t != -1), default=-1) + 2)

            all_results[attr] = {
                "n_articles": total_articles,
                "n_topics": len(combined_topics_data),
                "n_outliers": total_outliers,
                "has_hierarchy": False,
                "topics": combined_topics_data,
            }

        else:
            # 언어 분리 불가 → 통합 실행 (기존 방식 + 개선)
            print(f"  통합 모드")
            embeddings = sbert.encode(texts, show_progress_bar=False, batch_size=64)

            stopwords = BASE_STOPWORDS + ATTR_EXTRA_STOPWORDS.get(attr, [])
            mcs = max(5, len(texts) // 60)
            n_components = 10 if len(texts) > 800 else 5
            n_neighbors = 20 if len(texts) > 800 else 15

            umap_model = UMAP(n_neighbors=n_neighbors, n_components=n_components,
                              min_dist=0.0, metric='cosine', random_state=42)
            hdbscan_model = HDBSCAN(min_cluster_size=mcs, min_samples=3,
                                    metric='euclidean', prediction_data=True)
            vectorizer = CountVectorizer(stop_words=stopwords, min_df=2,
                                         ngram_range=(1, 2),
                                         token_pattern=r'(?u)\b[a-zA-Z가-힣]{2,}\b')
            seeds = SEED_TOPICS.get(attr, None)

            topic_model = BERTopic(
                embedding_model=sbert,
                umap_model=umap_model,
                hdbscan_model=hdbscan_model,
                vectorizer_model=vectorizer,
                representation_model=KeyBERTInspired(),
                seed_topic_list=seeds,
                nr_topics="auto",
                verbose=False,
            )
            topics, probs = topic_model.fit_transform(texts, embeddings)

            topic_info = topic_model.get_topic_info()
            n_topics = len([t for t in topic_info["Topic"] if t != -1])
            outliers = sum(1 for t in topics if t == -1)
            print(f"  → 토픽 {n_topics}개, 아웃라이어 {outliers}건 ({outliers/len(texts)*100:.0f}%)")

            topics_data = []
            for topic_id in sorted(set(topics)):
                if topic_id == -1:
                    continue
                words = topic_model.get_topic(topic_id)
                top_words = [w[0] for w in words[:10] if w[0]]
                count = sum(1 for t in topics if t == topic_id)
                source_dist = {}
                for t, s in zip(topics, sources):
                    if t == topic_id:
                        source_dist[s] = source_dist.get(s, 0) + 1

                t_idx = [i for i, t in enumerate(topics) if t == topic_id]
                t_emb = embeddings[t_idx]
                centroid = t_emb.mean(axis=0)
                dists = np.linalg.norm(t_emb - centroid, axis=1)
                closest = np.argsort(dists)[:5]
                rep_docs = [texts[t_idx[i]][:200] for i in closest]

                topics_data.append({
                    "topic_id": topic_id,
                    "lang": "mixed",
                    "keywords": top_words,
                    "count": count,
                    "source_dist": source_dist,
                    "representative_docs": rep_docs,
                })
                print(f"  Topic {topic_id} ({count}건): {', '.join(top_words[:5])}")

            all_results[attr] = {
                "n_articles": len(texts),
                "n_topics": n_topics,
                "n_outliers": outliers,
                "has_hierarchy": False,
                "topics": topics_data,
            }

    return all_results


def label_with_representative_docs(results):
    print(f"\n{'='*60}")
    print("=== 대표 문서 기반 GPT 라벨링 ===")

    for attr, data in results.items():
        attr_en = KR_TO_EN.get(attr, attr)
        for topic in data["topics"]:
            docs = topic.get("representative_docs", [])
            keywords = ", ".join(topic["keywords"][:8])
            lang_hint = topic.get("lang", "mixed")

            if docs:
                docs_text = "\n".join(f"  {i+1}. {d}" for i, d in enumerate(docs))
                prompt = f"""You are analyzing news articles about AI policy, in the area of "{attr_en}".
Language of articles: {lang_hint}

Top keywords for this cluster: {keywords}

5 representative articles:
{docs_text}

Based on keywords AND articles, provide a concise subtopic label (3-6 words) specific to what distinguishes this cluster within {attr_en}.

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
                lang_tag = f"[{topic.get('lang','?')}]"
                print(f"  [{attr}] {lang_tag} Topic {topic['topic_id']} ({topic['count']}건): {topic['label_ko']} / {topic['label_en']}")
            except Exception as e:
                topic["label_en"] = ", ".join(topic["keywords"][:3])
                topic["label_ko"] = topic["label_en"]
                print(f"  [{attr}] Topic {topic['topic_id']}: ERROR {e}")

            time.sleep(0.2)

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attr", nargs="+", help="특정 속성만 실행 (예: 책임/윤리AI 산업정책)")
    args = parser.parse_args()

    print("=== BERTopic v3: 개선된 소주제 추출 ===\n")
    print("개선사항:")
    print("  1. 카테고리 정의 키워드 → 해당 속성 stopword 추가")
    print("  2. min_cluster_size: n//40 → n//60 (더 작은 클러스터도 인식)")
    print("  3. 언어 분리 (EN: Guardian+NYT / KO: Naver) 후 합산")
    print("  4. 대규모 데이터: UMAP n_components=10, n_neighbors=20\n")

    articles = load_news()
    from collections import Counter
    attr_dist = Counter(a["attr"] for a in articles)
    print(f"전체: {len(articles)}건")
    for attr, cnt in attr_dist.most_common():
        mark = " ◀" if args.attr and attr in args.attr else ""
        print(f"  {attr}: {cnt}{mark}")

    target = args.attr if args.attr else None

    # 기존 v3 결과가 있으면 로드 (다른 속성은 유지)
    existing = {}
    if os.path.exists(OUTPUT) and target:
        with open(OUTPUT, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"\n기존 v3 결과 로드: {list(existing.keys())}")

    results = run_bertopic_v3(articles, target_attrs=target)

    # 기존 결과 병합 (target 속성만 업데이트)
    if target:
        for attr in existing:
            if attr not in results:
                results[attr] = existing[attr]

    results = label_with_representative_docs(
        {k: v for k, v in results.items() if (not target or k in target)}
        if target else results
    )

    # 라벨링된 결과만 업데이트
    for attr in list(results.keys()):
        if target and attr not in target:
            del results[attr]

    # 기존과 병합하여 저장
    final = dict(existing)
    final.update(results)

    # 대표 문서 제거 후 저장
    for attr, data in final.items():
        for topic in data["topics"]:
            topic.pop("representative_docs", None)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)

    total_topics = sum(d["n_topics"] for d in final.values())
    print(f"\n{'='*60}")
    print(f"저장: {OUTPUT}")
    print(f"총 소주제: {total_topics}개")

    print(f"\n{'속성':20s} {'기사':>6s} {'토픽':>4s} {'아웃라이어':>8s}")
    print("-" * 44)
    for attr in ATTRS_KR:
        if attr in final:
            d = final[attr]
            pct = d['n_outliers'] / d['n_articles'] * 100 if d['n_articles'] else 0
            print(f"{attr:20s} {d['n_articles']:>6d} {d['n_topics']:>4d} {d['n_outliers']:>6d} ({pct:.0f}%)")


if __name__ == "__main__":
    main()
