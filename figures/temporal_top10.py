"""소주제별 상위 10개의 분기별 랭킹 변동 시각화.

Bump chart (Plotly) — 분기별 top-10 랭킹 변화 + 진입/탈락 마커.
Lifespan bar (matplotlib) — 각 토픽이 top-10에 머문 기간 가로 막대.

입력:
  - news_analysis.duckdb :: subtopic_assignments      (article→topic 매핑, 최신 run)
  - output/analysis/subtopics_bertopic.json           (토픽 키워드/라벨)
  - news.duckdb :: news_articles                      (KR 기사 시간)
  - data/news/{guardian,nyt}_articles_raw.json        (영문 기사 시간)

출력:
  - figures/out/temporal_top10_bump.html
  - figures/out/temporal_top10_lifespan.png
"""
import os
import sys
import json
from pathlib import Path
from collections import Counter

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import duckdb
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import plotly.graph_objects as go
from plotly.colors import qualitative

import config


OUT_DIR = Path(config.FIGURES_OUT_DIR)
OUT_DIR.mkdir(parents=True, exist_ok=True)

TOP_N = 10                              # 매 분기 상위 N개
QUARTER_FREQ = "Q"                      # 분기 단위 (M=월, A=년 변경 가능)
MIN_QUARTERS_FOR_DISPLAY = 1            # 한 토픽이 top10 진입한 최소 분기 수


# ───────────────────────────────────────────────────────────────
# 1. 데이터 로딩
# ───────────────────────────────────────────────────────────────
def load_assignments():
    con = duckdb.connect(config.NEWS_ANALYSIS_DB_PATH, read_only=True)
    df = con.execute("""
        SELECT attr, article_id, source, lang, topic_id
        FROM subtopic_assignments
        WHERE run_timestamp = (SELECT MAX(run_timestamp) FROM subtopic_assignments)
    """).df()
    con.close()
    print(f"[1/5] assignments rows: {len(df):,}")
    return df


def build_id_to_date():
    """article_id ('source:native_id' 형식) → published timestamp."""
    id2date = {}

    # KR
    con = duckdb.connect(config.NEWS_DB_PATH, read_only=True)
    for nid, ts in con.execute("SELECT news_id, published_at FROM news_articles").fetchall():
        id2date[f"kr:{nid}"] = ts
    con.close()

    # Guardian (URL = article_id 와 일치)
    with open(os.path.join(config.NEWS_DIR, "guardian_articles_raw.json"), encoding="utf-8") as f:
        g = json.load(f)
    for a in g:
        url = a.get("url") or ""
        pub = a.get("pub_date") or a.get("webPublicationDate") or ""
        if url and pub:
            id2date[f"guardian:{url}"] = pub
        # 경로 ID 변형도 보존 (load_news 가 둘 다 가능하게 했었음)
        pid = a.get("id") or ""
        if pid and pub:
            id2date[f"guardian:https://www.theguardian.com/{pid}"] = pub

    # NYT
    with open(os.path.join(config.NEWS_DIR, "nyt_articles_raw.json"), encoding="utf-8") as f:
        n = json.load(f)
    for a in n:
        pub = a.get("pub_date") or ""
        for key in ("url", "web_url", "_id", "uri"):
            url = a.get(key) or ""
            if url and pub:
                id2date[f"nyt:{url}"] = pub
    print(f"[2/5] id→date entries: {len(id2date):,}")
    return id2date


def load_topic_labels():
    """JSON에서 (attr, lang, topic_id) → 표시 라벨 dict.
       merged 토픽은 동일 라벨을 EN/KO 양쪽에 매핑."""
    with open(os.path.join(config.ANALYSIS_DIR, "subtopics_bertopic.json"), encoding="utf-8") as f:
        bert = json.load(f)
    labels = {}
    for attr, data in bert.items():
        for t in data.get("topics", []):
            ttype = t.get("type", "?")
            kws_ko = t.get("keywords_ko") or t.get("keywords") or []
            kws = [k for k in kws_ko if k and isinstance(k, str)][:3]
            label_short = ", ".join(kws) if kws else "?"
            label = f"[{attr}] {label_short}"
            if ttype == "merged":
                en_tid = t.get("en_topic_id")
                ko_tid = t.get("ko_topic_id")
                if en_tid is not None:
                    labels[(attr, "en", int(en_tid))] = label
                if ko_tid is not None:
                    labels[(attr, "ko", int(ko_tid))] = label
            else:
                tid = t.get("topic_id")
                lang = "en" if ttype == "en_only" else ("ko" if ttype == "ko_only" else "mixed")
                if tid is not None:
                    labels[(attr, lang, int(tid))] = label
    print(f"[3/5] topic labels loaded: {len(labels):,}")
    return labels


# ───────────────────────────────────────────────────────────────
# 2. 랭킹 계산
# ───────────────────────────────────────────────────────────────
def compute_quarterly_ranks(df_assign, id2date, labels):
    df = df_assign[df_assign["topic_id"] != -1].copy()
    df["date"] = df["article_id"].map(id2date)
    missing = df["date"].isna().sum()
    df = df.dropna(subset=["date"])
    print(f"[4/5] dropped {missing:,} rows without date; remain {len(df):,}")
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"])
    df["quarter"] = df["date"].dt.tz_convert(None).dt.to_period(QUARTER_FREQ)

    # (attr, lang, topic_id) → label → 통합 키
    df["label"] = [
        labels.get((a, l, int(t)), f"[{a}] tid={t}")
        for a, l, t in zip(df["attr"], df["lang"], df["topic_id"])
    ]
    # merged 토픽은 EN+KO가 같은 label 받음 → 카운트가 자연스럽게 합쳐짐

    counts = df.groupby(["quarter", "label"]).size().reset_index(name="count")
    counts["rank"] = counts.groupby("quarter")["count"].rank(ascending=False, method="min").astype(int)
    return counts


# ───────────────────────────────────────────────────────────────
# 3. Bump chart (Plotly)
# ───────────────────────────────────────────────────────────────
def make_bump_chart(counts, out_path):
    top = counts[counts["rank"] <= TOP_N].copy()
    topic_set = top["label"].unique().tolist()

    # 각 토픽의 분기-별 (rank, count) — top10 안일 때만
    quarters_sorted = sorted(counts["quarter"].unique())
    q_to_x = {q: q.to_timestamp() for q in quarters_sorted}

    # 토픽 색 분배 — 안정적 매핑
    palette = qualitative.Dark24 + qualitative.Light24
    color_map = {label: palette[i % len(palette)] for i, label in enumerate(topic_set)}

    fig = go.Figure()
    for label in topic_set:
        sub = top[top["label"] == label].sort_values("quarter")
        if len(sub) < MIN_QUARTERS_FOR_DISPLAY:
            continue
        xs = [q_to_x[q] for q in sub["quarter"]]
        ys = sub["rank"].tolist()
        sizes = (np.log1p(sub["count"]) * 6 + 6).tolist()
        # 라인은 연속 분기끼리만 잇고 끊김도 자연스럽게 표현하려면 분기 갭이 있으면 None 삽입
        # 여기선 단순화: 모든 top10 점을 라인으로 잇음 (gap이 있으면 자동으로 직선 연결됨)
        # 진입/탈락 마커
        entry_idx = 0
        exit_idx  = len(sub) - 1
        marker_symbols = ["circle"] * len(sub)
        if len(sub) >= 1:
            marker_symbols[entry_idx] = "triangle-up"
            marker_symbols[exit_idx]  = "triangle-down" if exit_idx != entry_idx else "circle"
        fig.add_trace(go.Scatter(
            x=xs, y=ys,
            mode="lines+markers",
            name=label,
            line=dict(color=color_map[label], width=2),
            marker=dict(size=sizes, symbol=marker_symbols,
                        color=color_map[label],
                        line=dict(width=1, color="white")),
            hovertemplate=(
                "<b>%{text}</b><br>" +
                "분기: %{x|%Y Q%q}<br>" +
                "랭킹: %{y}<br>" +
                "기사 수: %{customdata}<extra></extra>"
            ),
            text=[label] * len(sub),
            customdata=sub["count"].tolist(),
        ))

    # 외부 사건 annotation
    events = [
        ("2020-03-01", "n번방 사건"),
        ("2021-01-01", "이루다 챗봇"),
        ("2022-11-30", "ChatGPT 출시"),
        ("2024-03-13", "EU AI Act 통과"),
        ("2024-10-08", "노벨 물리학상\n(머신러닝)"),
    ]
    shapes, annotations = [], []
    for date, text in events:
        x = pd.Timestamp(date)
        shapes.append(dict(type="line", x0=x, x1=x, y0=0.5, y1=TOP_N + 0.5,
                           line=dict(color="rgba(120,120,120,0.4)", width=1, dash="dot")))
        annotations.append(dict(x=x, y=0.5, text=text, showarrow=False,
                                yshift=10, font=dict(size=10, color="rgba(80,80,80,0.9)")))

    fig.update_layout(
        title=f"분기별 소주제 Top-{TOP_N} 랭킹 변동 (▲ 진입 / ▽ 탈락 / 점 크기 = log(기사수))",
        xaxis=dict(title="분기", type="date"),
        yaxis=dict(title="랭킹 (1 = 가장 많음)", autorange="reversed", dtick=1, range=[TOP_N + 0.5, 0.5]),
        height=800, width=1500,
        hovermode="closest",
        legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.02,
                    font=dict(size=10)),
        shapes=shapes, annotations=annotations,
        margin=dict(l=60, r=350, t=80, b=60),
    )
    fig.write_html(str(out_path), include_plotlyjs="cdn")
    print(f"  → bump chart: {out_path}")


# ───────────────────────────────────────────────────────────────
# 4. Lifespan (matplotlib)
# ───────────────────────────────────────────────────────────────
def make_lifespan(counts, out_path):
    top = counts[counts["rank"] <= TOP_N].copy()
    quarters_sorted = sorted(counts["quarter"].unique())
    q_index = {q: i for i, q in enumerate(quarters_sorted)}

    # 각 토픽 → top10 머문 분기 리스트 → 연속 구간으로 분할
    segments = []   # (label, start_q, end_q, mean_rank, max_count)
    for label, sub in top.groupby("label"):
        qs = sorted(sub["quarter"].unique(), key=lambda q: q_index[q])
        if not qs:
            continue
        # 연속 분기 그룹화
        run = [qs[0]]
        sub_idx = sub.set_index("quarter")
        for q in qs[1:]:
            if q_index[q] == q_index[run[-1]] + 1:
                run.append(q)
            else:
                seg_data = sub_idx.loc[run]
                segments.append((label, run[0], run[-1],
                                 seg_data["rank"].mean(), seg_data["count"].max()))
                run = [q]
        seg_data = sub_idx.loc[run]
        segments.append((label, run[0], run[-1],
                         seg_data["rank"].mean(), seg_data["count"].max()))

    # 정렬: 첫 진입 분기 + 평균 랭킹
    label_first_q = {}
    for lab, st, _, _, _ in segments:
        if lab not in label_first_q or q_index[st] < q_index[label_first_q[lab]]:
            label_first_q[lab] = st
    sorted_labels = sorted(label_first_q.keys(),
                            key=lambda l: (q_index[label_first_q[l]], l))

    fig, ax = plt.subplots(figsize=(16, max(6, 0.30 * len(sorted_labels))))
    label_y = {lab: i for i, lab in enumerate(sorted_labels)}

    cmap = plt.get_cmap("viridis_r")  # 진한색 = 1위(낮은 rank)
    for lab, st, ed, mean_rank, max_cnt in segments:
        x_start = st.to_timestamp()
        x_end = (ed + 1).to_timestamp()    # 분기 끝까지
        color = cmap((mean_rank - 1) / max(1, TOP_N - 1))
        ax.barh(label_y[lab], (x_end - x_start).days, left=x_start,
                height=0.7, color=color, edgecolor="white", linewidth=0.5)

    # 외부 사건
    events = [
        ("2020-03-01", "n번방"),
        ("2021-01-01", "이루다"),
        ("2022-11-30", "ChatGPT"),
        ("2024-03-13", "EU AI Act"),
        ("2024-10-08", "노벨 물리학상"),
    ]
    for date, text in events:
        x = pd.Timestamp(date)
        ax.axvline(x, color="gray", linestyle=":", alpha=0.5, linewidth=1)
        ax.text(x, -0.8, text, rotation=90, fontsize=8, color="gray",
                va="top", ha="right")

    ax.set_yticks(range(len(sorted_labels)))
    ax.set_yticklabels(sorted_labels, fontsize=8)
    ax.invert_yaxis()
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_xlabel("시간")
    ax.set_title(f"소주제 Top-{TOP_N} Lifespan — 각 토픽이 top10에 머문 기간 (색 = 평균 랭킹, 진할수록 1위 가까움)")

    # 한글 폰트
    for font_candidate in ["NanumGothic", "Noto Sans CJK KR", "Malgun Gothic"]:
        try:
            plt.rcParams["font.family"] = font_candidate
            break
        except Exception:
            pass

    plt.tight_layout()
    plt.savefig(str(out_path), dpi=130, bbox_inches="tight")
    plt.close()
    print(f"  → lifespan: {out_path}")


# ───────────────────────────────────────────────────────────────
def main():
    df_assign = load_assignments()
    if df_assign.empty:
        print("ERROR: subtopic_assignments 테이블이 비어있음. BERTopic 재실행 필요.")
        sys.exit(1)
    id2date = build_id_to_date()
    labels = load_topic_labels()
    counts = compute_quarterly_ranks(df_assign, id2date, labels)
    print(f"[5/5] quarterly count rows: {len(counts):,}; "
          f"unique topics: {counts['label'].nunique()}; "
          f"quarters: {counts['quarter'].nunique()}")

    bump_path = OUT_DIR / "temporal_top10_bump.html"
    life_path = OUT_DIR / "temporal_top10_lifespan.png"
    make_bump_chart(counts, bump_path)
    make_lifespan(counts, life_path)
    print("\nDone.")


if __name__ == "__main__":
    main()
