"""소주제(BERTopic) 결과 수동 후처리 — 토픽 제거 + 라벨 손질 (KO 단독).

클러스터링·라벨링·그룹화(`subtopic_bertopic.py`)가 끝난 정본 위에 사람이 손으로
마지막 손질을 가하는 단계. 두 가지만 한다:

  1) DROP    — stage-1(속성 분류) 오탐으로 끌려온 비-AI정책 토픽을 통째로 제거
               (예: 항공사고, AI주 차명거래 등 "AI를 지워도 성립하는" 토픽)
  2) RELABEL — centroid 라벨이 어색한 토픽/그룹의 ko·en 라벨을 직접 교정

설계 원칙 (이 파일 상단 DIRECTIVES 블록이 유일한 진실 소스):
  - 선언적   : 무엇을 지우고 무엇을 고칠지는 전부 아래 dict/list에 적는다. 로직 수정 불필요.
  - 비파괴   : 원본 `subtopics_bertopic.json`(클러스터링 산출물)은 건드리지 않는다.
               기본 출력은 별도 파일 `subtopics_bertopic_curated.json`.
  - 멱등/재현: 항상 원본에서 출발해 directives 를 다시 적용 → 몇 번 돌려도 같은 결과.
  - 소비자 계약: 모든 소비자가 "토픽 정의 = JSON, 멤버십 = DB 최신 run" 을 따르므로
               JSON 에서 토픽을 빼면 그 토픽 기사는 어디에도 렌더되지 않는다(아웃라이어로도 안 감).
               → DB 는 손대지 않는다. 제거된 기사는 감사를 위해 raw run 에 그대로 남는다.

실행 환경: 정본은 `.venv` (Python 3.12, duckdb 1.5.x). 예) `.venv/bin/python analyze/curate_subtopics.py ...`

대상 토픽 특정 (옛 run 의 topic_id 는 현재 정본과 다름 — 반드시 현재 run 에서 재특정):
  .venv/bin/python analyze/curate_subtopics.py find 제주항공          # 제목에 '제주항공' 포함 기사가 몰린 (속성,tid)
  .venv/bin/python analyze/curate_subtopics.py inspect 시장경쟁/독과점  # 한 속성의 토픽 전체 목록(tid·건수·그룹·라벨)
  .venv/bin/python analyze/curate_subtopics.py show 시장경쟁/독과점 2   # 특정 토픽의 기사 제목 표본

확인 후 아래 DROP_TOPICS / RELABEL_TOPIC / RELABEL_GROUP / TOPIC_OVERRIDES 에 채워넣고:
  .venv/bin/python analyze/curate_subtopics.py --dry-run            # 변경 내역만 출력, 파일 미작성
  .venv/bin/python analyze/curate_subtopics.py                      # = apply, curated 파일 작성
  .venv/bin/python analyze/curate_subtopics.py --in-place           # 원본 백업 후 정본 자체를 교체
  .venv/bin/python analyze/curate_subtopics.py reset                # --in-place 교체를 백업에서 복원
"""
import _bootstrap  # noqa: F401  (repo root 를 sys.path 에 추가)
import os, json, argparse, copy
from collections import defaultdict
from datetime import datetime
import duckdb

import config

# ─────────────────────────────────────────────────────────────────────────────
# DIRECTIVES — 수동 후처리 지시. 여기만 편집한다.
# ─────────────────────────────────────────────────────────────────────────────

# 통째로 제거할 토픽. (속성, topic_id, "사유 메모").
#   - 사유는 기록·감사용. 왜 지웠는지 반드시 남길 것.
#   - ⚠ topic_id 는 *현재 정본 run* 기준. 먼저 find/inspect/show 로 확인하고 채울 것.
DROP_TOPICS = [
    ("AI안전", 3, "AI 의료기기 보안 시험 — 협소 기술 테스트, 안보 담론 핵심 아님 (사용자 지정)"),
    ("산업정책", 1, "대학 AI 인재 양성 — 대학 학과·산학협력·교육과정 보도 중심, AI 정책 담론 밖 (사용자 지정)"),
    ("산업정책", 8, "AI 스마트 가전 경쟁 — 제품/시장 마케팅성 (사용자 지정)"),
    ("산업정책", 29, "산업 AI 인재 양성 — 대학/취업 홍보성, gid=1 인재양성 그룹 (사용자 지정)"),
    ("산업정책", 48, "AI 및 첨단기술 교육 확대 — 입시/모집 보도성, gid=1 인재양성 그룹 (사용자 지정)"),
    ("시장경쟁/독과점", 3, "AI 정책 내 내부자 거래 의혹 (이춘석 주식 차명거래) — 인물 정치사건, AI 무관 stage-1 오탐 (사용자 지정)"),
]

# 토픽 라벨 교정. (속성, topic_id) -> {"ko": "...", "en": "..."}.
#   ko/en 중 준 것만 덮어쓴다.
RELABEL_TOPIC = {
    # ("산업정책", 5): {"ko": "AI 반도체 공급망·국산화", "en": "AI semiconductor supply chain"},
}

# 묶음 그룹의 상위(umbrella) 라벨 교정. (속성, group_id) -> {"ko": "...", "en": "..."}.
#   그룹의 모든 멤버 토픽에 동일하게 기록(대표 토픽에만 있던 값도 통일).
RELABEL_GROUP = {
    # ("시장경쟁/독과점", 33): {"ko": "AI·가상자산 투자 시장", "en": "AI & crypto investment market"},
}

# 토픽별 필드 직접 오버라이드 — 수작업 JSON 편집(그룹 통합·group_label 손질·라벨 교정 등)을
# 재현 가능하게 박아둔 일반 directive. (속성, topic_id) -> {필드: 값}. raw 위에 그대로 덮어씀.
#   - group_id 통합(merge), group_label_ko/en, label_ko/en 모두 이걸로 표현 가능.
#   - DROP/RELABEL 이후 *마지막*에 적용되어 최종값을 보장. group_id 변경 시 n_groups 자동 재계산.
#   - ⚠ 이 블록은 `현재 JSON ↔ raw` diff 로 자동 생성됨(working 의 diff 생성기). 손으로 JSON 을
#     더 고쳤다면 그 diff 를 다시 떠서 여기 갱신해야 raw→현재 재현이 유지됨.
TOPIC_OVERRIDES = {
    # 책임/윤리AI — 사용자 group_label 손질(t0·t10) + 통합 3건(gid9/10/11)
    ("책임/윤리AI", 0):  {"group_label_ko": "AI 생성 딥페이크 성범죄", "group_label_en": "AI-Generated Deepfake Sexual Crimes"},
    ("책임/윤리AI", 1):  {"group_id": 9,  "group_label_ko": "AI 윤리·거버넌스", "group_label_en": "AI ethics & governance"},
    ("책임/윤리AI", 2):  {"group_id": 9,  "group_label_ko": "AI 윤리·거버넌스", "group_label_en": "AI ethics & governance"},
    ("책임/윤리AI", 4):  {"group_id": 10, "group_label_ko": "AI 생성 딥페이크·허위정보", "group_label_en": "AI-generated deepfakes & disinformation"},
    ("책임/윤리AI", 5):  {"group_id": 10, "group_label_ko": "AI 생성 딥페이크·허위정보", "group_label_en": "AI-generated deepfakes & disinformation"},
    ("책임/윤리AI", 7):  {"group_id": 11, "group_label_ko": "AI 편향·공정성", "group_label_en": "AI bias & fairness"},
    ("책임/윤리AI", 8):  {"group_id": 10, "group_label_ko": "AI 생성 딥페이크·허위정보", "group_label_en": "AI-generated deepfakes & disinformation"},
    ("책임/윤리AI", 10): {"group_label_ko": "AI와 예술적 창의성", "group_label_en": "AI and Artistic Creativity"},
    ("책임/윤리AI", 12): {"group_id": 9,  "group_label_ko": "AI 윤리·거버넌스", "group_label_en": "AI ethics & governance"},
    ("책임/윤리AI", 16): {"group_id": 11, "group_label_ko": "AI 편향·공정성", "group_label_en": "AI bias & fairness"},
}

# ─────────────────────────────────────────────────────────────────────────────
# 경로
# ─────────────────────────────────────────────────────────────────────────────
RAW_PATH     = os.path.join(config.ANALYSIS_DIR, "subtopics_bertopic.json")
CURATED_PATH = os.path.join(config.ANALYSIS_DIR, "subtopics_bertopic_curated.json")
BACKUP_PATH  = os.path.join(config.ANALYSIS_DIR, "subtopics_bertopic.raw.json")   # --in-place 시 원본 백업
LOG_PATH     = os.path.join(config.ANALYSIS_DIR, "subtopics_curation_log.json")


# ─────────────────────────────────────────────────────────────────────────────
# 공통 헬퍼
# ─────────────────────────────────────────────────────────────────────────────
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_raw():
    """원본 토픽 정의를 읽는다. --in-place 백업이 있으면 그게 진짜 원본."""
    src = BACKUP_PATH if os.path.exists(BACKUP_PATH) else RAW_PATH
    with open(src, encoding="utf-8") as f:
        return json.load(f), src


def topic_label(t):
    """표시용 라벨 — ko 라벨 우선, 없으면 키워드 상위어."""
    return t.get("label_ko") or ", ".join(t.get("keywords", [])[:5]) or f"(tid {t['topic_id']})"


def is_attr_block(info):
    return isinstance(info, dict) and "topics" in info


def _find_topic(d, attr, tid):
    info = d.get(attr)
    if not is_attr_block(info):
        return None
    for t in info["topics"]:
        if t["topic_id"] == tid:
            return t
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 핵심: directives 적용
# ─────────────────────────────────────────────────────────────────────────────
def apply_curation(raw):
    """raw(dict)에 DROP/RELABEL 적용. (curated, changelog) 반환. raw 는 변형하지 않음."""
    cur = copy.deepcopy(raw)
    log = {"dropped": [], "relabel_topic": [], "relabel_group": [], "overrides": [], "warnings": []}

    drop_memo = {(a, t): memo for a, t, memo in DROP_TOPICS}
    drop_set = set(drop_memo)

    # 1) 토픽 제거
    #    집계는 delta 로만 조정한다(원본 n_articles = 클러스터 기사 + 아웃라이어 이므로
    #    sum(count) 로 재계산하면 아웃라이어가 빠져 0건 제거에도 값이 어긋남). 제거가 있을
    #    때만 갱신 → 제거 0건이면 원본 집계 그대로 보존(진짜 no-op).
    for attr, info in cur.items():
        if not is_attr_block(info):
            continue
        kept, removed = [], []
        for t in info["topics"]:
            key = (attr, t["topic_id"])
            if key in drop_set:
                removed.append(t)
                log["dropped"].append({
                    "attr": attr, "topic_id": t["topic_id"],
                    "count": t.get("count", 0), "label": topic_label(t),
                    "memo": drop_memo[key],
                })
            else:
                kept.append(t)
        info["topics"] = kept
        if removed:
            info["n_topics"]   = len(kept)
            info["n_articles"] = info["n_articles"] - sum(t.get("count", 0) for t in removed)
            info["n_groups"]   = len({t.get("group_id", t["topic_id"]) for t in kept})
            # n_outliers 는 보존(제거 != 아웃라이어)

    seen = {(d["attr"], d["topic_id"]) for d in log["dropped"]}
    for a, t in drop_set - seen:
        log["warnings"].append(f"DROP 미적용: ({a}, {t}) — 현재 정본에 없는 (속성,tid)")

    # 2) 토픽 라벨 교정
    for (attr, tid), lab in RELABEL_TOPIC.items():
        hit = _find_topic(cur, attr, tid)
        if hit is None:
            log["warnings"].append(f"RELABEL_TOPIC 미적용: ({attr}, {tid}) 없음")
            continue
        before = topic_label(hit)
        if "ko" in lab:
            hit["label_ko"] = lab["ko"]
        if "en" in lab:
            hit["label_en"] = lab["en"]
        log["relabel_topic"].append({"attr": attr, "topic_id": tid,
                                     "before": before, "after": lab})

    # 3) 그룹 라벨 교정 — 그룹 모든 멤버에 동일 기록
    for (attr, gid), lab in RELABEL_GROUP.items():
        info = cur.get(attr)
        members = [t for t in info["topics"] if t.get("group_id") == gid] if is_attr_block(info) else []
        if not members:
            log["warnings"].append(f"RELABEL_GROUP 미적용: ({attr}, group {gid}) 멤버 없음")
            continue
        for t in members:
            if "ko" in lab:
                t["group_label_ko"] = lab["ko"]
            if "en" in lab:
                t["group_label_en"] = lab["en"]
        log["relabel_group"].append({"attr": attr, "group_id": gid,
                                     "members": len(members), "after": lab})

    # 4) 토픽별 필드 오버라이드 (수작업 편집 재현) — 최종 적용
    touched = set()
    for (attr, tid), fields in TOPIC_OVERRIDES.items():
        hit = _find_topic(cur, attr, tid)
        if hit is None:
            log["warnings"].append(f"TOPIC_OVERRIDES 미적용: ({attr}, {tid}) 없음(드롭됨?)")
            continue
        for k, v in fields.items():
            hit[k] = v
        touched.add(attr)
        log["overrides"].append({"attr": attr, "topic_id": tid, "fields": fields})
    # group_id 가 바뀐 속성은 n_groups 재계산 (raw 에서 n_groups == distinct group_id 확인됨)
    for attr in touched:
        info = cur.get(attr)
        if is_attr_block(info):
            info["n_groups"] = len({t.get("group_id", t["topic_id"]) for t in info["topics"]})

    return cur, log


def has_changes(log):
    return bool(log["dropped"] or log["relabel_topic"] or log["relabel_group"] or log["overrides"])


# ─────────────────────────────────────────────────────────────────────────────
# 변경 미리보기 출력
# ─────────────────────────────────────────────────────────────────────────────
def print_diff(raw, cur, log):
    print("=" * 72)
    print(f"  소주제 수동 후처리 미리보기  ({_now()})")
    print("=" * 72)

    if log["dropped"]:
        print(f"\n■ 제거 토픽 {len(log['dropped'])}개:")
        for d in sorted(log["dropped"], key=lambda x: -x["count"]):
            print(f"   - [{d['attr']}] tid={d['topic_id']:>3}  ({d['count']:,}건)  {d['label']}")
            print(f"       └ 사유: {d['memo']}")
    else:
        print("\n■ 제거 토픽: 없음")

    if log["relabel_topic"]:
        print(f"\n■ 토픽 라벨 교정 {len(log['relabel_topic'])}개:")
        for r in log["relabel_topic"]:
            print(f"   - [{r['attr']}] tid={r['topic_id']}")
            print(f"       {r['before']!r}  ->  {r['after']}")

    if log["relabel_group"]:
        print(f"\n■ 그룹 라벨 교정 {len(log['relabel_group'])}개:")
        for r in log["relabel_group"]:
            print(f"   - [{r['attr']}] group {r['group_id']} (멤버 {r['members']})  ->  {r['after']}")

    if log["overrides"]:
        print(f"\n■ 토픽 필드 오버라이드 {len(log['overrides'])}개 (통합·라벨 수작업 재현):")
        for r in log["overrides"]:
            print(f"   - [{r['attr']}] tid={r['topic_id']}  {r['fields']}")

    if log["warnings"]:
        print(f"\n⚠ 경고 {len(log['warnings'])}건:")
        for w in log["warnings"]:
            print(f"   - {w}")

    print("\n■ 속성별 집계 (토픽수 · 기사수):")
    for attr in raw:
        if not is_attr_block(raw[attr]):
            continue
        rt, ct = raw[attr]["n_topics"], cur[attr]["n_topics"]
        ra, ca = raw[attr]["n_articles"], cur[attr]["n_articles"]
        mark = "   <-변경" if (rt != ct or ra != ca) else ""
        print(f"   {attr:14s}  토픽 {rt:>3}->{ct:<3}  기사 {ra:>6,}->{ca:<6,}{mark}")

    tot_drop = sum(d["count"] for d in log["dropped"])
    print(f"\n  총 제거 기사: {tot_drop:,}  |  라벨 교정 "
          f"{len(log['relabel_topic']) + len(log['relabel_group'])}  ·  오버라이드 {len(log['overrides'])}")
    print("=" * 72)


# ─────────────────────────────────────────────────────────────────────────────
# 쓰기
# ─────────────────────────────────────────────────────────────────────────────
def _copy_file(src, dst):
    with open(src, encoding="utf-8") as f:
        data = f.read()
    with open(dst, "w", encoding="utf-8") as f:
        f.write(data)


def write_curated(cur, log, in_place):
    if in_place:
        if not os.path.exists(BACKUP_PATH):           # 최초 1회만 원본 백업
            _copy_file(RAW_PATH, BACKUP_PATH)
            print(f"원본 백업 -> {BACKUP_PATH}")
        out = RAW_PATH
    else:
        out = CURATED_PATH
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump({"applied_at": _now(), "source_run": "subtopic_assignments latest", **log},
                  f, ensure_ascii=False, indent=2)
    print(f"\n작성: {out}")
    print(f"로그: {LOG_PATH}")
    if not in_place:
        print("  ↳ 소비자(export_ko_lists 등)가 이 curated 파일을 읽도록 연결하거나, "
              "--in-place 로 정본 자체를 교체하세요.")


def cmd_reset():
    """--in-place 로 덮어쓴 정본을 원본 백업에서 복원."""
    if not os.path.exists(BACKUP_PATH):
        print("복원할 백업이 없습니다 (subtopics_bertopic.raw.json).")
        return
    _copy_file(BACKUP_PATH, RAW_PATH)
    os.remove(BACKUP_PATH)
    print(f"복원 완료: {BACKUP_PATH} -> {RAW_PATH} (백업 삭제)")


# ─────────────────────────────────────────────────────────────────────────────
# 읽기 전용 조사 도구 (대상 특정용)
# ─────────────────────────────────────────────────────────────────────────────
def _db():
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH, read_only=True)
    run = con.execute("SELECT max(run_timestamp) FROM subtopic_assignments").fetchone()[0]
    return con, run


def cmd_find(query):
    """제목에 query 가 포함된 기사가 어느 (속성, topic_id) 에 몰려 있는지."""
    con, run = _db()
    rows = con.execute("""
        SELECT a.attr, a.topic_id, count(*) c
        FROM subtopic_assignments a
        JOIN news_articles n ON a.article_id = ('kr:' || n.news_id)
        WHERE a.run_timestamp = ? AND a.lang = 'ko'
          AND n.title LIKE '%' || ? || '%'
        GROUP BY a.attr, a.topic_id ORDER BY c DESC LIMIT 30
    """, [run, query]).fetchall()
    con.close()
    raw, _ = load_raw()
    total = sum(c for *_, c in rows)
    print(f"'{query}' 포함 기사 {total}건 분포 (run {run}):")
    if not rows:
        print("  (해당 제목 없음)")
        return
    for attr, tid, c in rows:
        t = _find_topic(raw, attr, tid)
        lab = topic_label(t) if t else ("아웃라이어" if tid == -1 else f"tid {tid}")
        print(f"  [{attr}] tid={tid:>3}  {c:>4}건  {lab}")


def cmd_inspect(attr):
    """한 속성의 토픽 전체 목록 (그룹 묶어서, 기사수 내림차순)."""
    raw, _ = load_raw()
    info = raw.get(attr)
    if not is_attr_block(info):
        print(f"속성 없음: {attr}\n사용 가능: {', '.join(raw.keys())}")
        return
    g = defaultdict(list)
    for t in info["topics"]:
        g[t.get("group_id", t["topic_id"])].append(t)
    print(f"[{attr}]  토픽 {info['n_topics']} · 기사 {info['n_articles']:,} · "
          f"그룹 {info.get('n_groups')} · 아웃 {info['n_outliers']:,}")
    order = sorted(g.items(), key=lambda kv: -sum(t.get("count", 0) for t in kv[1]))
    for gid, ts in order:
        ts = sorted(ts, key=lambda x: -x.get("count", 0))
        if len(ts) > 1:
            gl = next((t.get("group_label_ko") for t in ts if t.get("group_label_ko")), "")
            tot = sum(t.get("count", 0) for t in ts)
            print(f"  ▣ group {gid}  [{tot:,}건]  {gl}")
            for t in ts:
                print(f"      tid={t['topic_id']:>3}  ({t.get('count',0):,}건)  {topic_label(t)}")
        else:
            t = ts[0]
            print(f"  · tid={t['topic_id']:>3}  ({t.get('count',0):,}건)  {topic_label(t)}")


def cmd_show(attr, tid, n):
    """특정 토픽의 기사 제목 표본."""
    con, run = _db()
    rows = con.execute("""
        SELECT n.published_at, a.source, n.title
        FROM subtopic_assignments a
        JOIN news_articles n ON a.article_id = ('kr:' || n.news_id)
        WHERE a.run_timestamp = ? AND a.lang = 'ko' AND a.attr = ? AND a.topic_id = ?
        ORDER BY n.published_at LIMIT ?
    """, [run, attr, tid, n]).fetchall()
    con.close()
    raw, _ = load_raw()
    t = _find_topic(raw, attr, tid)
    print(f"[{attr}] tid={tid}  {topic_label(t) if t else ''}  (표본 {len(rows)})")
    for pub, src, title in rows:
        date = str(pub)[:10] if pub else ""
        print(f"  - {date} [{src}] {(title or '').strip()[:90]}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="소주제 수동 후처리 (제거 + 라벨 교정)")
    sub = p.add_subparsers(dest="cmd")

    pa = sub.add_parser("apply", help="directives 적용 후 curated 작성 (기본)")
    pa.add_argument("--dry-run", action="store_true", help="변경 내역만 출력, 파일 미작성")
    pa.add_argument("--in-place", action="store_true", help="원본 백업 후 정본 파일 직접 교체")

    pf = sub.add_parser("find", help="제목 검색으로 대상 토픽 위치 찾기")
    pf.add_argument("query")

    pi = sub.add_parser("inspect", help="한 속성의 토픽 전체 목록")
    pi.add_argument("attr")

    ps = sub.add_parser("show", help="특정 토픽의 기사 제목 표본")
    ps.add_argument("attr")
    ps.add_argument("topic_id", type=int)
    ps.add_argument("n", nargs="?", type=int, default=25)

    sub.add_parser("reset", help="--in-place 교체를 백업에서 복원")

    # apply 의 플래그를 최상위에서도 받도록(인자 없이/--dry-run 만으로 호출 가능)
    p.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--in-place", action="store_true", help=argparse.SUPPRESS)

    args = p.parse_args()
    cmd = args.cmd or "apply"

    if cmd == "find":
        cmd_find(args.query)
    elif cmd == "inspect":
        cmd_inspect(args.attr)
    elif cmd == "show":
        cmd_show(args.attr, args.topic_id, args.n)
    elif cmd == "reset":
        cmd_reset()
    else:  # apply
        dry = getattr(args, "dry_run", False)
        in_place = getattr(args, "in_place", False)
        raw, src = load_raw()
        print(f"원본: {src}")
        cur, log = apply_curation(raw)
        print_diff(raw, cur, log)
        if dry:
            print("\n[--dry-run] 파일을 작성하지 않았습니다.")
            return
        if not has_changes(log):
            print("\n변경 사항이 없습니다. DROP_TOPICS / RELABEL_* 를 채운 뒤 다시 실행하세요.")
            print("대상 특정: find <검색어> · inspect <속성> · show <속성> <tid>")
            return
        write_curated(cur, log, in_place)

        from export_subtopic_lists import write_ko_lists, write_combined_lists
        json_for_md = RAW_PATH if in_place else CURATED_PATH
        write_ko_lists(json_for_md, os.path.join(config.OUTPUT_DIR, "article_lists_ko.md"))
        write_combined_lists(json_for_md, os.path.join(config.OUTPUT_DIR, "article_lists_by_subtopic.md"))


if __name__ == "__main__":
    main()
