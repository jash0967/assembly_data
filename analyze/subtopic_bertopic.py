"""BERTopic 소주제 추출 — 언어 분리 + 의미 정렬(cross-lingual alignment).

EN/KO 토픽 센트로이드 코사인 유사도로 대응 토픽 병합.
결과: 병합 토픽 / EN 고유 토픽 / KO 고유 토픽.

Usage:
    python subtopic_bertopic.py --attr 책임/윤리AI
    python subtopic_bertopic.py
"""
import json, os, sys, time, html as html_mod, re, argparse
from collections import Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import duckdb
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from sentence_transformers import SentenceTransformer
from bertopic import BERTopic
from bertopic.representation import KeyBERTInspired
from umap import UMAP
from hdbscan import HDBSCAN
from sklearn.feature_extraction.text import CountVectorizer
from openai import OpenAI
from dotenv import load_dotenv
from kiwipiepy import Kiwi

load_dotenv()

import _bootstrap  # noqa: F401
import config
from news_cleaning import STRICT_WHERE, CLEANED_CONTENT_SQL  # type: ignore[import-not-found]

KR_PROMPT_VERSION = "v2_en_20260418"
KR_BODY_CHAR_CAP  = 1500


# ─── Kiwi 토크나이저 (KO 명사 추출, 조사 결합형 해소) ───
_kiwi = Kiwi()
# 데이터 기반: Kiwi 기본 사전이 분리하는 핵심 어휘만 등록
KIWI_USER_WORDS = [
    # AI 핵심어
    "인공지능", "딥페이크", "자율주행", "데이터센터", "빅테크",
    "안면인식", "생성형", "머신러닝", "딥러닝",
    # AI 정책·범죄 키워드
    "딥페이크 성범죄", "디지털 성범죄", "가짜뉴스",
    "전세사기", "유심해킹", "개인정보",
    # 혼합 표기
    "챗GPT", "SK하이닉스",
    # 기타
    "G7", "정상회담",
]
for _w in KIWI_USER_WORDS:
    _kiwi.add_user_word(_w, "NNP", 10.0)

KO_NOUN_TAGS = {"NNG", "NNP", "SL"}   # 일반명사·고유명사·외래어
_PUNCT_TAIL = re.compile(r"[^A-Za-z가-힣\d]+$")

def ko_tokenizer(text):
    out = []
    for t in _kiwi.tokenize(text):
        if t.tag not in KO_NOUN_TAGS:
            continue
        form = _PUNCT_TAIL.sub("", t.form)
        if len(form) >= 2:
            out.append(form)
    return out

OUTPUT   = os.path.join(config.ANALYSIS_DIR, "subtopics_bertopic.json")
client   = OpenAI()

ATTRS_KR = ['AI안전','책임/윤리AI','산업정책','공익/소비자보호','국가안보',
            '선거/민주주의','시장경쟁/독과점','노동/고용','저작권/지식재산','국제협력']
ATTRS_EN = ['Safety','Responsible and ethical AI','Industrial policy',
            'Public interest','National security','Elections',
            'Market efficiency and power concentration (antitrust)',
            'Labor','Copyright','International collaboration']
KR_TO_EN = dict(zip(ATTRS_KR, ATTRS_EN))
NORMALIZE = {v: k for k, v in KR_TO_EN.items()}
NORMALIZE['Market efficiency and power concentration (antitrust)'] = '시장경쟁/독과점'

SBERT_MODEL     = "BAAI/bge-m3"   # 다국어 임베딩 (1024-dim, XLM-R-large 백본)
SBERT_BATCH     = 16              # BGE-M3는 VRAM 더 씀 (MiniLM 64 → 16)
MERGE_THRESHOLD = 0.68            # BGE-M3는 sim 분포가 더 높게 깔려 0.62 → 0.68 재캘리브

BASE_STOPWORDS = [
    'ai','artificial','intelligence','the','and','for','that','with',
    'has','have','are','was','were','will','its','but','not','from',
    'this','can','new','more','also','been','could','would','about',
    'says','said','use','used','using','make','like','just',
    'people','world','company','companies','technology',
    'year','years','one','two','three','first','last',
    '인공지능','기술','이용','활용','관련','위한','대한','통해',
    '있는','하는','것으로','밝혔다','했다','있다','한다','나타났다',
    '지난','최근','또한','이를','이에','이후','가능','확대','강화','추진',
    '제공','기반','에서','으로','까지','부터','라고',
    'quot','amp','lt','gt','nbsp',
]

ATTR_EXTRA_STOPWORDS = {
    '책임/윤리AI':    ['ethical','ethics','ethic','윤리','윤리적','책임',
                       'responsible','responsibility','algorithm','알고리즘',
                       'accountability','trustworthy','신뢰','신뢰할'],
    'AI안전':         ['safety','safe','안전','안전성','risk','위험','위해',
                       'harm','harmful','dangerous','danger'],
    '산업정책':       ['policy','industrial','산업','정책','지원','육성','진흥',
                       'industry','development','개발','혁신','innovation'],
    '공익/소비자보호':['consumer','public','소비자','공익','보호','피해',
                       'protection','protect','welfare'],
    '국가안보':       ['security','national','안보','국가','보안','국방',
                       'defense','defence','threat','위협'],
    '선거/민주주의':  ['election','democracy','선거','민주주의','민주',
                       'democratic','vote','voter','voting','투표'],
    '시장경쟁/독과점':['market','competition','시장','경쟁','독점',
                       'antitrust','monopoly','competitive'],
    '노동/고용':      ['labor','labour','employment','노동','고용','일자리',
                       'worker','workers','근로','근로자','job','jobs'],
    '저작권/지식재산':['copyright','intellectual','저작권','지식재산','저작물',
                       'property','creator','creative','창작'],
    '국제협력':       ['international','cooperation','국제','협력','협정',
                       'global','bilateral','multilateral','다자','글로벌'],
}

SEED_TOPICS = {
    '책임/윤리AI': [
        ["transparency","explainability","black box","투명성","설명가능"],
        ["facial recognition","bias","discrimination","안면인식","편향","차별"],
        ["governance","oversight","regulation","거버넌스","감독","규제"],
        ["hiring","recruitment","채용","면접","공정"],
        ["chatbot","conversational","챗봇","대화형"],
        ["audit","impact assessment","영향평가","감사"],
        ["education","school","student","교육","학교","학생"],
        ["corporate","company","기업","회사","자체규제"],
    ],
    'AI안전': [
        ["deepfake","synthetic","fake","딥페이크","가짜"],
        ["autonomous","self-driving","vehicle","자율주행","사고"],
        ["cybersecurity","hacking","vulnerability","사이버","보안"],
        ["medical","healthcare","diagnosis","의료","오진"],
        ["child","children","아동","청소년"],
    ],
    '산업정책': [
        ["chip","semiconductor","nvidia","반도체","칩"],
        ["datacenter","infrastructure","cloud","데이터센터","인프라"],
        ["startup","investment","funding","스타트업","투자"],
        ["healthcare","biotech","drug","바이오","신약"],
        ["energy","power","grid","에너지","전력"],
    ],
    '공익/소비자보호': [
        ["privacy","data","personal","개인정보","프라이버시"],
        ["health","patient","medical","건강","환자"],
        ["education","school","student","교육","학생"],
        ["fraud","scam","허위광고","사기"],
        ["disinformation","misinformation","허위정보"],
    ],
    '국가안보': [
        ["china","chinese","beijing","중국","미중"],
        ["military","defense","pentagon","군사","국방"],
        ["cyber","attack","espionage","사이버","해킹"],
        ["surveillance","intelligence","spy","감시","정보"],
    ],
    '선거/민주주의': [
        ["deepfake","political","campaign","딥페이크","정치광고"],
        ["manipulation","interference","조작","개입"],
        ["misinformation","disinformation","허위정보","가짜뉴스"],
    ],
    '시장경쟁/독과점': [
        ["antitrust","monopoly","독점","독과점"],
        ["platform","gatekeeper","플랫폼","시장지배"],
        ["merger","acquisition","big tech","인수","빅테크"],
    ],
    '노동/고용': [
        ["automation","displacement","자동화","일자리 감소"],
        ["gig","freelance","platform work","플랫폼노동"],
        ["reskilling","training","재교육","전환"],
    ],
    '저작권/지식재산': [
        ["music","art","creative","음악","창작"],
        ["training data","scraping","fair use","학습데이터","크롤링"],
        ["likeness","voice","deepfake","초상","퍼블리시티"],
    ],
    '국제협력': [
        ["treaty","agreement","조약","협정"],
        ["standard","harmonization","표준","호환"],
        ["united nations","oecd","g7","유엔","다자"],
    ],
}


# ──────────────────────────────────────────────
# 데이터 로드
# ──────────────────────────────────────────────
def clean_text(t):
    t = html_mod.unescape(t)
    t = re.sub(r'<[^>]+>','',t)
    return re.sub(r'\s+',' ',t).strip()

def load_news():
    with open(os.path.join(config.NEWS_DIR,"guardian_articles_raw.json"),encoding="utf-8") as f:
        g_raw = json.load(f)
    g_map = {}
    for a in g_raw:
        title = a.get("title","") or a.get("webTitle","")
        trail = a.get("trail_text","") or (a.get("fields",{}) or {}).get("trailText","") or ""
        text = clean_text(f"{title}. {trail}")
        for key in ["id","url"]:
            aid = a.get(key,"")
            if aid:
                g_map[aid] = text
                if key=="id": g_map[f"https://www.theguardian.com/{aid}"] = text

    with open(os.path.join(config.NEWS_DIR,"nyt_articles_raw.json"),encoding="utf-8") as f:
        n_raw = json.load(f)
    n_map = {}
    for a in n_raw:
        title = a.get("title","")
        if not title:
            h = a.get("headline",{}); title = h.get("main","") if isinstance(h,dict) else str(h)
        abstract = a.get("abstract","") or a.get("lead_paragraph","") or ""
        text = clean_text(f"{title}. {abstract}")
        for key in ["url","web_url","_id","uri"]:
            aid = a.get(key,"")
            if aid: n_map[aid] = text

    articles = []
    for cls_file, source, lang, text_map, normalize in [
        ("articles_classified_guardian.json","guardian","en",g_map,True),
        ("articles_classified_nyt.json","nyt","en",n_map,True),
    ]:
        with open(os.path.join(config.ANALYSIS_DIR,cls_file),encoding="utf-8") as f:
            cls = json.load(f)
        for a in cls:
            attr = a.get("primary","")
            if normalize: attr = NORMALIZE.get(attr,attr)
            if attr not in ATTRS_KR: continue
            text = text_map.get(a.get("article_id",""),"")
            if len(text) > 20:
                articles.append({"text":text,"source":source,"attr":attr,"lang":lang})

    # ── KR domestic news (news.duckdb) ──
    con = duckdb.connect(config.NEWS_DB_PATH, read_only=True)
    con.execute("PRAGMA disable_progress_bar")
    kr_sql = f"""
      SELECT n.title,
             SUBSTR(({CLEANED_CONTENT_SQL}), 1, {KR_BODY_CHAR_CAP}) AS body,
             n.provider,
             c.primary_attr
      FROM news_articles n
      JOIN news_classifications c
        ON c.news_id = n.news_id
       AND c.prompt_version = '{KR_PROMPT_VERSION}'
       AND c.error IS NULL
       AND c.primary_attr IS NOT NULL
       AND c.primary_attr <> 'none'
      WHERE {STRICT_WHERE}
    """
    kr_rows = con.execute(kr_sql).fetchall()
    con.close()
    n_kr_loaded = 0
    n_kr_skipped_label = 0
    for title, body, provider, primary in kr_rows:
        attr = NORMALIZE.get(primary)
        if attr not in ATTRS_KR:
            n_kr_skipped_label += 1
            continue
        text = clean_text(f"{title or ''}. {body or ''}")
        if len(text) > 20:
            articles.append({
                "text":   text,
                "source": provider,
                "attr":   attr,
                "lang":   "ko",
            })
            n_kr_loaded += 1
    print(f"  KR domestic news loaded: {n_kr_loaded:,}건 "
          f"(label miss {n_kr_skipped_label}건)")

    return articles


# ──────────────────────────────────────────────
# 단일 언어 BERTopic 실행 → 임베딩 포함 반환
# ──────────────────────────────────────────────
def run_bertopic_lang(texts, sources, attr, sbert, lang_tag):
    import time as _t
    n = len(texts)
    print(f"    [{lang_tag}] {n}건 임베딩 시작...", flush=True)
    _t0 = _t.time()
    embeddings = sbert.encode(texts, show_progress_bar=True,
                               batch_size=SBERT_BATCH,
                               normalize_embeddings=True,
                               device='cuda')   # (n, 1024)
    print(f"    [{lang_tag}] 임베딩 완료: {_t.time()-_t0:.1f}s, shape={embeddings.shape}", flush=True)

    stopwords = BASE_STOPWORDS + ATTR_EXTRA_STOPWORDS.get(attr, [])
    # BGE-M3 응집 강해 mcs 공식·min_samples·cluster_selection_method 재튜닝 (2026-05-24)
    mcs        = max(8, n // 200)
    n_comp     = 10 if n > 800 else 5
    n_neigh    = 15

    print(f"    min_cluster_size={mcs}, n_comp={n_comp}, n_neigh={n_neigh}")

    if lang_tag == "en":
        vectorizer_model = CountVectorizer(
            stop_words=stopwords, min_df=2, ngram_range=(1, 2),
            token_pattern=r'(?u)\b[a-zA-Z가-힣]{2,}\b')
    else:  # "ko" or "mixed" — Kiwi 명사 추출로 조사 결합형 해소
        vectorizer_model = CountVectorizer(
            tokenizer=ko_tokenizer, lowercase=False,
            stop_words=stopwords, min_df=2, ngram_range=(1, 1))   # 진단: ngram=(1,1)

    topic_model = BERTopic(
        embedding_model=sbert,
        umap_model=UMAP(n_neighbors=n_neigh, n_components=n_comp,
                        min_dist=0.0, metric='cosine', random_state=42),
        hdbscan_model=HDBSCAN(min_cluster_size=mcs, min_samples=1,
                               metric='euclidean', prediction_data=True,
                               cluster_selection_method='leaf'),
        vectorizer_model=vectorizer_model,
        representation_model=KeyBERTInspired(),
        seed_topic_list=SEED_TOPICS.get(attr),
        nr_topics="auto",
        verbose=False,
    )
    topics, _ = topic_model.fit_transform(texts, embeddings)

    n_topics  = len([t for t in set(topics) if t != -1])
    n_out     = sum(1 for t in topics if t == -1)
    print(f"    → 토픽 {n_topics}개, 아웃라이어 {n_out}건 ({n_out/n*100:.0f}%)")

    # 토픽별 데이터 수집
    topics_data = {}
    for tid in sorted(set(topics)):
        if tid == -1: continue
        words   = topic_model.get_topic(tid)
        keywords = [w[0] for w in words[:10] if w[0]]
        indices  = [i for i, t in enumerate(topics) if t == tid]
        src_dist = Counter(sources[i] for i in indices)

        # 대표 문서 (센트로이드 최근접 5건) — 원본 384차원으로 계산
        t_emb    = embeddings[indices]
        centroid = t_emb.mean(axis=0)
        dists    = np.linalg.norm(t_emb - centroid, axis=1)
        rep_idx  = np.argsort(dists)[:5]
        rep_docs = [texts[indices[i]][:200] for i in rep_idx]

        topics_data[tid] = {
            "lang":        lang_tag,
            "keywords":    keywords,
            "count":       len(indices),
            "source_dist": dict(src_dist),
            "centroid":    centroid,          # 정렬에 사용, 저장 전 제거
            "representative_docs": rep_docs,
        }
        print(f"    Topic {tid} ({len(indices)}건): {', '.join(keywords[:5])}")

    return topics_data, embeddings, topics


# ──────────────────────────────────────────────
# 의미 정렬 (cross-lingual alignment)
# ──────────────────────────────────────────────
def align_topics(en_topics, ko_topics, threshold=MERGE_THRESHOLD):
    """EN 토픽과 KO 토픽의 센트로이드 코사인 유사도로 대응 쌍 탐색."""
    if not en_topics or not ko_topics:
        return list(en_topics.values()), list(ko_topics.values()), []

    en_ids = list(en_topics.keys())
    ko_ids = list(ko_topics.keys())

    en_centroids = np.stack([en_topics[i]["centroid"] for i in en_ids])  # (n_en, 384)
    ko_centroids = np.stack([ko_topics[i]["centroid"] for i in ko_ids])  # (n_ko, 384)

    sim = cosine_similarity(en_centroids, ko_centroids)  # (n_en, n_ko)

    print(f"\n  [정렬] 유사도 행렬 ({len(en_ids)}×{len(ko_ids)})")
    print(f"  임계값: {threshold}")

    # 유사도 행렬 출력 (상위 쌍만)
    pairs = []
    for i, eid in enumerate(en_ids):
        for j, kid in enumerate(ko_ids):
            if sim[i, j] >= threshold:
                pairs.append((sim[i, j], eid, kid))
    pairs.sort(reverse=True)

    print(f"\n  임계값 이상 후보 쌍 ({len(pairs)}개):")
    for s, eid, kid in pairs:
        print(f"    EN {eid}({en_topics[eid]['count']}건) ↔ "
              f"KO {kid}({ko_topics[kid]['count']}건)  sim={s:.3f}")
        print(f"      EN: {', '.join(en_topics[eid]['keywords'][:4])}")
        print(f"      KO: {', '.join(ko_topics[kid]['keywords'][:4])}")

    # 탐욕적 매칭: 유사도 높은 순서로 1:1 매칭
    used_en, used_ko = set(), set()
    merged = []

    for s, eid, kid in pairs:
        if eid in used_en or kid in used_ko:
            continue
        en_t = en_topics[eid]
        ko_t = ko_topics[kid]

        # 병합 토픽
        merged_src = dict(en_t["source_dist"])
        for src, cnt in ko_t["source_dist"].items():
            merged_src[src] = merged_src.get(src, 0) + cnt

        # 키워드: EN + KO 각 4개씩
        merged_kw = en_t["keywords"][:4] + [k for k in ko_t["keywords"][:4]
                                             if k not in en_t["keywords"]]

        merged.append({
            "type":       "merged",
            "en_topic_id": eid,
            "ko_topic_id": kid,
            "similarity": round(float(s), 3),
            "keywords":   merged_kw,
            "keywords_en": en_t["keywords"][:6],
            "keywords_ko": ko_t["keywords"][:6],
            "count":      en_t["count"] + ko_t["count"],
            "count_en":   en_t["count"],
            "count_ko":   ko_t["count"],
            "source_dist": merged_src,
            "representative_docs_en": en_t["representative_docs"],
            "representative_docs_ko": ko_t["representative_docs"],
        })
        used_en.add(eid)
        used_ko.add(kid)

    # 매칭 안 된 토픽
    en_only = [dict(en_topics[eid], type="en_only", topic_id=eid)
               for eid in en_ids if eid not in used_en]
    ko_only = [dict(ko_topics[kid], type="ko_only", topic_id=kid)
               for kid in ko_ids if kid not in used_ko]

    print(f"\n  결과: 병합 {len(merged)}쌍 / EN 고유 {len(en_only)}개 / KO 고유 {len(ko_only)}개")
    return en_only, ko_only, merged


# ──────────────────────────────────────────────
# GPT 라벨링
# ──────────────────────────────────────────────
def label_topic(topic, attr_en):
    t_type = topic.get("type", "unknown")

    if t_type == "merged":
        docs_en = "\n".join(f"  EN{i+1}. {d}" for i,d in enumerate(topic.get("representative_docs_en",[])[:3]))
        docs_ko = "\n".join(f"  KO{i+1}. {d}" for i,d in enumerate(topic.get("representative_docs_ko",[])[:3]))
        kw_en   = ", ".join(topic.get("keywords_en",[])[:6])
        kw_ko   = ", ".join(topic.get("keywords_ko",[])[:6])
        prompt  = f"""AI policy topic cluster in "{attr_en}" — matched across English and Korean news.

English keywords: {kw_en}
English articles:
{docs_en}

Korean keywords: {kw_ko}
Korean articles:
{docs_ko}

Provide a single concise subtopic label (3-6 words) covering both.
Respond ONLY with JSON: {{"en": "English label", "ko": "Korean label"}}"""

    elif t_type == "en_only":
        docs = "\n".join(f"  {i+1}. {d}" for i,d in enumerate(topic.get("representative_docs",[])[:4]))
        kw   = ", ".join(topic.get("keywords",[])[:6])
        prompt = f"""AI policy topic cluster in "{attr_en}" — English-language news only.
Keywords: {kw}
Articles:
{docs}
Provide a subtopic label (3-6 words).
Respond ONLY with JSON: {{"en": "English label", "ko": "Korean label"}}"""

    else:  # ko_only
        docs = "\n".join(f"  {i+1}. {d}" for i,d in enumerate(topic.get("representative_docs",[])[:4]))
        kw   = ", ".join(topic.get("keywords",[])[:6])
        prompt = f"""AI policy topic cluster in "{attr_en}" — Korean-language news only.
Keywords: {kw}
Articles:
{docs}
Provide a subtopic label (3-6 words).
Respond ONLY with JSON: {{"en": "English label", "ko": "Korean label"}}"""

    try:
        resp  = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[{"role":"user","content":prompt}],
            temperature=0,
            response_format={"type":"json_object"},
        )
        label = json.loads(resp.choices[0].message.content)
        topic["label_en"] = label.get("en","")
        topic["label_ko"] = label.get("ko","")
    except Exception as e:
        topic["label_en"] = ", ".join(topic.get("keywords",[])[:3])
        topic["label_ko"] = topic["label_en"]
        print(f"    GPT 오류: {e}")
    time.sleep(0.2)
    return topic


# ──────────────────────────────────────────────
# 속성별 전체 파이프라인
# ──────────────────────────────────────────────
def label_topic_keywords_only(topic):
    if topic.get("type") == "merged":
        kw = (topic.get("keywords_en", [])[:2] + topic.get("keywords_ko", [])[:2]) or topic.get("keywords", [])[:4]
    else:
        kw = topic.get("keywords", [])[:3]
    label = ", ".join(kw)
    topic["label_en"] = label
    topic["label_ko"] = label
    return topic


def process_attr(articles, attr, sbert, *, skip_gpt_label=False):
    subset  = [a for a in articles if a["attr"] == attr]
    n_total = len(subset)
    if n_total < 20:
        print(f"[{attr}] {n_total}건 — 스킵")
        return None

    texts   = [a["text"]   for a in subset]
    sources = [a["source"] for a in subset]
    langs   = [a["lang"]   for a in subset]

    n_en = sum(1 for l in langs if l == "en")
    n_ko = sum(1 for l in langs if l == "ko")
    print(f"\n{'='*60}")
    print(f"[{attr}] 총 {n_total}건 — EN:{n_en} / KO:{n_ko}")

    run_separately = (n_en >= 30 and n_ko >= 30)

    if run_separately:
        # ── 언어별 분리 실행 ──
        en_idx  = [i for i,l in enumerate(langs) if l=="en"]
        ko_idx  = [i for i,l in enumerate(langs) if l=="ko"]
        en_texts   = [texts[i]   for i in en_idx]
        en_sources = [sources[i] for i in en_idx]
        ko_texts   = [texts[i]   for i in ko_idx]
        ko_sources = [sources[i] for i in ko_idx]

        en_topics_data, en_emb, en_topics = run_bertopic_lang(en_texts, en_sources, attr, sbert, "en")
        ko_topics_data, ko_emb, ko_topics = run_bertopic_lang(ko_texts, ko_sources, attr, sbert, "ko")

        # ── 의미 정렬 ──
        en_only, ko_only, merged = align_topics(en_topics_data, ko_topics_data)

        all_topics = merged + en_only + ko_only
        n_outliers = (sum(1 for t in en_topics if t==-1) +
                      sum(1 for t in ko_topics if t==-1))

    else:
        # 통합 실행 (언어 분리 불가)
        print("  통합 모드 (언어 분리 불가)")
        topics_data, emb, topics = run_bertopic_lang(texts, sources, attr, sbert, "mixed")
        all_topics = [dict(v, type="mixed", topic_id=k) for k,v in topics_data.items()]
        n_outliers = sum(1 for t in topics if t==-1)
        merged, en_only, ko_only = [], [], all_topics

    # ── 라벨링 ──
    attr_en = KR_TO_EN.get(attr, attr)
    if skip_gpt_label:
        print(f"\n  키워드 라벨링 ({len(all_topics)}개 토픽, GPT 스킵)...")
        for topic in all_topics:
            label_topic_keywords_only(topic)
            t_type = topic.get("type","?")
            sym = "🔗" if t_type=="merged" else ("EN" if t_type=="en_only" else ("KO" if t_type=="ko_only" else "~~"))
            print(f"  [{sym}] ({topic['count']}건) {topic.get('label_ko','')}")
    else:
        print(f"\n  GPT 라벨링 ({len(all_topics)}개 토픽)...")
        for topic in all_topics:
            topic = label_topic(topic, attr_en)
            t_type = topic.get("type","?")
            sym = "🔗" if t_type=="merged" else ("EN" if t_type=="en_only" else ("KO" if t_type=="ko_only" else "~~"))
            print(f"  [{sym}] ({topic['count']}건) {topic.get('label_ko','')} / {topic.get('label_en','')}")

    return {
        "n_articles": n_total,
        "n_topics":   len(all_topics),
        "n_merged":   len(merged),
        "n_en_only":  len(en_only),
        "n_ko_only":  len(ko_only),
        "n_outliers": n_outliers,
        "topics":     all_topics,
    }


# ──────────────────────────────────────────────
# 정리: centroid 제거 후 저장
# ──────────────────────────────────────────────
def clean_for_save(results):
    for attr, data in results.items():
        for t in data["topics"]:
            t.pop("centroid", None)
            t.pop("representative_docs", None)
            t.pop("representative_docs_en", None)
            t.pop("representative_docs_ko", None)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--attr", nargs="+")
    parser.add_argument("--no-label", action="store_true",
                        help="GPT 라벨링 스킵 (키워드 상위 3개를 라벨로 사용)")
    args = parser.parse_args()

    print("=== BERTopic v4: 언어 분리 + 의미 정렬 ===\n")
    print(f"병합 임계값: {MERGE_THRESHOLD}\n")

    articles = load_news()
    dist = Counter(a["attr"] for a in articles)
    print(f"전체: {len(articles)}건")
    for attr, cnt in dist.most_common():
        mark = " ◀" if (args.attr and attr in args.attr) else ""
        print(f"  {attr}: {cnt}{mark}")

    target = args.attr or ATTRS_KR

    print(f"\nLoading SBERT model: {SBERT_MODEL} ...")
    import torch
    _device = "cuda" if torch.cuda.is_available() else "cpu"
    sbert = SentenceTransformer(SBERT_MODEL, device=_device)
    print(f"  device: {sbert.device}, cuda_available: {torch.cuda.is_available()}", flush=True)

    # 기존 결과 로드 (부분 실행 시 병합)
    existing = {}
    if os.path.exists(OUTPUT) and args.attr:
        with open(OUTPUT, encoding="utf-8") as f:
            existing = json.load(f)

    results = dict(existing)
    for attr in ATTRS_KR:
        if attr not in target:
            continue
        res = process_attr(articles, attr, sbert, skip_gpt_label=args.no_label)
        if res:
            results[attr] = res

    clean_for_save(results)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    # ── 요약 ──
    print(f"\n{'='*60}")
    print(f"저장: {OUTPUT}\n")
    print(f"{'속성':20s} {'기사':>6s} {'토픽':>4s} {'병합':>4s} {'EN전용':>6s} {'KO전용':>6s} {'아웃라이어':>8s}")
    print("-" * 60)
    for attr in ATTRS_KR:
        if attr in results:
            d = results[attr]
            pct = d['n_outliers']/d['n_articles']*100 if d['n_articles'] else 0
            print(f"{attr:20s} {d['n_articles']:>6d} {d['n_topics']:>4d} "
                  f"{d.get('n_merged',0):>4d} {d.get('n_en_only',0):>6d} "
                  f"{d.get('n_ko_only',0):>6d} {d['n_outliers']:>6d}({pct:.0f}%)")


if __name__ == "__main__":
    main()
