"""Step 7: 논문 Figure 재현 시각화.

Figure 8~35를 matplotlib로 재현.
논문 스타일: 단순 막대 차트, 산점도, 히트맵.

Usage:
    python replicate_carvao/07_visualize.py
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

plt.rcParams["figure.dpi"] = 150
plt.rcParams["savefig.bbox"] = "tight"
plt.rcParams["font.size"] = 10

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FIG_DIR = os.path.join(os.path.dirname(__file__), "figures")

BLUE = "#1f4e79"
RED = "#c0392b"
PURPLE = "#8e44ad"
PARTY_COLORS = {"D": BLUE, "R": RED, "I": PURPLE}


def load_data():
    with open(os.path.join(DATA_DIR, "bills_processed.json"), encoding="utf-8") as f:
        bills = json.load(f)
    with open(os.path.join(DATA_DIR, "tfidf_classification.json"), encoding="utf-8") as f:
        tfidf = json.load(f)
    with open(os.path.join(DATA_DIR, "gpt_classification.json"), encoding="utf-8") as f:
        gpt = json.load(f)
    with open(os.path.join(DATA_DIR, "lda_topics.json"), encoding="utf-8") as f:
        lda = json.load(f)
    with open(os.path.join(DATA_DIR, "tfidf_lda_crossmap.json"), encoding="utf-8") as f:
        crossmap = json.load(f)
    return bills, tfidf, gpt, lda, crossmap


# ─── Figure 8: Bill sponsorship by party ───

def fig08_party(bills):
    party_counts = Counter(b["sponsor_party"] for b in bills)
    labels = ["Democratic", "Republican", "Independent"]
    keys = ["D", "R", "I"]
    vals = [party_counts.get(k, 0) for k in keys]
    colors = [BLUE, RED, PURPLE]

    fig, ax = plt.subplots(figsize=(6, 4))
    bars = ax.bar(labels, vals, color=colors, width=0.6)
    for bar, v in zip(bars, vals):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                str(v), ha="center", fontweight="bold")
    ax.set_ylabel("Number of Bills")
    ax.set_title("AI Bills by Party in Congress")
    ax.set_ylim(0, max(vals) * 1.15)
    fig.savefig(os.path.join(FIG_DIR, "fig08_party.png"))
    plt.close(fig)
    print("  fig08_party.png")


# ─── Figure 9: Bipartisanship scatter ───

def fig09_bipartisan(bills):
    fig, ax = plt.subplots(figsize=(8, 6))
    for b in bills:
        bp = b["bipartisan"]
        x = bp["dem_ratio"]
        y = bp["total_sponsors"]
        cat = bp["category"]
        color = BLUE if cat == "Democratic" else RED if cat == "Republican" else PURPLE
        ax.scatter(x, y, c=color, alpha=0.6, s=40, edgecolors="white", linewidth=0.5)

    ax.set_xlabel("Democratic Sponsor Ratio")
    ax.set_ylabel("Total Number of Sponsors")
    ax.set_title("Bill Sponsorship Analysis: Party Ratio vs Total Sponsors\nCongress")
    # Legend
    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor=BLUE, label="Democratic (>60%)", markersize=8),
        Line2D([0], [0], marker="o", color="w", markerfacecolor=RED, label="Republican (<40%)", markersize=8),
        Line2D([0], [0], marker="o", color="w", markerfacecolor=PURPLE, label="Bipartisan", markersize=8),
    ]
    ax.legend(handles=legend_elements, loc="upper left")
    fig.savefig(os.path.join(FIG_DIR, "fig09_bipartisan.png"))
    plt.close(fig)
    print("  fig09_bipartisan.png")


# ─── Figure 10: AI bills by state ───

def fig10_state(bills):
    state_counts = Counter(b["sponsor_state"] for b in bills if b["sponsor_state"])
    top = state_counts.most_common(36)
    states = [s for s, _ in top]
    vals = [c for _, c in top]

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.bar(states, vals, color=PURPLE, width=0.7)
    for i, v in enumerate(vals):
        ax.text(i, v + 0.2, str(v), ha="center", fontsize=7)
    ax.set_ylabel("Number of Bills")
    ax.set_title("AI Bills by State in Congress")
    ax.tick_params(axis="x", rotation=45)
    fig.savefig(os.path.join(FIG_DIR, "fig10_state.png"))
    plt.close(fig)
    print("  fig10_state.png")


# ─── Figure 22: Monthly distribution ───

def fig22_monthly(bills):
    house = [b for b in bills if b["chamber"] == "House"]
    senate = [b for b in bills if b["chamber"] == "Senate"]

    all_months = sorted(set(b["month"] for b in bills if b["month"]))

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

    for ax, subset, title, color in [
        (axes[0], bills, "Congress", PURPLE),
        (axes[1], house, "House", RED),
        (axes[2], senate, "Senate", BLUE),
    ]:
        counts = Counter(b["month"] for b in subset if b["month"])
        vals = [counts.get(m, 0) for m in all_months]
        ax.plot(range(len(all_months)), vals, marker="o", color=color, linewidth=1.5)
        for i, v in enumerate(vals):
            if v > 0:
                ax.annotate(str(v), (i, v), textcoords="offset points",
                           xytext=(0, 8), ha="center", fontsize=7)
        ax.set_title(f"Monthly Distribution of AI-Related Bills in {title}")
        ax.set_ylabel("Number of Bills")

    axes[2].set_xticks(range(len(all_months)))
    axes[2].set_xticklabels([m[2:] for m in all_months], rotation=45, fontsize=7)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "fig22_monthly.png"))
    plt.close(fig)
    print("  fig22_monthly.png")


# ─── Figure 23: Sponsorship by chamber ───

def fig23_chamber(bills):
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))

    for ax, subset, title in [
        (axes[0], bills, "Congress"),
        (axes[1], [b for b in bills if b["chamber"] == "House"], "House"),
        (axes[2], [b for b in bills if b["chamber"] == "Senate"], "Senate"),
    ]:
        pc = Counter(b["sponsor_party"] for b in subset)
        labels, vals, colors = [], [], []
        for party, label, color in [("D", "Democratic", BLUE), ("R", "Republican", RED), ("I", "Independent", PURPLE)]:
            if pc.get(party, 0) > 0:
                labels.append(label)
                vals.append(pc[party])
                colors.append(color)
        bars = ax.bar(labels, vals, color=colors, width=0.6)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                    str(v), ha="center", fontweight="bold")
        ax.set_title(f"AI Bills by Party in {title}")
        ax.set_ylabel("Number of Bills")

    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "fig23_chamber.png"))
    plt.close(fig)
    print("  fig23_chamber.png")


# ─── Figure 27-28: Most active members ───

def fig27_28_members(bills):
    for chamber, fignum in [("House", 27), ("Senate", 28)]:
        subset = [b for b in bills if b["chamber"] == chamber]
        sponsor_bills = defaultdict(list)
        for b in subset:
            sponsor_bills[b["sponsor_name"]].append(b["sponsor_party"])

        sorted_sponsors = sorted(sponsor_bills.items(), key=lambda x: -len(x[1]))
        top = sorted_sponsors[:30] if chamber == "House" else sorted_sponsors[:40]

        names = [s[0] for s in top]
        counts = [len(s[1]) for s in top]
        colors = [PARTY_COLORS.get(s[1][0], "gray") for s in top]

        fig, ax = plt.subplots(figsize=(14, 5))
        bars = ax.bar(range(len(names)), counts, color=colors, width=0.7)
        for i, v in enumerate(counts):
            ax.text(i, v + 0.1, str(v), ha="center", fontsize=7)
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, rotation=90, fontsize=6)
        ax.set_ylabel("Number of Bills Sponsored")
        ax.set_title(f"AI Bill Sponsorship in {chamber}")

        from matplotlib.patches import Patch
        legend = [Patch(facecolor=RED, label="Republican"),
                  Patch(facecolor=BLUE, label="Democrat")]
        if chamber == "Senate":
            legend.append(Patch(facecolor=PURPLE, label="Independent"))
        ax.legend(handles=legend)

        fig.tight_layout()
        fig.savefig(os.path.join(FIG_DIR, f"fig{fignum}_{chamber.lower()}.png"))
        plt.close(fig)
        print(f"  fig{fignum}_{chamber.lower()}.png")


# ─── Figure 29: Most active committees ───

def fig29_committees(bills):
    fig, axes = plt.subplots(2, 1, figsize=(12, 8))

    for ax, chamber, color in [(axes[0], "House", RED), (axes[1], "Senate", BLUE)]:
        subset = [b for b in bills if b["chamber"] == chamber]
        comm_counts = Counter(b["primary_committee"] for b in subset if b["primary_committee"])
        top = comm_counts.most_common(15)
        names = [c[0] for c in top]
        vals = [c[1] for c in top]

        bars = ax.barh(range(len(names)), vals, color=color)
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels(names, fontsize=7)
        ax.invert_yaxis()
        for i, v in enumerate(vals):
            ax.text(v + 0.2, i, str(v), va="center", fontsize=8)
        ax.set_title(f"AI Bills by Committee in {chamber}")
        ax.set_xlabel("Number of Bills")

    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "fig29_committees.png"))
    plt.close(fig)
    print("  fig29_committees.png")


# ─── Figure 30: Bill progression ───

def fig30_progression(bills):
    stage_labels = [
        "Introduced/\nReferred to\nCommittee",
        "Referred to\nSubcommittee",
        "Mark-up",
        "Reported Out\nof Committee",
        "Placed on\nCalendar",
        "Floor\nConsideration",
        "Passed One\nChamber",
        "Received by\nSecond Chamber",
    ]
    stage_counts = Counter(b["stage"] for b in bills)
    # Combine stage 0 and 1 like the paper seems to do
    vals = []
    for s in range(8):
        vals.append(stage_counts.get(s, 0))

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(range(len(vals)), vals, color=PURPLE, width=0.6)
    for bar, v in zip(bars, vals):
        if v > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                    str(v), ha="center", fontweight="bold")
    ax.set_xticks(range(len(stage_labels)))
    ax.set_xticklabels(stage_labels, fontsize=8)
    ax.set_ylabel("Number of Bills")
    ax.set_title("Number of Bills in Each Stage")
    fig.savefig(os.path.join(FIG_DIR, "fig30_progression.png"))
    plt.close(fig)
    print("  fig30_progression.png")


# ─── Figure 11/31: Primary + Top 3 Policy Attributes (GPT-based) ───

def fig31_attributes(gpt):
    primary = Counter(r["primary"] for r in gpt.values())
    top3 = Counter()
    for r in gpt.values():
        top3[r["primary"]] += 1
        top3[r["secondary"]] += 1
        top3[r["tertiary"]] += 1

    fig, axes = plt.subplots(2, 1, figsize=(12, 8))

    # Primary
    ax = axes[0]
    items = primary.most_common()
    names = [n for n, _ in items]
    vals = [v for _, v in items]
    ax.bar(range(len(names)), vals, color=PURPLE, width=0.6)
    for i, v in enumerate(vals):
        ax.text(i, v + 0.5, str(v), ha="center", fontweight="bold", fontsize=9)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=30, ha="right", fontsize=7)
    ax.set_ylabel("Number of Bills")
    ax.set_title("Primary Attributes in Congressional Bills")

    # Top 3
    ax = axes[1]
    items = top3.most_common()
    names = [n for n, _ in items]
    vals = [v for _, v in items]
    ax.bar(range(len(names)), vals, color=PURPLE, width=0.6)
    for i, v in enumerate(vals):
        ax.text(i, v + 1, str(v), ha="center", fontweight="bold", fontsize=9)
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=30, ha="right", fontsize=7)
    ax.set_ylabel("Number of Bills")
    ax.set_title("Top 3 Policy Attributes in Congressional Bills")

    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "fig31_attributes.png"))
    plt.close(fig)
    print("  fig31_attributes.png")


# ─── Figure 20: TF-IDF Distribution ───

def fig20_tfidf(tfidf):
    primary = Counter(r["primary"] for r in tfidf.values())
    items = primary.most_common()
    names = [n for n, _ in items]
    vals = [v for _, v in items]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(range(len(names)), vals, color=BLUE, width=0.6)
    for i, v in enumerate(vals):
        ax.text(i, v + 0.5, str(v), ha="center", fontweight="bold")
    ax.set_xticks(range(len(names)))
    ax.set_xticklabels(names, rotation=30, ha="right", fontsize=7)
    ax.set_ylabel("Number of Bills")
    ax.set_title("Distribution of Bill Categories (TF-IDF)")
    fig.savefig(os.path.join(FIG_DIR, "fig20_tfidf.png"))
    plt.close(fig)
    print("  fig20_tfidf.png")


# ─── Figure 21: TF-IDF vs LDA heatmap ───

def fig21_heatmap(crossmap):
    matrix = np.array(crossmap["matrix"])
    attr_names = crossmap["attr_names"]
    n_topics = crossmap["n_topics"]

    # Shorten attribute names
    short = [a[:35] for a in attr_names]

    fig, ax = plt.subplots(figsize=(12, 7))
    im = ax.imshow(matrix, cmap="YlOrRd", aspect="auto")

    ax.set_xticks(range(n_topics))
    ax.set_xticklabels([f"Topic {i}" for i in range(n_topics)], rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(short)))
    ax.set_yticklabels(short, fontsize=8)

    # Annotate cells
    for i in range(len(attr_names)):
        for j in range(n_topics):
            val = matrix[i, j]
            if val > 0:
                pct = val / len(next(iter({}))) if False else val  # just the count
                ax.text(j, i, f"{val}\n({val/154*100:.1f}%)", ha="center", va="center", fontsize=6)

    ax.set_title("Comparison of Bill Classifications:\nTF-IDF vs Natural Topic Discovery")
    ax.set_xlabel("LDA Classification")
    ax.set_ylabel("TF-IDF Classification")
    fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "fig21_heatmap.png"))
    plt.close(fig)
    print("  fig21_heatmap.png")


# ─── Figure 32: Primary attribute by party ───

def fig32_party_attr(bills, gpt):
    fig, axes = plt.subplots(2, 1, figsize=(12, 8))

    for ax, party, label, color in [(axes[0], "R", "Republican Sponsors", RED),
                                     (axes[1], "D", "Democratic Sponsors", BLUE)]:
        subset_ids = {b["bill_id"] for b in bills if b["sponsor_party"] == party}
        primary = Counter(gpt[bid]["primary"] for bid in subset_ids if bid in gpt)
        items = primary.most_common()
        names = [n for n, _ in items]
        vals = [v for _, v in items]

        ax.bar(range(len(names)), vals, color=color, width=0.6)
        for i, v in enumerate(vals):
            ax.text(i, v + 0.3, str(v), ha="center", fontweight="bold", fontsize=9)
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels(names, rotation=30, ha="right", fontsize=7)
        ax.set_ylabel("Number of Bills")
        ax.set_title(f"Primary Policy Attributes in Congressional Bills ({label})")

    fig.tight_layout()
    fig.savefig(os.path.join(FIG_DIR, "fig32_party_attr.png"))
    plt.close(fig)
    print("  fig32_party_attr.png")


# ─── Figure 35: Bills on legislative calendar ───

def fig35_calendar(bills):
    calendar_bills = [b for b in bills if b["stage"] >= 4]
    calendar_bills.sort(key=lambda x: x["stage"])

    print(f"\n  [Figure 35] Bills on legislative calendar: {len(calendar_bills)}건")
    print(f"  {'Bill':15s} {'Stage':6s} Title")
    print(f"  {'-'*15} {'-'*6} {'-'*50}")
    for b in calendar_bills[:20]:
        print(f"  {b['bill_id']:15s} {b['stage']:6d} {b['title'][:50]}")


def main():
    os.makedirs(FIG_DIR, exist_ok=True)
    bills, tfidf, gpt, lda, crossmap = load_data()

    print(f"데이터: {len(bills)}건")
    print(f"\n시각화 생성:")

    fig08_party(bills)
    fig09_bipartisan(bills)
    fig10_state(bills)
    fig22_monthly(bills)
    fig23_chamber(bills)
    fig27_28_members(bills)
    fig29_committees(bills)
    fig30_progression(bills)
    fig20_tfidf(tfidf)
    fig21_heatmap(crossmap)
    fig31_attributes(gpt)
    fig32_party_attr(bills, gpt)
    fig35_calendar(bills)

    print(f"\n완료: {FIG_DIR}/")


if __name__ == "__main__":
    main()
