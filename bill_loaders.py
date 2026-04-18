"""Shared loaders for KR/US/EU bill classification + metadata.

소비자들이 `data/bills_classified_*.json`을 직접 읽는 대신 이 모듈을 거치게 해서,
`age`/`congress`/`type` 같은 필터용 메타데이터와 원본 metadata를 한 번에 결합.

어댑터 파일(`*_policy_attr_all.json`)을 없애고 여기에 기능을 일원화.
"""
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
REP_DIR = os.path.join(ROOT, "replicate_carvao", "data")


def _read_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# KR
# =============================================================================

def load_kr_bills(ages=(19, 20, 21, 22), enrich=True):
    """KR 19~22대 분류 결과 + 메타데이터 결합.

    Args:
        ages: 포함할 대수 (기본 19, 20, 21, 22)
        enrich: True면 bill_txt_{age}/*.json에서 propose_date·proposer·committee 조인

    Returns: list of dicts with keys:
        primary, secondary, tertiary, id, title, age,
        (enrich=True: propose_date, proposer, committee)
    """
    out = []
    for age in ages:
        cls = _read_json(os.path.join(DATA_DIR, f"bills_classified_kr_{age}.json"))
        if not cls:
            continue
        txt_dir = os.path.join(DATA_DIR, f"bill_txt_{age}") if enrich else None
        for c in cls:
            item = {
                "primary": c.get("primary", ""),
                "secondary": c.get("secondary", ""),
                "tertiary": c.get("tertiary", ""),
                "id": c.get("id", ""),
                "title": c.get("title", ""),
                "age": age,
            }
            if enrich and txt_dir:
                tpath = os.path.join(txt_dir, f"{item['id']}.json")
                if os.path.exists(tpath):
                    with open(tpath, encoding="utf-8") as f:
                        meta = json.load(f)
                    item["propose_date"] = meta.get("propose_date", "")
                    item["proposer"] = meta.get("proposer", "")
                    item["committee"] = meta.get("committee", "")
                    if not item["title"]:
                        item["title"] = meta.get("bill_name", "")
            out.append(item)
    return out


# =============================================================================
# US
# =============================================================================

def load_us_bills(congresses=(118, 119), enrich=True):
    """US 118+119대 분류 결과 + 메타데이터 결합.

    Args:
        congresses: 포함할 congress (기본 118, 119)
        enrich: True면 {,us119_}bills_processed.json에서 sponsor·date·chamber 조인

    Returns: list of dicts with keys:
        primary, secondary, tertiary, id (bill_id), title, congress,
        (enrich=True: sponsor_name, sponsor_party, sponsor_state, introduced_date,
                     chamber, bipartisan, cosponsor_count, stage, primary_committee)
    """
    meta_files = {
        118: "bills_processed.json",
        119: "us119_bills_processed.json",
    }
    out = []
    for cg in congresses:
        cls = _read_json(os.path.join(DATA_DIR, f"bills_classified_us_{cg}.json"))
        if not cls:
            continue
        meta_by_id = {}
        if enrich:
            meta = _read_json(os.path.join(REP_DIR, meta_files.get(cg, "")))
            if meta:
                for m in meta:
                    meta_by_id[m.get("bill_id", "")] = m
        for c in cls:
            m = meta_by_id.get(c.get("id", ""), {})
            out.append({
                "primary": c.get("primary", ""),
                "secondary": c.get("secondary", ""),
                "tertiary": c.get("tertiary", ""),
                "id": c.get("id", ""),
                "title": c.get("title", "") or m.get("title", ""),
                "congress": cg,
                "sponsor_name": m.get("sponsor_name", ""),
                "sponsor_party": m.get("sponsor_party", ""),
                "sponsor_state": m.get("sponsor_state", ""),
                "introduced_date": m.get("introduced_date", ""),
                "chamber": m.get("chamber", ""),
                "bipartisan": m.get("bipartisan", m.get("bipartisan_category", "")),
                "cosponsor_count": m.get("cosponsor_count", 0),
                "stage": m.get("stage", ""),
                "primary_committee": m.get("primary_committee", ""),
            })
    return out


# =============================================================================
# EU
# =============================================================================

def load_eu_bills(include=("act", "amendments"), enrich=True):
    """EU AI Act 조문 + 수정안 분류 결과 + 메타데이터 결합.

    Args:
        include: "act", "amendments" 중 선택
        enrich: True면 eu_ai_act_articles.json / eu_amendments.json에서 title/target 조인

    Returns: list of dicts with keys:
        primary, secondary, tertiary, id, title, type ("article"|"amendment"),
        (article: article_num) or (amendment: amendment_num, target)
    """
    out = []
    if "act" in include:
        cls = _read_json(os.path.join(DATA_DIR, "bills_classified_eu_act.json"))
        articles_meta = {}
        if enrich and cls:
            meta = _read_json(os.path.join(DATA_DIR, "eu_ai_act_articles.json"))
            if meta:
                for a in meta:
                    articles_meta[str(a.get("article_num", ""))] = a
        for c in cls or []:
            aid = str(c.get("id", ""))
            m = articles_meta.get(aid, {})
            out.append({
                "primary": c.get("primary", ""),
                "secondary": c.get("secondary", ""),
                "tertiary": c.get("tertiary", ""),
                "id": aid,
                "title": c.get("title", "") or m.get("title", ""),
                "type": "article",
                "article_num": aid,
            })

    if "amendments" in include:
        cls = _read_json(os.path.join(DATA_DIR, "bills_classified_eu_amendments.json"))
        amend_meta = {}
        if enrich and cls:
            meta = _read_json(os.path.join(DATA_DIR, "eu_amendments.json"))
            if meta:
                for a in meta:
                    amend_meta[str(a.get("amendment_num", ""))] = a
        for c in cls or []:
            aid = str(c.get("id", ""))
            m = amend_meta.get(aid, {})
            out.append({
                "primary": c.get("primary", ""),
                "secondary": c.get("secondary", ""),
                "tertiary": c.get("tertiary", ""),
                "id": aid,
                "title": c.get("title", "") or m.get("target", ""),
                "type": "amendment",
                "amendment_num": aid,
                "target": c.get("title", "") or m.get("target", ""),
            })

    return out
