"""한국 국회 Step 3: NLP 분류 — kiwipiepy TF-IDF + LDA + GPT 검증.

미국 replication과 동일한 3단계 파이프라인을 한국어에 적용:
1. TF-IDF (kiwipiepy 명사 추출) → 10개 Policy Attribute
2. LDA 비지도 토픽 모델링
3. GPT-4o-mini 검증

Usage:
    python replicate_carvao/kr_03_nlp_classify.py
"""
import json
import os
import re
import sys
import time
from collections import Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
from kiwipiepy import Kiwi
from sklearn.feature_extraction.text import TfidfVectorizer, CountVectorizer
from sklearn.decomposition import LatentDirichletAllocation
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
TEXT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "bill_txt_22")
PROCESSED_FILE = os.path.join(DATA_DIR, "kr_bills_processed.json")
OUTPUT_TFIDF = os.path.join(DATA_DIR, "kr_tfidf_classification.json")
OUTPUT_LDA = os.path.join(DATA_DIR, "kr_lda_topics.json")
OUTPUT_CROSSMAP = os.path.join(DATA_DIR, "kr_tfidf_lda_crossmap.json")
OUTPUT_GPT = os.path.join(DATA_DIR, "kr_gpt_classification.json")
FIGURES_DIR = os.path.join(os.path.dirname(__file__), "figures")

kiwi = Kiwi()
client = OpenAI()

# ─── 한국어 Policy Attributes + 키워드 ───
# Carvão 10개 카테고리를 한국 맥락에 적응

POLICY_ATTRIBUTES = {
    "시장경쟁/독과점 (Market efficiency)": [
        "공정거래", "독점", "독과점", "시장지배", "불공정",
        "플랫폼", "경쟁", "공정위", "공정거래위원회",
        "가격", "담합", "카르텔", "시장", "소비자기만",
    ],
    "AI안전 (Safety)": [
        "안전", "안전성", "위험", "위해", "사고", "피해",
        "검증", "평가", "테스트", "취약", "오류",
        "워터마크", "고위험", "신고", "사전평가",
    ],
    "책임/윤리AI (Responsible AI)": [
        "책임", "윤리", "투명", "투명성", "편향", "차별",
        "공정", "신뢰", "감독", "설명가능", "설명의무",
        "알고리즘", "자동화", "자동결정", "영향평가",
        "감사", "인증",
    ],
    "국가안보 (National security)": [
        "국방", "군사", "안보", "사이버보안", "사이버공격",
        "방위", "국가정보원", "테러", "북한",
        "정보기관", "첩보", "무기", "자율무기",
    ],
    "산업정책 (Industrial policy)": [
        "산업", "육성", "진흥", "연구개발", "투자",
        "반도체", "데이터센터", "인프라", "스타트업", "혁신",
        "과학기술", "특구", "지원", "기금", "인력양성",
    ],
    "공익/소비자보호 (Public interest)": [
        "소비자", "보호", "건강", "의료", "환자",
        "교육", "학생", "아동", "청소년", "장애",
        "환경", "에너지", "복지", "공공",
        "농업", "교통",
    ],
    "노동/고용 (Labor)": [
        "노동", "고용", "근로", "근로자", "자동화",
        "일자리", "재교육", "직업훈련", "임금",
        "플랫폼노동", "플랫폼종사자", "해고",
    ],
    "저작권/지식재산 (Copyright)": [
        "저작권", "지식재산", "지적재산", "창작", "저작물",
        "생성물", "학습데이터", "초상", "퍼블리시티",
        "라이선스", "콘텐츠", "웹툰",
    ],
    "국제협력 (International collaboration)": [
        "국제", "협력", "조약", "표준", "글로벌",
        "다자", "양자", "수출", "통상",
    ],
    "선거/민주주의 (Elections)": [
        "선거", "후보", "투표", "딥페이크", "허위정보",
        "가짜뉴스", "여론조작", "정치광고", "공직선거",
        "허위사실", "조작",
    ],
}

# 한국어 법률 불용어
LEGAL_STOPWORDS = [
    "법률", "법안", "조", "항", "호", "목", "관", "대통령",
    "국회", "위원회", "장관", "기관", "정부", "행정",
    "규정", "시행", "시행령", "고시", "개정", "신설",
    "삭제", "전문", "다음", "경우", "필요", "사항",
    "관련", "조치", "대한", "이상", "이하", "해당",
    "바에", "따라", "의한", "위하여", "관하여",
    "일부", "전부", "부칙", "제출", "발의",
    # AI 법안에서 과매칭되는 일반 단어
    "인공지능", "기술", "시스템", "정보", "이용",
    "서비스", "제공", "활용", "기반", "촉진",
    "지원", "개발", "관리", "운영", "설치",
]


def extract_nouns(text: str) -> str:
    """kiwipiepy로 명사 추출 → 공백 구분 문자열."""
    tokens = kiwi.tokenize(text)
    nouns = [t.form for t in tokens
             if t.tag in ("NNG", "NNP") and len(t.form) >= 2
             and t.form not in LEGAL_STOPWORDS]
    return " ".join(nouns)


def load_bill_texts(bill_ids: set) -> dict[str, str]:
    """법안 텍스트 로드 (reason_and_content)."""
    texts = {}
    for fname in os.listdir(TEXT_DIR):
        if not fname.endswith(".json"):
            continue
        bid = fname.replace(".json", "")
        if bid not in bill_ids:
            continue
        with open(os.path.join(TEXT_DIR, fname), encoding="utf-8") as f:
            bill = json.load(f)
        reason = bill.get("reason_and_content", "") or bill.get("full_text", "") or ""
        if len(reason) > 100:
            texts[bid] = reason
    return texts


def classify_tfidf(tfidf_matrix, feature_names, doc_idx: int) -> list[tuple[str, float]]:
    """TF-IDF 기반 Policy Attribute 점수."""
    scores = {}
    row = tfidf_matrix[doc_idx].toarray().flatten()

    for attr, keywords in POLICY_ATTRIBUTES.items():
        score = 0.0
        for kw in keywords:
            if kw in feature_names:
                idx = feature_names[kw]
                score += row[idx]
        scores[attr] = score

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def run_tfidf(bill_ids, tokenized_texts):
    """TF-IDF 분류."""
    print("\n=== TF-IDF Classification (Korean) ===")

    vectorizer = TfidfVectorizer(
        max_features=5000,
        min_df=2,
        max_df=0.90,
    )
    tfidf_matrix = vectorizer.fit_transform(tokenized_texts)
    feature_names = {name: idx for idx, name in enumerate(vectorizer.get_feature_names_out())}
    print(f"  Vocabulary: {len(feature_names)} terms")

    results = {}
    for i, bid in enumerate(bill_ids):
        ranked = classify_tfidf(tfidf_matrix, feature_names, i)
        primary = ranked[0][0] if ranked[0][1] > 0 else "미분류"
        top3 = [r[0] for r in ranked[:3] if r[1] > 0]
        results[bid] = {
            "primary": primary,
            "top3": top3,
            "scores": {r[0]: round(r[1], 4) for r in ranked},
        }

    primary_counts = Counter(r["primary"] for r in results.values())
    print("\n  Primary Distribution:")
    for attr, cnt in primary_counts.most_common():
        print(f"    {attr}: {cnt}")

    return results


def run_lda(bill_ids, tokenized_texts, n_topics=10):
    """LDA 토픽 모델링."""
    print("\n=== LDA Topic Modeling (Korean) ===")

    vectorizer = CountVectorizer(max_features=5000, min_df=2, max_df=0.90)
    count_matrix = vectorizer.fit_transform(tokenized_texts)
    feature_names = vectorizer.get_feature_names_out()

    lda = LatentDirichletAllocation(
        n_components=n_topics, random_state=42,
        max_iter=50, learning_method="online",
    )
    doc_topics = lda.fit_transform(count_matrix)

    topic_keywords = {}
    for topic_idx, topic in enumerate(lda.components_):
        top_words = [feature_names[i] for i in topic.argsort()[:-11:-1]]
        topic_keywords[topic_idx] = top_words
        print(f"  Topic {topic_idx}: {', '.join(top_words[:6])}")

    results = {}
    for i, bid in enumerate(bill_ids):
        primary_topic = int(np.argmax(doc_topics[i]))
        results[bid] = {
            "primary_topic": primary_topic,
            "topic_distribution": [round(float(p), 4) for p in doc_topics[i]],
            "topic_keywords": topic_keywords[primary_topic][:5],
        }

    topic_counts = Counter(r["primary_topic"] for r in results.values())
    print(f"\n  Topic distribution:")
    for t, cnt in sorted(topic_counts.items()):
        print(f"    Topic {t}: {cnt}건 — {', '.join(topic_keywords[t][:5])}")

    return results, topic_keywords


def build_crossmap(tfidf_results, lda_results):
    """TF-IDF vs LDA 교차 히트맵."""
    attr_names = list(POLICY_ATTRIBUTES.keys())
    n_topics = max(r["primary_topic"] for r in lda_results.values()) + 1
    matrix = np.zeros((len(attr_names), n_topics), dtype=int)

    for bid in tfidf_results:
        if bid not in lda_results:
            continue
        tfidf_attr = tfidf_results[bid]["primary"]
        lda_topic = lda_results[bid]["primary_topic"]
        if tfidf_attr in attr_names:
            row = attr_names.index(tfidf_attr)
            matrix[row, lda_topic] += 1

    return {"attr_names": attr_names, "n_topics": n_topics, "matrix": matrix.tolist()}


def run_gpt_validation(bill_ids, raw_texts, titles):
    """GPT-4o-mini 검증."""
    print(f"\n=== GPT Validation ({len(bill_ids)}건) ===")

    attr_list = "\n".join(f"{i+1}. {a}" for i, a in enumerate(POLICY_ATTRIBUTES.keys()))
    system_prompt = f"""You are an expert Korean legislative analyst.
Classify the following Korean AI bill into the policy attributes below.
Assign exactly 3 attributes ranked by relevance (primary, secondary, tertiary).

Policy Attributes:
{attr_list}

Respond ONLY with valid JSON:
{{"primary": "attribute name in Korean", "secondary": "attribute name", "tertiary": "attribute name"}}"""

    if os.path.exists(OUTPUT_GPT):
        with open(OUTPUT_GPT, encoding="utf-8") as f:
            results = json.load(f)
        print(f"  기존: {len(results)}건")
    else:
        results = {}

    new = 0
    for i, bid in enumerate(bill_ids, 1):
        if bid in results:
            continue

        title = titles.get(bid, "")
        text = raw_texts.get(bid, "")[:3000]
        user_msg = f"법안 제목: {title}\n\n제안이유 및 주요내용:\n{text}"

        print(f"  [{i}/{len(bill_ids)}] {title[:35]}...", end=" ", flush=True)

        try:
            response = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            results[bid] = result
            new += 1
            print(f"{result.get('primary', '?')[:20]}")
        except Exception as e:
            print(f"ERROR: {e}")
            results[bid] = {"primary": "미분류", "secondary": "미분류", "tertiary": "미분류"}

        time.sleep(0.3)
        if new % 30 == 0 and new > 0:
            with open(OUTPUT_GPT, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)

    with open(OUTPUT_GPT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    return results


def main():
    os.makedirs(FIGURES_DIR, exist_ok=True)

    with open(PROCESSED_FILE, encoding="utf-8") as f:
        processed = json.load(f)
    print(f"법안: {len(processed)}건")

    bill_id_set = {b["bill_id"] for b in processed}
    titles = {b["bill_id"]: b["bill_name"] for b in processed}

    # Load raw texts
    raw_texts = load_bill_texts(bill_id_set)
    print(f"텍스트 로드: {len(raw_texts)}건")

    # Tokenize with kiwipiepy
    print("명사 추출 중...")
    bill_ids = []
    tokenized = []
    for b in processed:
        bid = b["bill_id"]
        if bid in raw_texts:
            bill_ids.append(bid)
            tokenized.append(extract_nouns(raw_texts[bid]))

    print(f"분석 대상: {len(bill_ids)}건")

    # 1. TF-IDF
    tfidf_results = run_tfidf(bill_ids, tokenized)
    with open(OUTPUT_TFIDF, "w", encoding="utf-8") as f:
        json.dump(tfidf_results, f, indent=2, ensure_ascii=False)

    # 2. LDA
    lda_results, topic_keywords = run_lda(bill_ids, tokenized, n_topics=10)
    with open(OUTPUT_LDA, "w", encoding="utf-8") as f:
        json.dump({"topic_keywords": {str(k): v for k, v in topic_keywords.items()},
                    "bill_topics": lda_results}, f, indent=2, ensure_ascii=False)

    # 3. Cross-map
    crossmap = build_crossmap(tfidf_results, lda_results)
    with open(OUTPUT_CROSSMAP, "w", encoding="utf-8") as f:
        json.dump(crossmap, f, indent=2, ensure_ascii=False)

    # 4. GPT Validation
    gpt_results = run_gpt_validation(bill_ids, raw_texts, titles)

    # ─── 결과 비교 ───
    print(f"\n{'='*60}")
    print("[GPT] Primary Distribution:")
    gpt_primary = Counter(r.get("primary", "미분류") for r in gpt_results.values())
    for attr, cnt in gpt_primary.most_common():
        print(f"  {attr}: {cnt}")

    print(f"\n[TF-IDF vs GPT 일치도]")
    agree = disagree = 0
    for bid in bill_ids:
        if bid in gpt_results and bid in tfidf_results:
            if gpt_results[bid].get("primary") == tfidf_results[bid]["primary"]:
                agree += 1
            else:
                disagree += 1
    total = agree + disagree
    if total:
        print(f"  일치: {agree}/{total} ({agree/total*100:.1f}%)")
        print(f"  불일치: {disagree}/{total} ({disagree/total*100:.1f}%)")


if __name__ == "__main__":
    main()
