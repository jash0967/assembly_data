"""TF-IDF + LDA 검증: 새 데이터(21대 49건 + 22대 185건) 기준.

GPT 분류 결과와 교차 검증하여 방법론적 견고성을 확인.
"""
import json
import os
import sys
import re
from collections import Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
from kiwipiepy import Kiwi
from sklearn.feature_extraction.text import TfidfVectorizer, CountVectorizer
from sklearn.decomposition import LatentDirichletAllocation

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SHARED_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
TEXT_DIR_21 = os.path.join(SHARED_DATA_DIR, "bill_txt_21")
TEXT_DIR_22 = os.path.join(SHARED_DATA_DIR, "bill_txt_22")

kiwi = Kiwi()

# ─── 한국어 Policy Attributes 키워드 사전 ───
POLICY_ATTRIBUTES = {
    "산업정책": [
        "산업", "육성", "진흥", "연구개발", "투자",
        "반도체", "데이터센터", "인프라", "스타트업", "혁신",
        "과학기술", "특구", "지원", "기금", "인력양성",
        "양성", "인재", "클라우드", "컴퓨팅",
    ],
    "공익/소비자보호": [
        "소비자", "보호", "건강", "의료", "환자",
        "교육", "학생", "아동", "청소년", "장애",
        "환경", "에너지", "복지", "공공",
        "농업", "교통", "허위광고", "피해",
    ],
    "AI안전": [
        "안전", "안전성", "위험", "위해", "사고", "피해",
        "검증", "평가", "테스트", "취약", "오류",
        "워터마크", "고위험", "신고", "사전평가",
    ],
    "책임/윤리AI": [
        "책임", "윤리", "투명", "투명성", "편향", "차별",
        "공정", "신뢰", "감독", "설명가능", "설명의무",
        "알고리즘", "자동화", "자동결정", "영향평가",
        "감사", "인증",
    ],
    "선거/민주주의": [
        "선거", "후보", "투표", "딥페이크", "허위정보",
        "가짜뉴스", "여론조작", "정치광고", "공직선거",
        "허위사실", "조작",
    ],
    "저작권/지식재산": [
        "저작권", "지식재산", "지적재산", "창작", "저작물",
        "생성물", "학습데이터", "초상", "퍼블리시티",
        "라이선스", "콘텐츠", "웹툰",
    ],
    "노동/고용": [
        "노동", "고용", "근로", "근로자", "자동화",
        "일자리", "재교육", "직업훈련", "임금",
        "플랫폼노동", "플랫폼종사자", "해고", "채용",
    ],
    "국가안보": [
        "국방", "군사", "안보", "사이버보안", "사이버공격",
        "방위", "국가정보원", "테러", "북한",
        "정보기관", "첩보", "무기", "자율무기",
    ],
    "국제협력": [
        "국제", "협력", "조약", "표준", "글로벌",
        "다자", "양자", "수출", "통상",
    ],
    "시장경쟁/독과점": [
        "공정거래", "독점", "독과점", "시장지배", "불공정",
        "플랫폼", "경쟁", "공정위", "공정거래위원회",
        "가격", "담합", "카르텔", "시장", "소비자기만",
    ],
}

LEGAL_STOPWORDS = [
    "법률", "법안", "조", "항", "호", "목", "관", "대통령",
    "국회", "위원회", "장관", "기관", "정부", "행정",
    "규정", "시행", "시행령", "고시", "개정", "신설",
    "삭제", "전문", "다음", "경우", "필요", "사항",
    "관련", "조치", "대한", "이상", "이하", "해당",
    "바에", "따라", "의한", "위하여", "관하여",
    "일부", "전부", "부칙", "제출", "발의",
    "인공지능", "기술", "시스템", "정보", "이용",
    "서비스", "제공", "활용", "기반", "촉진",
    "지원", "개발", "관리", "운영", "설치",
]


def extract_nouns(text):
    tokens = kiwi.tokenize(text)
    nouns = [t.form for t in tokens
             if t.tag in ("NNG", "NNP") and len(t.form) >= 2
             and t.form not in LEGAL_STOPWORDS]
    return " ".join(nouns)


def load_all_bills():
    """21대 + 22대 AI 법안 텍스트 로드."""
    bills = []
    for age, bills_file, text_dir in [
        (21, os.path.join(DATA_DIR, "kr21_ai_bills.json"), TEXT_DIR_21),
        (22, os.path.join(DATA_DIR, "kr22_ai_bills.json"), TEXT_DIR_22),
    ]:
        with open(bills_file, encoding="utf-8") as f:
            bill_list = json.load(f)
        for b in bill_list:
            bid = b["bill_id"]
            txt_path = os.path.join(text_dir, f"{bid}.json")
            if os.path.exists(txt_path):
                with open(txt_path, encoding="utf-8") as f:
                    raw = json.load(f)
                reason = raw.get("reason_and_content", "") or raw.get("full_text", "") or ""
                if len(reason) > 100:
                    bills.append({
                        "bill_id": bid,
                        "bill_name": b.get("bill_name", ""),
                        "age": age,
                        "text": reason,
                    })
    return bills


def classify_tfidf(tfidf_matrix, feature_names, doc_idx):
    scores = {}
    row = tfidf_matrix[doc_idx].toarray().flatten()
    for attr, keywords in POLICY_ATTRIBUTES.items():
        score = 0.0
        for kw in keywords:
            if kw in feature_names:
                score += row[feature_names[kw]]
        scores[attr] = score
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def main():
    print("=== TF-IDF + LDA 검증 (21대 + 22대) ===\n")

    # 1. 데이터 로드
    bills = load_all_bills()
    print(f"법안 텍스트 로드: {len(bills)}건")

    # 2. 명사 추출
    print("kiwipiepy 명사 추출 중...")
    bill_ids = [b["bill_id"] for b in bills]
    bill_names = {b["bill_id"]: b["bill_name"] for b in bills}
    bill_ages = {b["bill_id"]: b["age"] for b in bills}
    tokenized = [extract_nouns(b["text"]) for b in bills]
    print(f"토큰화 완료: {len(tokenized)}건\n")

    # ═══════════════════════════════════════
    # 3. TF-IDF 분류
    # ═══════════════════════════════════════
    print("=== TF-IDF Classification ===")
    vectorizer = TfidfVectorizer(max_features=5000, min_df=2, max_df=0.90)
    tfidf_matrix = vectorizer.fit_transform(tokenized)
    feature_names = {name: idx for idx, name in enumerate(vectorizer.get_feature_names_out())}
    print(f"  Vocabulary: {len(feature_names)} terms")

    tfidf_results = {}
    for i, bid in enumerate(bill_ids):
        ranked = classify_tfidf(tfidf_matrix, feature_names, i)
        primary = ranked[0][0] if ranked[0][1] > 0 else "미분류"
        top3 = [r[0] for r in ranked[:3] if r[1] > 0]
        tfidf_results[bid] = {"primary": primary, "top3": top3}

    tfidf_dist = Counter(r["primary"] for r in tfidf_results.values())
    print("\n  TF-IDF Primary Distribution:")
    for attr, cnt in tfidf_dist.most_common():
        print(f"    {attr}: {cnt}")

    # ═══════════════════════════════════════
    # 4. LDA 토픽 모델링
    # ═══════════════════════════════════════
    print("\n=== LDA Topic Modeling (10 topics) ===")
    count_vec = CountVectorizer(max_features=5000, min_df=2, max_df=0.90)
    count_matrix = count_vec.fit_transform(tokenized)
    lda_features = count_vec.get_feature_names_out()

    lda = LatentDirichletAllocation(
        n_components=10, random_state=42, max_iter=50, learning_method="online"
    )
    doc_topics = lda.fit_transform(count_matrix)

    topic_keywords = {}
    for t_idx, topic in enumerate(lda.components_):
        top_words = [lda_features[i] for i in topic.argsort()[:-11:-1]]
        topic_keywords[t_idx] = top_words
        print(f"  Topic {t_idx}: {', '.join(top_words[:6])}")

    lda_results = {}
    for i, bid in enumerate(bill_ids):
        primary_topic = int(np.argmax(doc_topics[i]))
        lda_results[bid] = {"primary_topic": primary_topic}

    lda_topic_dist = Counter(r["primary_topic"] for r in lda_results.values())
    print(f"\n  LDA Topic Distribution:")
    for t, cnt in sorted(lda_topic_dist.items()):
        print(f"    Topic {t}: {cnt}건 — {', '.join(topic_keywords[t][:5])}")

    # ═══════════════════════════════════════
    # 5. GPT 분류 결과 로드 (bill_loaders 사용)
    # ═══════════════════════════════════════
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from bill_loaders import load_kr_bills
    gpt_results = {b["id"]: b for b in load_kr_bills(ages=(21, 22), enrich=False)}

    # ═══════════════════════════════════════
    # 6. TF-IDF vs GPT 비교
    # ═══════════════════════════════════════
    print("\n=== TF-IDF vs GPT 일치도 ===")
    agree = disagree = 0
    disagree_examples = []
    for bid in bill_ids:
        if bid in gpt_results and bid in tfidf_results:
            gpt_primary = gpt_results[bid].get("primary", "")
            tfidf_primary = tfidf_results[bid]["primary"]
            if gpt_primary == tfidf_primary:
                agree += 1
            else:
                disagree += 1
                if len(disagree_examples) < 10:
                    disagree_examples.append({
                        "bill": bill_names.get(bid, "")[:40],
                        "tfidf": tfidf_primary,
                        "gpt": gpt_primary,
                    })

    total = agree + disagree
    print(f"  일치: {agree}/{total} ({agree/total*100:.1f}%)")
    print(f"  불일치: {disagree}/{total} ({disagree/total*100:.1f}%)")
    print(f"\n  불일치 예시:")
    for ex in disagree_examples:
        print(f"    {ex['bill']} — TF-IDF:{ex['tfidf']} vs GPT:{ex['gpt']}")

    # ═══════════════════════════════════════
    # 7. TF-IDF vs LDA 히트맵 데이터
    # ═══════════════════════════════════════
    print("\n=== TF-IDF vs LDA Cross-map ===")
    attr_names = list(POLICY_ATTRIBUTES.keys())
    matrix = np.zeros((len(attr_names), 10), dtype=int)
    for bid in bill_ids:
        if bid in tfidf_results and bid in lda_results:
            tfidf_attr = tfidf_results[bid]["primary"]
            lda_topic = lda_results[bid]["primary_topic"]
            if tfidf_attr in attr_names:
                row = attr_names.index(tfidf_attr)
                matrix[row, lda_topic] += 1

    print(f"  {'':20s}", end="")
    for t in range(10):
        print(f"  T{t:d}", end="")
    print()
    for i, attr in enumerate(attr_names):
        print(f"  {attr:20s}", end="")
        for t in range(10):
            print(f"  {matrix[i,t]:2d}", end="")
        print()

    # ═══════════════════════════════════════
    # 8. GPT vs LDA 대응
    # ═══════════════════════════════════════
    print("\n=== GPT 속성 vs LDA 토픽 대응 ===")
    gpt_lda_matrix = np.zeros((len(attr_names), 10), dtype=int)
    for bid in bill_ids:
        if bid in gpt_results and bid in lda_results:
            gpt_attr = gpt_results[bid].get("primary", "")
            lda_topic = lda_results[bid]["primary_topic"]
            if gpt_attr in attr_names:
                row = attr_names.index(gpt_attr)
                gpt_lda_matrix[row, lda_topic] += 1

    print(f"  {'':20s}", end="")
    for t in range(10):
        print(f"  T{t:d}", end="")
    print()
    for i, attr in enumerate(attr_names):
        print(f"  {attr:20s}", end="")
        for t in range(10):
            print(f"  {gpt_lda_matrix[i,t]:2d}", end="")
        print()

    # ═══════════════════════════════════════
    # 9. 결과 저장
    # ═══════════════════════════════════════
    output = {
        "tfidf_distribution": dict(tfidf_dist.most_common()),
        "tfidf_vs_gpt_agreement": {"agree": agree, "disagree": disagree, "total": total, "rate": round(agree/total*100, 1)},
        "lda_topic_keywords": {str(k): v for k, v in topic_keywords.items()},
        "tfidf_lda_crossmap": matrix.tolist(),
        "gpt_lda_crossmap": gpt_lda_matrix.tolist(),
        "attr_names": attr_names,
        "tfidf_results": {bid: tfidf_results[bid] for bid in bill_ids},
        "lda_results": {bid: lda_results[bid] for bid in bill_ids},
    }

    out_path = os.path.join(DATA_DIR, "kr_tfidf_lda_validation.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n저장: {out_path}")


if __name__ == "__main__":
    main()
