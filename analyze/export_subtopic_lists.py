"""소주제(BERTopic) 분류 완료 후 마크다운 리스트 생성.

두 가지 뷰:
  write_ko_lists(json_path, out_path)        — KO 단독, 그룹·목차 구조
  write_combined_lists(json_path, out_path)  — EN+KO 합산, 토픽별 평면 리스트

두 함수 모두 import 해서 호출 가능 (subtopic_bertopic.py, curate_subtopics.py 에서 사용).
독립 실행:  .venv/bin/python analyze/export_subtopic_lists.py
"""
import _bootstrap  # noqa: F401
import os, json, html as html_mod, re
from collections import defaultdict
import duckdb
import config

ATTRS = ['AI안전', '책임/윤리AI', '산업정책', '공익/소비자보호', '국가안보',
         '선거/민주주의', '시장경쟁/독과점', '노동/고용', '저작권/지식재산', '국제협력']


def _clean(t):
    t = html_mod.unescape(t or "")
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\s+", " ", t).strip()


# ─────────────────────────────────────────────────────────────────
# write_ko_lists — KO 단독, 그룹·목차 구조
# ─────────────────────────────────────────────────────────────────

def write_ko_lists(json_path, out_path, db_path=None):
    """KO 소주제 목록 마크다운. 그룹화 구조(group_id) + 목차 포함."""
    db_path = db_path or config.NEWS_ANALYSIS_DB_PATH
    d = json.load(open(json_path, encoding="utf-8"))

    con = duckdb.connect(db_path, read_only=True)
    run = con.execute("SELECT max(run_timestamp) FROM subtopic_assignments").fetchone()[0]
    rows = con.execute("""
        SELECT a.attr, a.topic_id, a.source, n.title, n.published_at
        FROM subtopic_assignments a
        LEFT JOIN news_articles n ON a.article_id = ('kr:' || n.news_id)
        WHERE a.run_timestamp = ? AND a.lang = 'ko'
    """, [run]).fetchall()
    con.close()

    bucket = defaultdict(list)   # (attr, topic_id) -> [(date, source, title)]
    for attr, tid, source, title, pub in rows:
        date = str(pub)[:10] if pub else ""
        bucket[(attr, tid)].append((date, source, _clean(title) or "(제목없음)"))

    gpt_labeled = sum(
        1 for v in d.values() for t in v["topics"]
        if t.get("label_ko", "").count(",") < 2
    ) > sum(len(v["topics"]) for v in d.values()) / 2

    def label(t):
        lk = t.get("label_ko", "")
        if gpt_labeled and lk and lk.count(",") < 2:
            le = t.get("label_en", "")
            return lk + (f" / {le}" if le else "")
        return ", ".join(t.get("keywords", [])[:6])

    def n_arts(attr, tid):
        return len(bucket.get((attr, tid), []))

    def group_label(ts):
        gk = ts[0].get("group_label_ko", "")
        if gk:
            ge = ts[0].get("group_label_en", "")
            return gk + (f" / {ge}" if ge else "")
        return label(ts[0])

    def groups_of(info, attr):
        g = defaultdict(list)
        for i, t in enumerate(info["topics"]):
            g[t.get("group_id", f"_solo{i}")].append(t)
        out = []
        for ts in g.values():
            ts = sorted(ts, key=lambda x: n_arts(attr, x["topic_id"]), reverse=True)
            tot = sum(n_arts(attr, t["topic_id"]) for t in ts)
            out.append((tot, ts))
        return sorted(out, key=lambda x: x[0], reverse=True)

    grouped = any("group_id" in t for v in d.values() for t in v["topics"])

    L = ["# AI 정책 뉴스 — 속성·그룹·소주제별 기사 제목 (KO 단독 분류)", ""]
    L.append(f"- 분류 run: `{run}` (KO 단독, eom, deterministic, cross-lingual merge 없음)")
    L.append(f"- 토픽 라벨: {'GPT 생성 (한/영)' if gpt_labeled else '키워드 상위어'}")
    if grouped:
        L.append("- 그룹화: centroid average-linkage sim≥0.80 (AI안전 제외)")
    L.append("")

    # 목차
    L.append("## 목차 — 속성 · 그룹 · 토픽\n")
    for attr in ATTRS:
        if attr not in d:
            continue
        info = d[attr]
        ng = info.get("n_groups", info["n_topics"])
        gl = groups_of(info, attr)
        n_multi = sum(1 for _, ts in gl if len(ts) > 1)
        n_solo  = sum(1 for _, ts in gl if len(ts) == 1)
        L.append(f"### {attr}  (기사 {info['n_articles']:,} · 그룹 {ng}"
                 f"[묶음 {n_multi}+단독 {n_solo}] · 토픽 {info['n_topics']} · 아웃 {info['n_outliers']:,})")
        for tot, ts in gl:
            if len(ts) > 1:
                L.append(f"- ▣ **[{tot:,}건] {group_label(ts)}**  _(토픽 {len(ts)})_")
                for t in ts:
                    L.append(f"    - ({n_arts(attr, t['topic_id']):,}건) {label(t)}")
            else:
                L.append(f"- · [단독] ({tot:,}건) {label(ts[0])}")
        L.append("")

    L.append("\n---\n\n# 기사 제목 전체\n")

    grand = 0
    for attr in ATTRS:
        if attr not in d:
            continue
        info = d[attr]
        ng = info.get("n_groups", info["n_topics"])
        L.append(f"\n## {attr}  (기사 {info['n_articles']:,} · 그룹 {ng} · 토픽 {info['n_topics']})")
        for tot, ts in groups_of(info, attr):
            multi = len(ts) > 1
            if multi:
                L.append(f"\n### ▣ [그룹 {tot:,}건 · 토픽 {len(ts)}] {group_label(ts)}")
            for t in ts:
                tid = t["topic_id"]
                kw  = ", ".join(t.get("keywords", [])[:6])
                lab = label(t)
                arts = sorted(bucket.get((attr, tid), []))
                head = "####" if multi else "### ·"
                solo_tag = "" if multi else "[단독] "
                L.append(f"\n{head} {solo_tag}({len(arts)}건) {lab}")
                if lab != kw:
                    L.append(f"*키워드: {kw}*")
                for date, source, title in arts:
                    L.append(f"- {(date + ' ') if date else ''}[{source}] {title}")
                grand += len(arts)
        outs = sorted(bucket.get((attr, -1), []))
        if outs:
            L.append(f"\n### [아웃라이어 미분류] ({len(outs)}건)")
            for date, source, title in outs:
                L.append(f"- {(date + ' ') if date else ''}[{source}] {title}")
            grand += len(outs)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"WROTE {out_path}  ({grand:,}건  {os.path.getsize(out_path)/1024/1024:.1f} MB)")


# ─────────────────────────────────────────────────────────────────
# write_combined_lists — EN+KO 합산, 토픽별 평면 리스트
# ─────────────────────────────────────────────────────────────────

def _load_en_titles():
    m = {}
    gp = os.path.join(config.NEWS_DIR, "guardian_articles_raw.json")
    if os.path.exists(gp):
        for a in json.load(open(gp, encoding="utf-8")):
            title = _clean(a.get("title", "") or a.get("webTitle", ""))
            for key in ("id", "url"):
                aid = a.get(key, "")
                if aid:
                    m[f"guardian:{aid}"] = title
    npath = os.path.join(config.NEWS_DIR, "nyt_articles_raw.json")
    if os.path.exists(npath):
        for a in json.load(open(npath, encoding="utf-8")):
            title = a.get("title", "")
            if not title:
                h = a.get("headline", {})
                title = h.get("main", "") if isinstance(h, dict) else str(h)
            title = _clean(title)
            for key in ("url", "web_url", "_id", "uri"):
                aid = a.get(key, "")
                if aid:
                    m[f"nyt:{aid}"] = title
    return m


def _topic_key_sets(info):
    out = []
    for t in info["topics"]:
        ty = t.get("type", "ko_only")
        if ty == "merged":
            keys = {("en", t["en_topic_id"]), ("ko", t["ko_topic_id"])}
        else:
            lang = "en" if ty == "en_only" else ("ko" if ty == "ko_only" else "mixed")
            keys = {(lang, t.get("topic_id", -1))}
        out.append({
            "type":     ty,
            "label_ko": t.get("label_ko", ""),
            "label_en": t.get("label_en", ""),
            "count":    t.get("count", 0),
            "keys":     keys,
        })
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


def write_combined_lists(json_path, out_path, db_path=None):
    """EN+KO 합산 소주제 목록 마크다운. 토픽 타입 마커(🔗/EN/KO/~) 포함."""
    db_path = db_path or config.NEWS_ANALYSIS_DB_PATH
    d = json.load(open(json_path, encoding="utf-8"))
    en_titles = _load_en_titles()

    con = duckdb.connect(db_path, read_only=True)
    run = con.execute("SELECT max(run_timestamp) FROM subtopic_assignments").fetchone()[0]
    rows = con.execute("""
        SELECT a.attr, a.lang, a.topic_id, a.article_id, a.source,
               n.title, n.published_at
        FROM subtopic_assignments a
        LEFT JOIN news_articles n ON a.article_id = ('kr:' || n.news_id)
        WHERE a.run_timestamp = ?
    """, [run]).fetchall()
    con.close()

    bucket = defaultdict(list)   # (attr, lang, topic_id) -> [(date, source, title, aid)]
    for attr, lang, tid, aid, source, kr_title, published_at in rows:
        if lang == "ko":
            title = _clean(kr_title) or "(제목없음)"
            date  = str(published_at)[:10] if published_at else ""
        else:
            title = en_titles.get(aid, "") or aid
            date  = ""
        bucket[(attr, lang, tid)].append((date, source, title, aid))

    lines = ["# AI 정책 뉴스 — 속성·소주제별 기사 리스트", ""]
    lines.append(f"- 분류 run: `{run}`")
    lines.append(f"- 토픽 정의: `output/analysis/subtopics_bertopic.json`")
    lines.append(f"- 제목 소스: KR=`news_articles.title`, EN=Guardian/NYT 원본 JSON")
    lines.append("")

    grand = 0
    for attr in ATTRS:
        if attr not in d:
            continue
        info   = d[attr]
        topics = _topic_key_sets(info)
        lines.append(f"\n## {attr}  (기사 {info['n_articles']:,} · 토픽 {info['n_topics']})")
        for t in topics:
            arts = []
            for (lang, tid) in t["keys"]:
                arts.extend(bucket.get((attr, lang, tid), []))
            arts.sort()
            sym = {"merged": "🔗", "en_only": "EN", "ko_only": "KO", "mixed": "~"}.get(t["type"], "?")
            lines.append(f"\n### [{sym}] {t['label_ko']} / {t['label_en']}  ({len(arts)}건)")
            for date, source, title, aid in arts:
                lines.append(f"- {(date + ' ') if date else ''}[{source}] {title}")
            grand += len(arts)

        outs = (bucket.get((attr, "ko", -1), []) +
                bucket.get((attr, "en", -1), []) +
                bucket.get((attr, "mixed", -1), []))
        if outs:
            outs.sort()
            lines.append(f"\n### [—] 아웃라이어 (미분류)  ({len(outs)}건)")
            for date, source, title, aid in outs:
                lines.append(f"- {(date + ' ') if date else ''}[{source}] {title}")
            grand += len(outs)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"WROTE {out_path}  ({grand:,}건  {os.path.getsize(out_path)/1024/1024:.1f} MB)")


# ─────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="소주제 마크다운 리스트 생성")
    parser.add_argument("--json", default=os.path.join(config.ANALYSIS_DIR, "subtopics_bertopic.json"),
                        help="토픽 정의 JSON (기본: subtopics_bertopic.json)")
    parser.add_argument("--ko-only", action="store_true", help="KO 리스트만 생성")
    parser.add_argument("--combined-only", action="store_true", help="EN+KO 리스트만 생성")
    a = parser.parse_args()

    out_ko   = os.path.join(config.OUTPUT_DIR, "article_lists_ko.md")
    out_both = os.path.join(config.OUTPUT_DIR, "article_lists_by_subtopic.md")

    if not a.combined_only:
        write_ko_lists(a.json, out_ko)
    if not a.ko_only:
        write_combined_lists(a.json, out_both)
