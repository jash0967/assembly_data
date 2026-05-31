"""Shared loaders for KR/US/EU bill classification + metadata.

Phase 5: classification rows live in DuckDB (bill_classifications,
bill_ai_filter). This module is now a thin wrapper that reads from DB
and joins on-disk source metadata where applicable.

Public signatures (preserved across the migration):
- load_kr_bills(ages=(19,20,21,22), enrich=True) -> list[dict]
- load_us_bills(congresses=(118,119), enrich=True) -> list[dict]
- load_eu_bills(include=("act","amendments"), enrich=True) -> list[dict]

Returned dict shape is identical to the pre-migration version so that
analyze/make_figures.py and validators don't need changes.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

import duckdb

import config

ROOT = os.path.dirname(os.path.abspath(__file__))
REP_DIR = os.path.join(ROOT, "replicate_carvao", "data")


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _connect_ro() -> duckdb.DuckDBPyConnection:
    """Open analysis DB read-only and ATTACH raw read-only.

    All v_kr_bills_analysis-style cross-DB views resolve via this connection.
    """
    con = duckdb.connect(config.ANALYSIS_DB_PATH, read_only=True)
    con.execute(f"ATTACH '{config.RAW_DB_PATH}' AS raw (READ_ONLY)")
    return con


# =============================================================================
# KR
# =============================================================================

def load_kr_bills(ages: Iterable[int] = (19, 20, 21, 22),
                  enrich: bool = True) -> list[dict]:
    """KR 분류 결과 + 메타데이터 결합 (v_kr_bills_analysis 경유).

    Returns list of dicts with keys:
        primary, secondary, tertiary, id, title, age,
        (enrich=True) propose_date, proposer, committee
    """
    age_list = list(ages)
    if not age_list:
        return []

    placeholders = ", ".join("?" * len(age_list))
    cols = ["primary_attr", "secondary_attr", "tertiary_attr",
            "bill_id", "classified_title", "bill_name", "age"]
    if enrich:
        cols += ["propose_date", "lead_proposer", "committee"]

    sql = f"""
        SELECT {', '.join(cols)}
        FROM v_kr_bills_analysis
        WHERE primary_attr IS NOT NULL
          AND age IN ({placeholders})
        ORDER BY age, propose_date
    """

    con = _connect_ro()
    try:
        rows = con.execute(sql, age_list).fetchall()
    finally:
        con.close()

    out = []
    for row in rows:
        record = dict(zip(cols, row))
        item = {
            "primary":   record.get("primary_attr") or "",
            "secondary": record.get("secondary_attr") or "",
            "tertiary":  record.get("tertiary_attr") or "",
            "id":        record["bill_id"],
            "title":     record.get("classified_title") or record.get("bill_name") or "",
            "age":       record["age"],
        }
        if enrich:
            item["propose_date"] = str(record.get("propose_date") or "")
            item["proposer"]     = record.get("lead_proposer") or ""
            item["committee"]    = record.get("committee") or ""
        out.append(item)
    return out


# =============================================================================
# US
# =============================================================================

def load_us_bills(congresses: Iterable[int] = (118, 119),
                  enrich: bool = True) -> list[dict]:
    """US 분류 결과 + 메타데이터 결합. 메타는 replicate_carvao/data/ JSON 파일에서.

    Returns list of dicts (keys identical to pre-migration version).
    """
    cg_list = list(congresses)
    if not cg_list:
        return []

    sources = [f"us_{cg}" for cg in cg_list]
    placeholders = ", ".join("?" * len(sources))

    sql = f"""
        SELECT bill_id, primary_attr, secondary_attr, tertiary_attr,
               title, source
        FROM v_bill_classifications_current
        WHERE source IN ({placeholders})
    """
    con = _connect_ro()
    try:
        rows = con.execute(sql, sources).fetchall()
    finally:
        con.close()

    meta_files = {118: "bills_processed.json", 119: "us119_bills_processed.json"}
    meta_by_cg: dict[int, dict[str, dict]] = {}
    if enrich:
        for cg in cg_list:
            data = _read_json(os.path.join(REP_DIR, meta_files.get(cg, "")))
            meta_by_cg[cg] = (
                {m.get("bill_id", ""): m for m in data} if data else {}
            )

    out = []
    for bill_id, p, s, t, title, source in rows:
        cg = int(source.split("_", 1)[1])
        m = meta_by_cg.get(cg, {}).get(bill_id, {})
        out.append({
            "primary":   p or "",
            "secondary": s or "",
            "tertiary":  t or "",
            "id":        bill_id,
            "title":     title or m.get("title", ""),
            "congress":  cg,
            "sponsor_name":     m.get("sponsor_name", ""),
            "sponsor_party":    m.get("sponsor_party", ""),
            "sponsor_state":    m.get("sponsor_state", ""),
            "introduced_date":  m.get("introduced_date", ""),
            "chamber":          m.get("chamber", ""),
            "bipartisan":       m.get("bipartisan", m.get("bipartisan_category", "")),
            "cosponsor_count":  m.get("cosponsor_count", 0),
            "stage":            m.get("stage", ""),
            "primary_committee": m.get("primary_committee", ""),
        })
    return out


# =============================================================================
# EU
# =============================================================================

def load_eu_bills(include: Iterable[str] = ("act", "amendments"),
                  enrich: bool = True) -> list[dict]:
    """EU AI Act 조문 + 수정안 분류 결과 + 메타데이터 결합.

    Returns list of dicts (keys identical to pre-migration version).
    """
    include = tuple(include)

    article_meta: dict[str, dict] = {}
    amend_meta: dict[str, dict] = {}
    if enrich:
        if "act" in include:
            data = _read_json(os.path.join(config.BILLS_EU_DIR, "eu_ai_act_articles.json"))
            if data:
                article_meta = {str(a.get("article_num", "")): a for a in data}
        if "amendments" in include:
            data = _read_json(os.path.join(config.BILLS_EU_DIR, "eu_amendments.json"))
            if data:
                amend_meta = {str(a.get("amendment_num", "")): a for a in data}

    sources = []
    if "act" in include:
        sources.append("eu_act")
    if "amendments" in include:
        sources.append("eu_amendments")
    if not sources:
        return []

    placeholders = ", ".join("?" * len(sources))
    sql = f"""
        SELECT bill_id, primary_attr, secondary_attr, tertiary_attr, title, source
        FROM v_bill_classifications_current
        WHERE source IN ({placeholders})
    """
    con = _connect_ro()
    try:
        rows = con.execute(sql, sources).fetchall()
    finally:
        con.close()

    out = []
    for bill_id, p, s, t, title, source in rows:
        if source == "eu_act":
            m = article_meta.get(bill_id, {})
            out.append({
                "primary":   p or "",
                "secondary": s or "",
                "tertiary":  t or "",
                "id":        bill_id,
                "title":     title or m.get("title", ""),
                "type":      "article",
                "article_num": bill_id,
            })
        else:  # eu_amendments
            m = amend_meta.get(bill_id, {})
            out.append({
                "primary":   p or "",
                "secondary": s or "",
                "tertiary":  t or "",
                "id":        bill_id,
                "title":     title or m.get("target", ""),
                "type":      "amendment",
                "amendment_num": bill_id,
                "target":    title or m.get("target", ""),
            })
    return out
