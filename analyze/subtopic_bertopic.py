"""BERTopic 소주제 추출 — 언어 분리 + 의미 정렬(cross-lingual alignment).

EN/KO 토픽 센트로이드 코사인 유사도로 대응 토픽 병합.
결과: 병합 토픽 / EN 고유 토픽 / KO 고유 토픽.

Usage:
    python subtopic_bertopic.py --attr 책임/윤리AI
    python subtopic_bertopic.py
"""
import json, os, sys, time, html as html_mod, re, argparse, hashlib, math
from collections import Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-assembly-data")

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

KR_PROMPT_VERSION = "v2_en_20260418"
KR_BODY_CHAR_CAP  = 1500


# ─── Kiwi 토크나이저 (KO 명사 추출, 조사 결합형 해소) ───
KIWI_WORKERS = min(8, os.cpu_count() or 1)
_kiwi = Kiwi(num_workers=KIWI_WORKERS)
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

def _ko_forms(tokens):
    out = []
    for t in tokens:
        if t.tag not in KO_NOUN_TAGS:
            continue
        form = _PUNCT_TAIL.sub("", t.form)
        if len(form) >= 2:
            out.append(form)
    return out


def ko_tokenizer(text):
    return _ko_forms(_kiwi.tokenize(text))


def pretokenize_ko_texts(texts):
    return [" ".join(_ko_forms(tokens)) for tokens in _kiwi.tokenize(texts)]

OUTPUT   = os.path.join(config.ANALYSIS_DIR, "subtopics_bertopic.json")
EMBED_CACHE_DIR = config.BERTOPIC_EMBED_CACHE
os.makedirs(EMBED_CACHE_DIR, exist_ok=True)

_client = None
def _get_client():
    global _client
    if _client is None:
        _client = OpenAI()
    return _client

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

# ── (B) 동일 언어 내 유사 토픽 사후 병합 ──
# stability 연구(working/finding_cluster_stability.md): cuML 비결정성으로 swap되는
# fragment들의 centroid sim 0.78~0.91 = "사람 기준 한 토픽이 sub-cluster로 쪼개진 인공물".
# leaf 과분할을 유지한 채 sim≥임계값인 동일 언어 토픽을 transitive(connected components) 병합.
MERGE_SIMILAR_TOPICS    = False   # --merge-similar 로 활성화
# (B) KO(및 mixed) 토픽에만 적용 — EN 은 미병합 후 align_topics 에서 cross-lingual 정렬.
# 임계값 0.80: working/diagnose_merge_cuml.py 의 cuML 백엔드 실측 기준. KO 토픽 centroid
# sim 분포(median 0.72~0.77, max 0.93~0.96)에서 0.80 은 평균연결(AGG) 기준 적극 감축
# 구간(산업 KO 99→13, 공익 49→12, 노동 34→8). 인공물 fragment 통합이 주 목적.
MERGE_SIMILAR_THRESHOLD = 0.80    # --merge-threshold 로 조정

CLUSTER_BACKEND = "cpu"
DETERMINISTIC_CLUSTERING = False
USE_KEYBERT_REPRESENTATION = True
# HDBSCAN 토픽 선택 방식: "leaf"(잘게 쪼갬, 기본) / "eom"(안정적 상위 클러스터, 토픽 적음).
# --cluster-method 로 조정. eom 은 작은 EN 데이터에서 거대 컨테이너 위험(실측 확인됨).
CLUSTER_SELECTION_METHOD = "leaf"

# ── 속성별 min_cluster_size 함수 (거대 컨테이너 분해, 2026-06-01 재calibration) ──
# 단일 함수(sqrt×0.6)는 공익 58%·AI안전 52%·국가 38% 등 거대 컨테이너를 못 깬다.
# working/calibrate_mcs.py 로 8함수×10속성 sweep 후, 속성별로 max%(1위 토픽 비중)를
# 낮추면서 outlier 폭증·과편화를 피하고 top 토픽이 on-theme 인 함수를 선택.
# 근거·전체 매트릭스: working/calibration_history.md, working/mcs_calibration.json.
# ⚠ 산업·공익·책임·선거는 cuML 비결정성으로 재실행 시 결과 변동 가능(working/finding_cluster_stability.md).
# 책임/윤리AI 는 현행 sqrt×0.6 이 이미 max 15%·최저 outlier 라 유지(거대 컨테이너 없음).
_DEFAULT_MCS = lambda n: max(8, math.ceil(0.6 * math.sqrt(n)))
ATTR_MCS = {
    '산업정책':        lambda n: max(8, math.ceil(0.5 * math.sqrt(n))),        # sqrt×0.5 → 69t max11%
    '공익/소비자보호':  lambda n: max(8, math.ceil(10 * math.log10(max(n, 10)))),  # log10×10 → 38t max16%
    '국가안보':        lambda n: max(8, n // 200),                            # linear   → 38t max30%
    '노동/고용':       lambda n: max(8, math.ceil(3 * n ** (1/3))),           # cbrt×3   → 21t max19%
    '책임/윤리AI':     _DEFAULT_MCS,                                          # sqrt×0.6 유지(이미 양호)
    '시장경쟁/독과점':  lambda n: max(8, math.ceil(3 * n ** (1/3))),           # cbrt×3   → 24t max7%
    '국제협력':        lambda n: max(8, n // 200),                            # linear   → 38t max10%
    '선거/민주주의':    lambda n: max(8, math.ceil(10 * math.log10(max(n, 10)))),  # log10×10 → 13t max10%
    'AI안전':         lambda n: max(8, math.ceil(15 * math.log10(max(n, 10)))),  # log10×15 → 8t max14%
    '저작권/지식재산':  lambda n: max(8, n // 200),                            # linear   → 33t max6%
}

BASE_STOPWORDS = [
    'ai','AI','A.I','A.I.','Ai','artificial','intelligence','the','and','for','that','with',
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

def load_news(langs=("ko", "en")):
    """기사 로드. langs 로 적재 언어 선택 — KO 단독 분석 시 ("ko",)."""
    articles = []

    # ── 영문 (Guardian / NYT) — langs 에 'en' 있을 때만 ──
    if "en" in langs:
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
                aid = a.get("article_id","")
                text = text_map.get(aid,"")
                if len(text) > 20:
                    articles.append({"id":f"{source}:{aid}","text":text,"source":source,"attr":attr,"lang":lang})

    if "ko" not in langs:
        return articles

    # ── KR domestic news (news_analysis.duckdb — Stage 1+2 적용본) ──
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH, read_only=True)
    con.execute("PRAGMA disable_progress_bar")
    kr_sql = f"""
      SELECT n.news_id,
             n.title,
             SUBSTR(n.content, 1, {KR_BODY_CHAR_CAP}) AS body,
             n.provider,
             c.primary_attr
      FROM news_articles n
      JOIN news_classifications c
        ON c.news_id = n.news_id
       AND c.prompt_version = '{KR_PROMPT_VERSION}'
       AND c.error IS NULL
       AND c.primary_attr IS NOT NULL
       AND c.primary_attr <> 'none'
    """
    kr_rows = con.execute(kr_sql).fetchall()
    con.close()
    n_kr_loaded = 0
    n_kr_skipped_label = 0
    for news_id, title, body, provider, primary in kr_rows:
        attr = NORMALIZE.get(primary)
        if attr not in ATTRS_KR:
            n_kr_skipped_label += 1
            continue
        text = clean_text(f"{title or ''}. {body or ''}")
        if len(text) > 20:
            articles.append({
                "id":     f"kr:{news_id}",
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
def make_cluster_models(n_neigh, n_comp, mcs):
    if CLUSTER_BACKEND == "cuml":
        from cuml.manifold import UMAP as cuUMAP
        from cuml.cluster import HDBSCAN as cuHDBSCAN

        # cuML UMAP with a fixed random_state can force serial epochs. Leave it
        # unset for the fast GPU path unless explicitly requested.
        random_state = 42 if DETERMINISTIC_CLUSTERING else None
        umap_model = cuUMAP(
            n_neighbors=n_neigh,
            n_components=n_comp,
            min_dist=0.0,
            metric="cosine",
            random_state=random_state,
            output_type="numpy",
        )
        hdbscan_model = cuHDBSCAN(
            min_cluster_size=mcs,
            min_samples=1,
            metric="euclidean",
            prediction_data=True,
            cluster_selection_method=CLUSTER_SELECTION_METHOD,
            output_type="numpy",
        )
        return umap_model, hdbscan_model

    umap_model = UMAP(
        n_neighbors=n_neigh,
        n_components=n_comp,
        min_dist=0.0,
        metric="cosine",
        random_state=42,
    )
    hdbscan_model = HDBSCAN(
        min_cluster_size=mcs,
        min_samples=1,
        metric="euclidean",
        prediction_data=True,
        cluster_selection_method="leaf",
        core_dist_n_jobs=-1,
    )
    return umap_model, hdbscan_model


def embedding_cache_path(attr, lang_tag, texts):
    h = hashlib.sha1()
    h.update(SBERT_MODEL.encode("utf-8"))
    h.update(lang_tag.encode("utf-8"))
    h.update(attr.encode("utf-8"))
    for text in texts:
        encoded = text.encode("utf-8", errors="ignore")
        h.update(len(encoded).to_bytes(8, "little"))
        h.update(encoded)
    safe_attr = re.sub(r"[^A-Za-z0-9가-힣_-]+", "_", attr).strip("_")
    return os.path.join(EMBED_CACHE_DIR, f"{safe_attr}_{lang_tag}_{h.hexdigest()[:16]}.npy")


def embed_texts(texts, attr, sbert, lang_tag):
    import time as _t

    os.makedirs(EMBED_CACHE_DIR, exist_ok=True)
    cache_path = embedding_cache_path(attr, lang_tag, texts)
    if os.path.exists(cache_path):
        _t0 = _t.time()
        embeddings = np.load(cache_path)
        print(f"    [{lang_tag}] 임베딩 캐시 로드: {_t.time()-_t0:.1f}s, shape={embeddings.shape}", flush=True)
        return embeddings

    print(f"    [{lang_tag}] {len(texts)}건 임베딩 시작...", flush=True)
    _t0 = _t.time()
    embeddings = sbert.encode(texts, show_progress_bar=True,
                               batch_size=SBERT_BATCH,
                               normalize_embeddings=True,
                               device=str(sbert.device))   # (n, 1024)
    print(f"    [{lang_tag}] 임베딩 완료: {_t.time()-_t0:.1f}s, shape={embeddings.shape}", flush=True)
    np.save(cache_path, embeddings)
    print(f"    [{lang_tag}] 임베딩 캐시 저장: {cache_path}", flush=True)
    return embeddings


def run_bertopic_lang(texts, sources, attr, sbert, lang_tag):
    import time as _t
    n = len(texts)
    embeddings = embed_texts(texts, attr, sbert, lang_tag)

    stopwords = BASE_STOPWORDS + ATTR_EXTRA_STOPWORDS.get(attr, [])
    # 속성별 mcs (ATTR_MCS, 2026-06-01 재calibration). 미등록 속성은 sqrt×0.6 기본.
    mcs        = ATTR_MCS.get(attr, _DEFAULT_MCS)(n)
    n_comp     = 10 if n > 800 else 5
    n_neigh    = 15

    print(f"    min_cluster_size={mcs}, n_comp={n_comp}, n_neigh={n_neigh}")

    fit_texts = texts
    if lang_tag == "en":
        vectorizer_model = CountVectorizer(
            stop_words=stopwords, min_df=2, ngram_range=(1, 2),
            token_pattern=r'(?u)\b[a-zA-Z가-힣]{2,}\b')
    else:  # "ko" or "mixed" — Kiwi 명사 추출로 조사 결합형 해소
        print(f"    [{lang_tag}] Kiwi 병렬 토큰화 시작...", flush=True)
        _tok0 = _t.time()
        fit_texts = pretokenize_ko_texts(texts)
        print(f"    [{lang_tag}] Kiwi 병렬 토큰화 완료: {_t.time()-_tok0:.1f}s", flush=True)
        vectorizer_model = CountVectorizer(
            lowercase=False,
            stop_words=stopwords, min_df=2, ngram_range=(1, 2))

    umap_model, hdbscan_model = make_cluster_models(n_neigh, n_comp, mcs)

    topic_model = BERTopic(
        embedding_model=sbert,
        umap_model=umap_model,
        hdbscan_model=hdbscan_model,
        vectorizer_model=vectorizer_model,
        representation_model=KeyBERTInspired() if USE_KEYBERT_REPRESENTATION else None,
        seed_topic_list=SEED_TOPICS.get(attr),
        nr_topics="auto",
        verbose=False,
    )
    print(f"    [{lang_tag}] BERTopic fit 시작...", flush=True)
    _t1 = _t.time()
    topics, _ = topic_model.fit_transform(fit_texts, embeddings)
    print(f"    [{lang_tag}] BERTopic fit 완료: {_t.time()-_t1:.1f}s", flush=True)

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

        # 대표 문서 (센트로이드 최근접 5건) — 원본 임베딩 차원(1024)으로 계산
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

    # (B) 병합은 호출부(process_attr)에서 KO·mixed 에만 선택 적용 — EN 은 미병합.
    return topics_data, embeddings, topics


def apply_b_merge(topics_data, topics, lang_tag):
    """(B) 동일 언어 유사 토픽 병합을 topics_data + topics(article→tid) 에 적용.

    process_attr 에서 KO·mixed 에만 호출. EN 은 호출하지 않아 미병합 유지.
    반환: (topics_data, topics)  — 병합 비활성·토픽<2 면 원본 그대로.
    """
    if not (MERGE_SIMILAR_TOPICS and len(topics_data) > 1):
        return topics_data, topics
    before = len(topics_data)
    topics_data, remap = merge_similar_topics_data(topics_data, MERGE_SIMILAR_THRESHOLD)
    topics = [remap.get(t, t) if t != -1 else -1 for t in topics]
    n_groups = sum(1 for v in topics_data.values() if v.get("n_merged_subtopics", 1) > 1)
    print(f"    [{lang_tag}] 유사 토픽 병합: {before}개 → {len(topics_data)}개 "
          f"(sim≥{MERGE_SIMILAR_THRESHOLD}, 병합그룹 {n_groups}개)", flush=True)
    return topics_data, topics


# ──────────────────────────────────────────────
# (B) 동일 언어 유사 토픽 병합 (transitive / connected components)
# ──────────────────────────────────────────────
def merge_similar_topics_data(topics_data, threshold):
    """같은 언어의 토픽들 중 centroid 코사인 유사도 >= threshold 인 것들을
    connected components 로 묶어 하나로 병합.

    1:1 매칭이 아니라 transitive: A~B, B~C 이면 {A,B,C} 한 그룹.
    각 그룹의 대표 topic_id 는 최대 count 토픽.

    반환: (new_topics_data, remap)
      new_topics_data: {대표_tid: 병합된 토픽 dict}  (구조는 입력과 동일 + n_merged_subtopics)
      remap: {old_tid: 대표_tid}  — 호출부에서 article→topic 배열 remap 에 사용
    """
    tids = list(topics_data.keys())
    if len(tids) <= 1:
        return topics_data, {t: t for t in tids}

    centroids = np.stack([topics_data[t]["centroid"] for t in tids])

    # average-linkage 계층 군집 (centroid 코사인 거리, 1-sim).
    # single-linkage(union-find)는 A~B~C~D 사슬(chaining)로 의미가 먼 토픽까지
    # 한 덩어리로 묶어 거대 컨테이너를 만든다. average-linkage 는 "그룹 전체와
    # 평균적으로 가까워야 합류"하므로 chaining 없이 안전하게 병합한다.
    from collections import defaultdict
    n = len(tids)
    sim = cosine_similarity(centroids)
    dist = 1.0 - sim
    np.fill_diagonal(dist, 0.0)
    dist = (dist + dist.T) / 2.0
    from scipy.cluster.hierarchy import linkage, fcluster
    Z = linkage(dist[np.triu_indices(n, 1)], method="average")
    labels = fcluster(Z, t=1.0 - threshold, criterion="distance")
    groups = defaultdict(list)
    for t, lab in zip(tids, labels):
        groups[int(lab)].append(t)

    new_topics_data, remap = {}, {}
    for members in groups.values():
        if len(members) == 1:
            t = members[0]
            new_topics_data[t] = topics_data[t]
            remap[t] = t
            continue

        # 대표 = 최대 count
        members_sorted = sorted(members, key=lambda t: topics_data[t]["count"], reverse=True)
        rep   = members_sorted[0]
        total = sum(topics_data[t]["count"] for t in members)

        # count 가중 centroid
        cent = sum(topics_data[t]["centroid"] * topics_data[t]["count"] for t in members) / total

        # 키워드 union (큰 토픽 우선, 중복 제거)
        kw_order, seen_kw, merged_src = [], set(), {}
        for t in members_sorted:
            for k in topics_data[t]["keywords"]:
                if k not in seen_kw:
                    seen_kw.add(k); kw_order.append(k)
            for src, c in topics_data[t]["source_dist"].items():
                merged_src[src] = merged_src.get(src, 0) + c

        new_topics_data[rep] = {
            "lang":               topics_data[rep]["lang"],
            "keywords":           kw_order[:10],
            "count":              total,
            "source_dist":        merged_src,
            "centroid":           cent,
            "representative_docs": topics_data[rep]["representative_docs"],
            "n_merged_subtopics": len(members),
        }
        for t in members:
            remap[t] = rep

    return new_topics_data, remap


# ──────────────────────────────────────────────
# 의미 정렬 (cross-lingual alignment)
# ──────────────────────────────────────────────
def align_topics(en_topics, ko_topics, threshold=MERGE_THRESHOLD):
    """EN 토픽과 KO 토픽의 센트로이드 코사인 유사도로 대응 쌍 탐색."""
    if not en_topics or not ko_topics:
        return list(en_topics.values()), list(ko_topics.values()), []

    en_ids = list(en_topics.keys())
    ko_ids = list(ko_topics.keys())

    en_centroids = np.stack([en_topics[i]["centroid"] for i in en_ids])  # (n_en, 1024)
    ko_centroids = np.stack([ko_topics[i]["centroid"] for i in ko_ids])  # (n_ko, 1024)

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
        resp  = _get_client().chat.completions.create(
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

    ids     = [a.get("id","")  for a in subset]
    texts   = [a["text"]   for a in subset]
    sources = [a["source"] for a in subset]
    langs   = [a["lang"]   for a in subset]

    n_en = sum(1 for l in langs if l == "en")
    n_ko = sum(1 for l in langs if l == "ko")
    print(f"\n{'='*60}")
    print(f"[{attr}] 총 {n_total}건 — EN:{n_en} / KO:{n_ko}")

    # ── 언어별 독립 분류 (cross-lingual merge 없음) ──
    # 각 언어를 따로 BERTopic → 각 토픽은 그 언어 전용(en_only / ko_only).
    # EN 은 기사량이 적어 단독 subtopic 분석이 부정확하므로 기본 미적재(--lang ko).
    assignments = []   # [(article_id, source, lang, topic_id), ...]
    all_topics = []
    n_outliers = 0
    type_counts = {"en_only": 0, "ko_only": 0}

    for lang_tag, type_name in (("ko", "ko_only"), ("en", "en_only")):
        idx = [i for i, l in enumerate(langs) if l == lang_tag]
        if len(idx) < 30:
            continue
        l_texts   = [texts[i]   for i in idx]
        l_sources = [sources[i] for i in idx]
        l_ids     = [ids[i]     for i in idx]

        topics_data, emb, topics = run_bertopic_lang(l_texts, l_sources, attr, sbert, lang_tag)
        # (B) 동일 언어 유사 토픽 병합 (기본 OFF; --merge-similar 시에만)
        topics_data, topics = apply_b_merge(topics_data, topics, lang_tag)

        for aid, src, tid in zip(l_ids, l_sources, topics):
            assignments.append((aid, src, lang_tag, int(tid)))
        all_topics += [dict(v, type=type_name, topic_id=k) for k, v in topics_data.items()]
        n_outliers += sum(1 for t in topics if t == -1)
        type_counts[type_name] = len(topics_data)

    merged   = []
    en_only  = [t for t in all_topics if t["type"] == "en_only"]
    ko_only  = [t for t in all_topics if t["type"] == "ko_only"]

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
        "assignments": assignments,
    }


# ──────────────────────────────────────────────
# DuckDB: article→subtopic 매핑 저장
# ──────────────────────────────────────────────
ASSIGNMENT_TABLE = "subtopic_assignments"

def write_assignments_to_db(results):
    """Append per-article subtopic assignment to NEWS_ANALYSIS_DB.

    Schema:
      attr           VARCHAR     -- e.g. '책임/윤리AI'
      article_id     VARCHAR     -- 'guardian:..' / 'nyt:..' / 'kr:<news_id>'
      source         VARCHAR     -- provider name (KBS/MBC/.. or 'guardian'/'nyt')
      lang           VARCHAR     -- 'en' / 'ko' / 'mixed'
      topic_id       INTEGER     -- per (attr,lang) topic; -1 = outlier
      run_timestamp  TIMESTAMP   -- same value for every row in this run
    """
    from datetime import datetime
    rows = []
    run_ts = datetime.now()
    for attr, data in results.items():
        for aid, src, lang, tid in data.get("assignments", []):
            if not aid:        # skip articles missing an id
                continue
            rows.append((attr, aid, src, lang, tid, run_ts))
    if not rows:
        print("  [DB] no assignments to write")
        return
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {ASSIGNMENT_TABLE} (
            attr           VARCHAR,
            article_id     VARCHAR,
            source         VARCHAR,
            lang           VARCHAR,
            topic_id       INTEGER,
            run_timestamp  TIMESTAMP
        )
    """)
    con.executemany(
        f"INSERT INTO {ASSIGNMENT_TABLE} VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.close()
    print(f"  [DB] wrote {len(rows):,} rows to {ASSIGNMENT_TABLE} "
          f"(run_timestamp={run_ts.isoformat(timespec='seconds')})")


# ──────────────────────────────────────────────
# 정리: centroid 제거 후 저장
# ──────────────────────────────────────────────
def clean_for_save(results, keep_repr=False):
    """centroid 는 항상 제거. keep_repr=True 면 대표문서 유지(--label-only 가 사용)."""
    for attr, data in results.items():
        for t in data["topics"]:
            t.pop("centroid", None)
            if not keep_repr:
                t.pop("representative_docs", None)
                t.pop("representative_docs_en", None)
                t.pop("representative_docs_ko", None)
    return results


LABEL_CENTROID_CAP = 50   # centroid 최근접 상위 N개 제목을 GPT 에 투입


def _label_titles_by_centroid(attr, topics, articles, db_topic):
    """attr 의 각 토픽에 대해 centroid 최근접 상위 LABEL_CENTROID_CAP 개 제목 + 점수 반환.
    반환: {topic_id: [(score, title), ...] 점수 내림차순}."""
    import glob
    subset = [a for a in articles if a["attr"] == attr]
    ids   = [a["id"]   for a in subset]
    safe_attr = re.sub(r"[^A-Za-z0-9가-힣_-]+", "_", attr).strip("_")
    cands = [f for f in glob.glob(os.path.join(EMBED_CACHE_DIR, f"{safe_attr}_ko_*.npy"))
             if "_meta" not in f]
    if not cands:
        return {}
    emb = np.load(max(cands, key=os.path.getmtime))
    if len(emb) != len(ids):
        print(f"  ⚠ [{attr}] 캐시({len(emb)})≠기사({len(ids)}) — centroid 라벨 스킵")
        return {}
    id2row = {aid: i for i, aid in enumerate(ids)}
    from collections import defaultdict
    by_t = defaultdict(list)   # topic_id -> [(row, title)]
    for aid, (tid, title) in db_topic.get(attr, {}).items():
        if tid == -1 or aid not in id2row:
            continue
        by_t[tid].append((id2row[aid], title))
    out = {}
    for tid, items in by_t.items():
        rws = [r for r, _ in items]
        tts = [t for _, t in items]
        cent = emb[rws].mean(axis=0, keepdims=True)
        sims = cosine_similarity(emb[rws], cent).ravel()
        order = np.argsort(-sims)[:LABEL_CENTROID_CAP]
        out[tid] = [(float(sims[i]), tts[i]) for i in order]
    return out


def label_topic_centroid(attr_en, scored):
    """centroid 점수 표기된 제목들로 토픽 라벨 생성 (저점 outlier 무시 지시)."""
    body = "\n".join(f"- [{s:.2f}] {t}" for s, t in scored)
    prompt = f"""AI policy subtopic in "{attr_en}". Korean article titles sorted by
representativeness score [0-1] = cosine similarity to the cluster centroid (higher = central).

Titles ({len(scored)}):
{body}

Give ONE concise subtopic label (3-7 words) for the dominant theme of the high-score titles.
Ignore rare low-score outliers. Respond ONLY with JSON: {{"en": "English label", "ko": "한국어 라벨"}}"""
    try:
        r = _get_client().chat.completions.create(
            model="gpt-4.1-mini", messages=[{"role": "user", "content": prompt}],
            temperature=0, response_format={"type": "json_object"})
        o = json.loads(r.choices[0].message.content)
        return o.get("ko", ""), o.get("en", "")
    except Exception as e:
        print(f"    GPT 오류(topic): {e}")
        return "", ""


def label_group_umbrella(attr_en, members):
    """묶음 그룹의 상위 라벨 — 하위 토픽 라벨+건수+키워드 기반.
    members: [(label_ko, count, keywords_str), ...]"""
    body = "\n".join(f"- ({c}건) {lk}  [kw: {kw}]" for lk, c, kw in members)
    prompt = f"""AI policy attribute "{attr_en}". Below are sub-topics grouped together
because their article embeddings are similar (count + label each).

Sub-topics ({len(members)}):
{body}

Write ONE umbrella label (3-8 words) capturing what these sub-topics SHARE.
- Cover the MAJORITY of sub-topics (weight by article count), not just one.
- General enough to encompass them, but specific to this attribute.
Respond ONLY with JSON: {{"en": "English umbrella label", "ko": "한국어 상위 라벨"}}"""
    try:
        r = _get_client().chat.completions.create(
            model="gpt-4.1-mini", messages=[{"role": "user", "content": prompt}],
            temperature=0, response_format={"type": "json_object"})
        o = json.loads(r.choices[0].message.content)
        return o.get("ko", ""), o.get("en", "")
    except Exception as e:
        print(f"    GPT 오류(group): {e}")
        return "", ""


def run_label_only():
    """클러스터링 재실행 없이 저장된 JSON 토픽에 GPT 라벨 부여 (centroid 점수 기반).

    1) 토픽 라벨: centroid 최근접 상위 N개 제목(점수표기) → GPT
    2) 그룹 라벨(묶음): 하위 토픽 라벨 + 건수 + 키워드 → GPT 상위 라벨 (group_label_ko/en)
    3) 단독 토픽: 토픽 라벨 그대로
    centroid 점수는 임베딩 캐시 + DB(최신 run) 에서 재계산 → 클러스터링 재실행 불필요."""
    import time as _t
    from collections import defaultdict
    if not os.path.exists(OUTPUT):
        print(f"[label-only] {OUTPUT} 없음 — 먼저 클러스터링을 실행하세요.")
        return
    with open(OUTPUT, encoding="utf-8") as f:
        results = json.load(f)

    # DB: article_id -> (topic_id, title), attr 별
    articles = load_news(langs=("ko",))
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH, read_only=True)
    run = con.execute("SELECT max(run_timestamp) FROM subtopic_assignments").fetchone()[0]
    rows = con.execute("""
        SELECT a.attr, a.article_id, a.topic_id, n.title
        FROM subtopic_assignments a
        LEFT JOIN news_articles n ON a.article_id = ('kr:' || n.news_id)
        WHERE a.run_timestamp = ? AND a.lang = 'ko'
    """, [run]).fetchall()
    con.close()
    db_topic = defaultdict(dict)
    for attr, aid, tid, title in rows:
        db_topic[attr][aid] = (tid, clean_text(title) or "(제목없음)")

    for attr, data in results.items():
        attr_en = KR_TO_EN.get(attr, attr)
        topics = data.get("topics", [])
        scored_by_tid = _label_titles_by_centroid(attr, topics, articles, db_topic)

        # 1) 토픽 라벨
        for t in topics:
            scored = scored_by_tid.get(t["topic_id"])
            if scored:
                lk, le = label_topic_centroid(attr_en, scored); _t.sleep(0.1)
            else:   # centroid 실패 시 키워드 fallback
                lk = le = ", ".join(t.get("keywords", [])[:3])
            t["label_ko"], t["label_en"] = lk, le

        # 2) 그룹 단위 라벨
        groups = defaultdict(list)
        for t in topics:
            groups[t.get("group_id", t["topic_id"])].append(t)
        n_grp = 0
        for gid, members in groups.items():
            if len(members) <= 1:
                continue   # 단독: 토픽 라벨 그대로
            members = sorted(members, key=lambda x: x.get("count", 0), reverse=True)
            payload = [(m.get("label_ko", ""), m.get("count", 0),
                        ", ".join(m.get("keywords", [])[:5])) for m in members]
            gk, ge = label_group_umbrella(attr_en, payload); _t.sleep(0.1)
            for m in members:
                m["group_label_ko"], m["group_label_en"] = gk, ge
            n_grp += 1
        print(f"[{attr}] 토픽 {len(topics)} 라벨 + 묶음그룹 {n_grp} 상위라벨", flush=True)

    clean_for_save(results, keep_repr=False)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n저장(라벨 갱신): {OUTPUT}")


# ──────────────────────────────────────────────
# (C) 토픽 그룹화 — centroid average-linkage 로 토픽을 상위 그룹으로 묶기
# ──────────────────────────────────────────────
GROUP_EXCLUDE_ATTRS = {"AI안전"}   # 토픽 수가 적어 그룹화 시 1그룹으로 뭉개지는 attr


def _topic_centroids_from_cache(attr, articles, db_topic):
    """attr 의 (topic_id -> centroid) 를 임베딩 캐시 + DB assignment 에서 재계산.
    반환: {topic_id: centroid(np.array)} (outlier -1 제외)."""
    import glob
    subset = [a for a in articles if a["attr"] == attr]
    ids = [a["id"] for a in subset]
    safe_attr = re.sub(r"[^A-Za-z0-9가-힣_-]+", "_", attr).strip("_")
    cands = [f for f in glob.glob(os.path.join(EMBED_CACHE_DIR, f"{safe_attr}_ko_*.npy"))
             if "_meta" not in f]
    if not cands:
        return None
    emb = np.load(max(cands, key=os.path.getmtime))
    if len(emb) != len(ids):
        print(f"  ⚠ [{attr}] 캐시({len(emb)})≠기사({len(ids)}) — 그룹화 스킵")
        return None
    from collections import defaultdict
    acc = defaultdict(list)
    for i, aid in enumerate(ids):
        t = db_topic.get(aid)
        if t is None or t == -1:
            continue
        acc[t].append(emb[i])
    return {t: np.mean(v, axis=0) for t, v in acc.items()}


def run_group_topics(threshold):
    """저장된 JSON 토픽에 group_id 부여 (average-linkage, attr별 독립).
    centroid 는 임베딩 캐시 + DB(최신 run) 에서 재계산 → step1 재실행 불필요."""
    from scipy.cluster.hierarchy import linkage, fcluster

    if not os.path.exists(OUTPUT):
        print(f"[group] {OUTPUT} 없음 — 먼저 클러스터링을 실행하세요.")
        return
    with open(OUTPUT, encoding="utf-8") as f:
        results = json.load(f)

    articles = load_news(langs=("ko",))
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH, read_only=True)
    run = con.execute("SELECT max(run_timestamp) FROM subtopic_assignments").fetchone()[0]
    rows = con.execute("SELECT article_id, topic_id FROM subtopic_assignments "
                       "WHERE run_timestamp=? AND lang='ko'", [run]).fetchall()
    con.close()
    db_topic = {aid: tid for aid, tid in rows}

    print(f"=== 토픽 그룹화 (sim≥{threshold}, average-linkage) ===")
    for attr, data in results.items():
        topics = data.get("topics", [])
        # 제외 attr 또는 토픽<2 → 각 토픽이 독립 그룹
        if attr in GROUP_EXCLUDE_ATTRS or len(topics) < 2:
            for i, t in enumerate(topics):
                t["group_id"] = i
            data["n_groups"] = len(topics)
            print(f"  [{attr}] 그룹화 제외 — {len(topics)}토픽 = {len(topics)}그룹")
            continue

        cents_map = _topic_centroids_from_cache(attr, articles, db_topic)
        if not cents_map:
            for i, t in enumerate(topics):
                t["group_id"] = i
            data["n_groups"] = len(topics)
            continue

        tids = [t["topic_id"] for t in topics]
        # centroid 없는 토픽(드묾) 방어: 자기 자신 그룹
        usable = [tid for tid in tids if tid in cents_map]
        cents = np.stack([cents_map[tid] for tid in usable])
        sim = cosine_similarity(cents)
        dist = 1.0 - sim
        np.fill_diagonal(dist, 0.0)
        dist = (dist + dist.T) / 2.0
        Z = linkage(dist[np.triu_indices(len(usable), 1)], method="average")
        labels = fcluster(Z, t=1.0 - threshold, criterion="distance")
        gid_of = {tid: int(lab) for tid, lab in zip(usable, labels)}
        next_gid = (max(labels) + 1) if len(labels) else 0
        for t in topics:
            if t["topic_id"] in gid_of:
                t["group_id"] = gid_of[t["topic_id"]]
            else:
                t["group_id"] = next_gid; next_gid += 1
        data["n_groups"] = len(set(t["group_id"] for t in topics))
        print(f"  [{attr}] {len(topics)}토픽 → {data['n_groups']}그룹")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n저장(그룹 갱신): {OUTPUT}")

    from export_subtopic_lists import write_ko_lists, write_combined_lists
    write_ko_lists(OUTPUT, os.path.join(config.OUTPUT_DIR, "article_lists_ko.md"))
    write_combined_lists(OUTPUT, os.path.join(config.OUTPUT_DIR, "article_lists_by_subtopic.md"))


def resolve_cluster_backend(requested, cuda_available):
    if requested == "cpu" or not cuda_available:
        return "cpu"
    try:
        import cuml  # noqa: F401
        return "cuml"
    except Exception as e:
        if requested == "cuml":
            raise RuntimeError("cuML backend requested but cuML is unavailable") from e
        print(f"cuML unavailable, falling back to CPU: {e}", flush=True)
        return "cpu"


def main():
    global CLUSTER_BACKEND, DETERMINISTIC_CLUSTERING, USE_KEYBERT_REPRESENTATION
    global MERGE_SIMILAR_TOPICS, MERGE_SIMILAR_THRESHOLD, CLUSTER_SELECTION_METHOD

    parser = argparse.ArgumentParser()
    parser.add_argument("--attr", nargs="+")
    parser.add_argument("--no-label", action="store_true",
                        help="GPT 라벨링 스킵 (키워드 상위 3개를 라벨로 사용)")
    parser.add_argument("--backend", choices=["auto", "cuml", "cpu"], default="auto",
                        help="UMAP/HDBSCAN backend. auto uses cuML when CUDA is available.")
    parser.add_argument("--deterministic", action="store_true",
                        help="Use deterministic clustering settings. Slower with cuML.")
    parser.add_argument("--no-keybert", action="store_true",
                        help="Skip KeyBERTInspired topic representation for faster diagnostics.")
    parser.add_argument("--merge-similar", action="store_true",
                        help="(B) 동일 언어 내 centroid sim≥임계값 토픽을 transitive 병합 (과분할 완화)")
    parser.add_argument("--merge-threshold", type=float, default=MERGE_SIMILAR_THRESHOLD,
                        help=f"--merge-similar 의 코사인 유사도 임계값 (기본 {MERGE_SIMILAR_THRESHOLD})")
    parser.add_argument("--cluster-method", choices=["leaf", "eom"], default=CLUSTER_SELECTION_METHOD,
                        help="HDBSCAN 토픽 선택 방식. leaf=잘게 쪼갬(기본), eom=안정적 상위 클러스터(토픽 적음)")
    parser.add_argument("--lang", choices=["ko", "en", "both"], default="ko",
                        help="분류 대상 언어. ko=한국 기사만(기본), en=영문만, both=둘 다 독립 분류")
    parser.add_argument("--label-only", action="store_true",
                        help="클러스터링 재실행 없이 저장된 JSON 토픽에 GPT 라벨만 부여 (cuML 비결정성 분리)")
    parser.add_argument("--group-topics", action="store_true",
                        help="저장된 JSON 토픽을 centroid average-linkage 로 상위 그룹화 (group_id 부여)")
    parser.add_argument("--group-threshold", type=float, default=0.80,
                        help="--group-topics 의 코사인 유사도 임계값 (기본 0.80)")
    args = parser.parse_args()

    MERGE_SIMILAR_TOPICS    = args.merge_similar
    MERGE_SIMILAR_THRESHOLD = args.merge_threshold
    CLUSTER_SELECTION_METHOD = args.cluster_method
    langs = ("ko", "en") if args.lang == "both" else (args.lang,)

    # ── 라벨링 단계 (클러스터링·SBERT·DB 미접근) ──
    if args.label_only:
        print("=== GPT 라벨링 전용 (클러스터링 재실행 없음) ===\n")
        run_label_only()
        return

    # ── 그룹화 단계 (클러스터링·SBERT 미접근, DB read-only) ──
    if args.group_topics:
        run_group_topics(args.group_threshold)
        return

    print("=== BERTopic v5: 언어별 독립 분류 (cross-lingual merge 제거) ===\n")
    print(f"분류 언어: {args.lang}")
    print(f"HDBSCAN 토픽 선택: {CLUSTER_SELECTION_METHOD}")
    print(f"라벨링: 클러스터링 단계에서는 키워드 임시 라벨만 — GPT 라벨은 --label-only 로 별도 실행")
    if MERGE_SIMILAR_TOPICS:
        print(f"동일 언어 유사 토픽 병합(B): ON (sim≥{MERGE_SIMILAR_THRESHOLD})")

    articles = load_news(langs=langs)
    dist = Counter(a["attr"] for a in articles)
    print(f"전체: {len(articles)}건")
    for attr, cnt in dist.most_common():
        mark = " ◀" if (args.attr and attr in args.attr) else ""
        print(f"  {attr}: {cnt}{mark}")

    target = args.attr or ATTRS_KR

    print(f"\nLoading SBERT model: {SBERT_MODEL} ...")
    import torch
    _device = "cuda" if torch.cuda.is_available() else "cpu"
    CLUSTER_BACKEND = resolve_cluster_backend(args.backend, torch.cuda.is_available())
    DETERMINISTIC_CLUSTERING = args.deterministic
    USE_KEYBERT_REPRESENTATION = not args.no_keybert
    print(f"UMAP/HDBSCAN backend: {CLUSTER_BACKEND}"
          f"{' (deterministic)' if DETERMINISTIC_CLUSTERING else ''}\n",
          flush=True)
    print(f"KeyBERT representation: {'on' if USE_KEYBERT_REPRESENTATION else 'off'}")
    print(f"Kiwi workers: {KIWI_WORKERS}\n", flush=True)
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
        # 클러스터링 단계는 항상 GPT 라벨 스킵(키워드 임시 라벨). GPT 라벨은 --label-only.
        res = process_attr(articles, attr, sbert, skip_gpt_label=True)
        if res:
            results[attr] = res

    # ── DuckDB: article→topic 매핑 저장 ──
    write_assignments_to_db(results)

    # JSON에는 assignments(대량) 빼고 저장. 대표문서는 유지 → --label-only 가 사용.
    for d in results.values():
        d.pop("assignments", None)
    clean_for_save(results, keep_repr=True)

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
