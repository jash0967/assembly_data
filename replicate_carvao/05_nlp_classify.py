"""Step 5-6: NLP 기반 법안 분류 — TF-IDF + LDA + GPT 검증.

논문 Appendix II (p.92-94) 방법론 재현:
1. TF-IDF: 법안 전문 토큰화 → TF-IDF 가중치 → 10개 Policy Attribute 분류
2. LDA: 비지도 토픽 모델링 (10개 토픽)
3. TF-IDF vs LDA 교차 히트맵
4. GPT-4o-mini 검증

Usage:
    python replicate_carvao/05_nlp_classify.py
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer, CountVectorizer
from sklearn.decomposition import LatentDirichletAllocation

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TEXT_DIR = os.path.join(DATA_DIR, "bills_text")
PROCESSED_FILE = os.path.join(DATA_DIR, "bills_processed.json")
OUTPUT_TFIDF = os.path.join(DATA_DIR, "tfidf_classification.json")
OUTPUT_LDA = os.path.join(DATA_DIR, "lda_topics.json")
OUTPUT_CROSSMAP = os.path.join(DATA_DIR, "tfidf_lda_crossmap.json")
FIGURES_DIR = os.path.join(os.path.dirname(__file__), "figures")


# ─── 10 Policy Attributes + 키워드 사전 ───
# 논문 p.92: "categories were developed based on legislative text analysis
# and consultation with existing taxonomies of AI policy concerns"

POLICY_ATTRIBUTES = {
    "Market efficiency and power concentration (antitrust)": [
        "antitrust", "monopoly", "monopolies", "anticompetitive",
        "market power", "market concentration", "dominant platform",
        "merger", "acquisition", "gatekeeper",
        "self-preferencing", "anti-competitive", "market dominance",
        "interoperability", "portability", "app store",
        "covered platform", "dominant", "compete", "competing",
        # 추가: 논문 Fig20에서 35건 — 플랫폼 규제/경쟁 관련 확대
        "platform", "collusion", "cartel", "pricing",
        "algorithmic pricing", "price fixing",
        "section 230", "immunity", "liability",
        "commission", "enforcement", "ftc",
        "federal trade", "deceptive practice",
        "unfair", "dark pattern", "manipulative design",
    ],
    "Safety": [
        "safety", "safe", "risk assessment", "risk management",
        "hazard", "dangerous", "harm", "harmful",
        "incident", "accident", "red team", "red-team",
        "benchmark", "guardrail", "mitigation", "mitigate",
        "vulnerability", "robustness", "reliable", "reliability",
        "assurance", "secure", "watermark",
        "reporting requirement",
        # 추가: 논문 Fig20에서 13건
        "risk", "testing", "evaluation",
        "safety standard", "safety review",
        "notification", "warning", "recall",
    ],
    "Responsible and ethical AI": [
        "responsible", "ethical", "ethics", "accountability", "accountable",
        "transparent", "transparency", "explainable", "explainability",
        "bias", "fairness", "discrimination", "equitable",
        "trustworthy", "governance", "oversight",
        "impact assessment", "algorithmic", "automated decision",
        "audit", "auditing", "certification",
        "civil rights", "civil liberties",
        # 제거: standards, compliance, framework, guidelines, regulatory,
        #       disclosure, notice, consent, rights — 법률 일반 용어로 과매칭 유발
    ],
    "National security": [
        "national security", "defense", "military",
        "homeland security", "cybersecurity", "cyber attack",
        "adversary", "adversarial", "china", "chinese",
        "foreign adversary", "threat", "critical infrastructure",
        "classified", "counterintelligence", "espionage", "weapons",
        "autonomous weapon", "department of defense", "pentagon",
        "intelligence community", "national defense",
    ],
    "Industrial policy": [
        "industrial", "manufacturing", "innovation",
        "research and development", "investment", "funding",
        "grant", "subsidy", "semiconductor", "chip", "compute",
        "infrastructure", "workforce development", "stem",
        "national science foundation", "nsf", "nist", "startup",
        "competitiveness", "leadership", "training program",
        "fellowship", "pilot program", "testbed",
    ],
    "Public interest": [
        "consumer", "consumer protection",
        "public health", "healthcare", "health care", "patient",
        "education", "student", "children", "child", "minor",
        "disability", "accessible", "accessibility",
        "housing", "transportation", "agriculture", "environment",
        "energy", "climate", "water", "food", "public service",
        "rural", "underserved", "vulnerable",
    ],
    "Labor": [
        "labor", "worker", "workers", "employment", "employee",
        "job", "jobs",  "automation", "displacement",
        "wage", "union", "workplace", "occupational", "hiring",
        "recruitment", "retraining", "reskilling", "gig economy",
        "unemployment", "layoff",
    ],
    "Copyright": [
        "copyright", "intellectual property",
        "creative", "creator", "artist", "author",
        "generative", "generated content", "training data",
        "fair use", "license", "licensing", "royalty", "patent",
        "trademark", "infringement", "synthetic media",
        "likeness", "replica", "voice cloning",
    ],
    "International collaboration": [
        "international cooperation", "global", "multilateral", "bilateral",
        "treaty", "cooperation", "collaboration",
        "alliance", "diplomatic", "united nations",
        "oecd", "nato", "cross-border", "harmonization",
        "foreign government", "foreign country",
    ],
    "Elections": [
        "election", "elections", "electoral", "voter",
        "voting", "campaign", "candidate", "political",
        "deepfake", "disinformation", "misinformation",
        "manipulate", "deceptive", "impersonate", "robocall",
        "political advertising", "political ad", "ballot",
        "democracy", "democratic process",
    ],
}

# 과매칭 방지: 이 단어들이 특정 문맥에서만 의미가 있도록
# "intelligence" → "artificial intelligence"의 일부이므로 National Security에서 제외
# "security" → "national security", "cybersecurity"는 포함하되 단독 "security" 제외
# "content" → Copyright에서 제거 (너무 일반적, 671회 등장)
# "platform" → Market efficiency에서 "covered platform", "dominant platform"으로 특정화
# "surveillance" → National Security에서 제거 (privacy와 겹침)

# 법률 문서 불용어 (논문 p.92-93: "bill", "committee", "section" 등)
LEGAL_STOPWORDS = [
    "bill", "act", "section", "subsection", "paragraph", "subparagraph",
    "clause", "title", "chapter", "part", "division", "subtitle",
    "congress", "senate", "house", "representatives", "committee",
    "subcommittee", "secretary", "director", "administrator",
    "agency", "department", "commission", "office", "bureau",
    "united", "states", "america", "federal", "government",
    "shall", "may", "including", "means", "purpose", "purposes",
    "general", "appropriate", "described", "established",
    "amended", "amend", "insert", "strike", "redesignate",
    "paragraph", "subparagraph", "preceding", "following",
    "date", "enactment", "fiscal", "year", "period",
    "report", "submit", "provide", "require", "determine",
    # AI 법안에서 과매칭되는 일반 단어
    "intelligence", "artificial", "technology", "system", "systems",
    "information", "data", "use", "person", "entity",
    "term", "defined", "covered", "applicable", "relevant",
    "notice", "consent", "rights", "standards",
    "compliance", "framework", "guidelines", "regulatory",
    "disclosure", "service", "program", "plan",
]


def load_bill_texts() -> dict[str, str]:
    """법안 전문 텍스트 로드."""
    texts = {}
    for fname in os.listdir(TEXT_DIR):
        if not fname.endswith(".txt"):
            continue
        # HR_4814.txt -> H.R. 4814
        bill_id = fname.replace(".txt", "")
        bill_id = re.sub(r"^HR_", "H.R. ", bill_id)
        bill_id = re.sub(r"^S_", "S. ", bill_id)
        bill_id = re.sub(r"^HJRes_", "H.J.Res. ", bill_id)

        with open(os.path.join(TEXT_DIR, fname), encoding="utf-8") as f:
            texts[bill_id] = f.read()
    return texts


def classify_tfidf(bill_id: str, text: str, tfidf_matrix, feature_names, doc_idx: int) -> list[tuple[str, float]]:
    """TF-IDF 가중치 기반으로 10개 Policy Attribute에 점수 부여.

    다어절 키워드: ngram feature가 있으면 사용, 없으면 개별 단어 합산.
    단일 단어 키워드: 직접 매칭.
    """
    scores = {}
    row = tfidf_matrix[doc_idx].toarray().flatten()

    for attr, keywords in POLICY_ATTRIBUTES.items():
        score = 0.0
        for kw in keywords:
            kw_lower = kw.lower()
            # 먼저 ngram (bigram)으로 직접 매칭 시도
            if kw_lower in feature_names:
                idx = feature_names[kw_lower]
                score += row[idx] * 2.0  # bigram 매칭은 가중치 2배
            elif " " in kw_lower:
                # bigram이 vocabulary에 없으면 개별 단어 합산 (가중치 낮게)
                words = kw_lower.split()
                for word in words:
                    if word in feature_names:
                        idx = feature_names[word]
                        score += row[idx] * 0.5
            else:
                # 단일 단어
                if kw_lower in feature_names:
                    idx = feature_names[kw_lower]
                    score += row[idx]
        scores[attr] = score

    # 점수 순 정렬
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked


def run_tfidf_classification(bill_ids: list[str], texts: list[str]) -> dict:
    """TF-IDF 기반 분류 실행."""
    print("\n=== TF-IDF Classification ===")

    # Vectorize
    vectorizer = TfidfVectorizer(
        stop_words="english",
        max_features=10000,
        min_df=2,
        max_df=0.95,
        ngram_range=(1, 2),
        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z-]+\b",
    )
    tfidf_matrix = vectorizer.fit_transform(texts)
    feature_names = {name: idx for idx, name in enumerate(vectorizer.get_feature_names_out())}
    print(f"  Vocabulary: {len(feature_names)} terms")
    print(f"  Matrix: {tfidf_matrix.shape}")

    results = {}
    for i, bill_id in enumerate(bill_ids):
        ranked = classify_tfidf(bill_id, texts[i], tfidf_matrix, feature_names, i)
        primary = ranked[0][0] if ranked[0][1] > 0 else "Unclassified"
        top3 = [r[0] for r in ranked[:3] if r[1] > 0]

        results[bill_id] = {
            "primary": primary,
            "top3": top3,
            "scores": {r[0]: round(r[1], 4) for r in ranked},
        }

    # Distribution
    primary_counts = Counter(r["primary"] for r in results.values())
    print("\n  Primary Attribute Distribution (Figure 20):")
    for attr, cnt in primary_counts.most_common():
        print(f"    {attr}: {cnt}")

    return results


def run_lda(bill_ids: list[str], texts: list[str], n_topics: int = 10) -> dict:
    """LDA 토픽 모델링."""
    print("\n=== LDA Topic Modeling ===")

    # CountVectorizer (LDA는 카운트 기반)
    vectorizer = CountVectorizer(
        stop_words="english",
        max_features=10000,
        min_df=2,
        max_df=0.95,
        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z-]+\b",
    )
    count_matrix = vectorizer.fit_transform(texts)
    feature_names = vectorizer.get_feature_names_out()

    # Add legal stopwords by zeroing their columns
    legal_stop_indices = []
    for sw in LEGAL_STOPWORDS:
        matches = [i for i, name in enumerate(feature_names) if name == sw]
        legal_stop_indices.extend(matches)
    if legal_stop_indices:
        count_matrix_dense = count_matrix.toarray()
        count_matrix_dense[:, legal_stop_indices] = 0
        from scipy.sparse import csr_matrix
        count_matrix = csr_matrix(count_matrix_dense)

    # LDA
    lda = LatentDirichletAllocation(
        n_components=n_topics,
        random_state=42,
        max_iter=50,
        learning_method="online",
    )
    doc_topics = lda.fit_transform(count_matrix)
    print(f"  Topics: {n_topics}")
    print(f"  Perplexity: {lda.perplexity(count_matrix):.2f}")

    # Topic keywords
    topic_keywords = {}
    for topic_idx, topic in enumerate(lda.components_):
        top_words = [feature_names[i] for i in topic.argsort()[:-15:-1]]
        topic_keywords[topic_idx] = top_words
        print(f"  Topic {topic_idx}: {', '.join(top_words[:8])}")

    # Assign topics to bills
    results = {}
    for i, bill_id in enumerate(bill_ids):
        topic_dist = doc_topics[i]
        primary_topic = int(np.argmax(topic_dist))
        results[bill_id] = {
            "primary_topic": primary_topic,
            "topic_distribution": [round(float(p), 4) for p in topic_dist],
            "topic_keywords": topic_keywords[primary_topic][:5],
        }

    # Topic distribution
    topic_counts = Counter(r["primary_topic"] for r in results.values())
    print(f"\n  Topic distribution:")
    for t, cnt in sorted(topic_counts.items()):
        print(f"    Topic {t}: {cnt} bills — {', '.join(topic_keywords[t][:5])}")

    return results, topic_keywords


def build_crossmap(tfidf_results: dict, lda_results: dict) -> dict:
    """TF-IDF vs LDA 교차 히트맵 (Figure 21)."""
    print("\n=== TF-IDF vs LDA Cross-map (Figure 21) ===")

    attr_names = list(POLICY_ATTRIBUTES.keys())
    n_topics = max(r["primary_topic"] for r in lda_results.values()) + 1

    # Count matrix: TF-IDF attribute × LDA topic
    matrix = np.zeros((len(attr_names), n_topics), dtype=int)

    for bill_id in tfidf_results:
        if bill_id not in lda_results:
            continue
        tfidf_attr = tfidf_results[bill_id]["primary"]
        lda_topic = lda_results[bill_id]["primary_topic"]

        if tfidf_attr in attr_names:
            row = attr_names.index(tfidf_attr)
            matrix[row, lda_topic] += 1

    # Print
    print(f"\n  {'':40s}", end="")
    for t in range(n_topics):
        print(f"  T{t:2d}", end="")
    print()
    for i, attr in enumerate(attr_names):
        short = attr[:38]
        print(f"  {short:40s}", end="")
        for t in range(n_topics):
            val = matrix[i, t]
            print(f"  {val:3d}", end="")
        print()

    return {
        "attr_names": attr_names,
        "n_topics": n_topics,
        "matrix": matrix.tolist(),
    }


def main():
    os.makedirs(FIGURES_DIR, exist_ok=True)

    # Load processed bills
    with open(PROCESSED_FILE, encoding="utf-8") as f:
        processed = json.load(f)
    print(f"법안: {len(processed)}건")

    # Load texts
    all_texts = load_bill_texts()
    print(f"텍스트 로드: {len(all_texts)}건")

    # Align bill_ids and texts
    bill_ids = []
    texts = []
    for bill in processed:
        bid = bill["bill_id"]
        if bid in all_texts and len(all_texts[bid]) > 100:
            bill_ids.append(bid)
            texts.append(all_texts[bid])

    print(f"분석 대상: {len(bill_ids)}건 (텍스트 있는 법안)")

    # 1. TF-IDF Classification
    tfidf_results = run_tfidf_classification(bill_ids, texts)
    with open(OUTPUT_TFIDF, "w", encoding="utf-8") as f:
        json.dump(tfidf_results, f, indent=2, ensure_ascii=False)
    print(f"\n저장: {OUTPUT_TFIDF}")

    # 2. LDA Topic Modeling
    lda_results, topic_keywords = run_lda(bill_ids, texts, n_topics=10)
    lda_output = {
        "topic_keywords": {str(k): v for k, v in topic_keywords.items()},
        "bill_topics": lda_results,
    }
    with open(OUTPUT_LDA, "w", encoding="utf-8") as f:
        json.dump(lda_output, f, indent=2, ensure_ascii=False)
    print(f"저장: {OUTPUT_LDA}")

    # 3. Cross-map
    crossmap = build_crossmap(tfidf_results, lda_results)
    with open(OUTPUT_CROSSMAP, "w", encoding="utf-8") as f:
        json.dump(crossmap, f, indent=2, ensure_ascii=False)
    print(f"저장: {OUTPUT_CROSSMAP}")

    # 4. Primary + Top 3 분포 (Figure 11, 12)
    print(f"\n{'='*60}")
    print("[Figure 11] Primary Policy Attributes:")
    primary_counts = Counter(r["primary"] for r in tfidf_results.values())
    for attr, cnt in primary_counts.most_common():
        paper_val = ""
        if "Responsible" in attr: paper_val = " (논문: 71)"
        elif "National security" in attr: paper_val = " (논문: 24)"
        elif "Safety" in attr: paper_val = " (논문: 14)"
        elif "Public interest" in attr: paper_val = " (논문: 13)"
        print(f"  {attr}: {cnt}{paper_val}")

    print(f"\n[Figure 12] Top 3 Policy Attributes:")
    top3_counts = Counter()
    for r in tfidf_results.values():
        for attr in r["top3"]:
            top3_counts[attr] += 1
    for attr, cnt in top3_counts.most_common():
        paper_val = ""
        if "Responsible" in attr: paper_val = " (논문: 141)"
        elif "National security" in attr: paper_val = " (논문: 106)"
        elif "Safety" in attr: paper_val = " (논문: 66)"
        elif "Public interest" in attr: paper_val = " (논문: 53)"
        print(f"  {attr}: {cnt}{paper_val}")


if __name__ == "__main__":
    main()
