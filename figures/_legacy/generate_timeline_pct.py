"""Policy Attribute 비율(%) 기반 시계열 그래프.

Usage:
    python generate_timeline_pct.py
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
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np

for f in fm.findSystemFonts():
    if 'malgun' in f.lower():
        plt.rcParams['font.family'] = fm.FontProperties(fname=f).get_name()
        break
plt.rcParams['axes.unicode_minus'] = False

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FIG_DIR = os.path.join(os.path.dirname(__file__), "figures")
os.makedirs(FIG_DIR, exist_ok=True)

EN_ATTRS = [
    'Safety', 'Responsible and ethical AI',
    'Market efficiency and power concentration (antitrust)',
    'Industrial policy', 'National security', 'Public interest',
    'Labor', 'Copyright', 'International collaboration', 'Elections',
]
ATTR_KR = {
    'Safety': 'AI 안전', 'Responsible and ethical AI': '책임/윤리 AI',
    'Market efficiency and power concentration (antitrust)': '시장경쟁/독과점',
    'Industrial policy': '산업정책', 'National security': '국가안보',
    'Public interest': '공익/소비자보호', 'Labor': '노동/고용',
    'Copyright': '저작권/지식재산', 'International collaboration': '국제협력',
    'Elections': '선거/민주주의',
}
KR_TO_EN = {
    'AI안전': 'Safety', '책임/윤리AI': 'Responsible and ethical AI',
    '시장경쟁/독과점': 'Market efficiency and power concentration (antitrust)',
    '산업정책': 'Industrial policy', '국가안보': 'National security',
    '공익/소비자보호': 'Public interest', '노동/고용': 'Labor',
    '저작권/지식재산': 'Copyright', '국제협력': 'International collaboration',
    '선거/민주주의': 'Elections',
}
COLORS = {
    'Safety': '#ef5350', 'Responsible and ethical AI': '#ab47bc',
    'Market efficiency and power concentration (antitrust)': '#42a5f5',
    'Industrial policy': '#66bb6a', 'National security': '#ff7043',
    'Public interest': '#26c6da', 'Labor': '#ffa726',
    'Copyright': '#ec407a', 'International collaboration': '#8d6e63',
    'Elections': '#78909c',
}


def normalize_primary(p):
    canonical = {a.lower(): a for a in EN_ATTRS}
    p = p.strip()
    if p and p[0].isdigit():
        p = p.split('.', 1)[-1].strip()
    return canonical.get(p.lower(), KR_TO_EN.get(p, None))


def to_quarter(datestr):
    try:
        if 'T' in datestr:
            d = datetime.fromisoformat(datestr.replace('Z', '+00:00').split('+')[0])
        else:
            d = datetime.strptime(datestr[:10], '%Y-%m-%d')
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}"
    except:
        return None


def load_news_paired(raw_path, cls_path, id_field):
    with open(raw_path, encoding='utf-8') as f:
        raw = json.load(f)
    with open(cls_path, encoding='utf-8') as f:
        cls = json.load(f)
    raw_by_id = {a[id_field]: a for a in raw}
    paired = []
    for c in cls:
        r = raw_by_id.get(c['article_id'])
        if r:
            attr = normalize_primary(c.get('primary', ''))
            if attr:
                paired.append({'attr': attr, 'date': r.get('pub_date', '')})
    return paired


def load_kr_bills():
    with open(os.path.join('replicate_carvao', 'data', 'kr_gpt_classification.json'), encoding='utf-8') as f:
        cls = json.load(f)
    with open(os.path.join('replicate_carvao', 'data', 'kr_bills_processed.json'), encoding='utf-8') as f:
        proc = json.load(f)
    proc_by_id = {b['bill_id']: b for b in proc}
    paired = []
    for bill_id, c in cls.items():
        b = proc_by_id.get(bill_id)
        if b:
            attr = normalize_primary(c.get('primary', ''))
            if attr:
                paired.append({'attr': attr, 'date': b.get('propose_date', '')})
    return paired


def build_quarterly(data):
    result = defaultdict(lambda: Counter())
    for d in data:
        q = to_quarter(d['date'])
        if q:
            result[d['attr']][q] += 1
    return result


def get_all_quarters(start='2016-Q1', end='2026-Q2'):
    quarters = []
    for y in range(2016, 2027):
        for q in range(1, 5):
            label = f"{y}-Q{q}"
            if label >= start and label <= end:
                quarters.append(label)
    return quarters


def to_pct(quarterly, quarters):
    """Convert absolute counts to percentages per quarter."""
    pct = defaultdict(dict)
    for q in quarters:
        total = sum(quarterly[attr].get(q, 0) for attr in EN_ATTRS)
        for attr in EN_ATTRS:
            if total > 0:
                pct[attr][q] = quarterly[attr].get(q, 0) / total * 100
            else:
                pct[attr][q] = 0
    return pct


def plot_stacked_pct(pct_data, quarters, title, filename):
    """100% stacked area chart."""
    fig, ax = plt.subplots(figsize=(14, 6))
    x = np.arange(len(quarters))

    # Order by total share
    totals = {attr: sum(pct_data[attr].get(q, 0) for q in quarters) for attr in EN_ATTRS}
    ordered = sorted(EN_ATTRS, key=lambda a: -totals[a])

    bottom = np.zeros(len(quarters))
    for attr in ordered:
        vals = np.array([pct_data[attr].get(q, 0) for q in quarters])
        ax.fill_between(x, bottom, bottom + vals, alpha=0.75,
                        color=COLORS[attr], label=ATTR_KR[attr])
        bottom += vals

    tick_pos = [i for i, q in enumerate(quarters) if q.endswith('Q1')]
    tick_lab = [q[:4] for q in quarters if q.endswith('Q1')]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_lab, fontsize=10)

    chatgpt_q = quarters.index('2022-Q4') if '2022-Q4' in quarters else None
    if chatgpt_q is not None:
        ax.axvline(chatgpt_q, color='white', linewidth=1.5, linestyle='--', alpha=0.8)
        ax.text(chatgpt_q + 0.5, 95, 'ChatGPT', fontsize=9, color='white', fontweight='bold')

    ax.set_ylim(0, 100)
    ax.set_ylabel('비율 (%)', fontsize=11)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.legend(loc='center left', bbox_to_anchor=(1, 0.5), fontsize=9)
    ax.set_xlim(0, len(quarters) - 1)

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, filename + '.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, filename + '.pdf'), bbox_inches='tight')
    plt.close()
    print(f"  {filename}", flush=True)


def plot_individual_pct(pct_g, pct_n, quarters, title, filename):
    """10 individual line charts — Guardian vs NYT percentage."""
    fig, axes = plt.subplots(2, 5, figsize=(22, 8), sharex=True, sharey=True)
    axes = axes.flatten()
    x = np.arange(len(quarters))

    for i, attr in enumerate(EN_ATTRS):
        ax = axes[i]
        g_vals = [pct_g[attr].get(q, 0) for q in quarters]
        n_vals = [pct_n[attr].get(q, 0) for q in quarters]

        # Smooth with 2-quarter moving average
        def smooth(vals, w=2):
            return np.convolve(vals, np.ones(w)/w, mode='same')

        ax.plot(x, smooth(g_vals), color='#42a5f5', linewidth=1.5, label='Guardian')
        ax.plot(x, smooth(n_vals), color='#ff7043', linewidth=1.5, label='NYT')
        ax.fill_between(x, smooth(g_vals), alpha=0.15, color='#42a5f5')
        ax.fill_between(x, smooth(n_vals), alpha=0.15, color='#ff7043')

        ax.set_title(ATTR_KR[attr], fontsize=10, fontweight='bold', color=COLORS[attr])

        cq = quarters.index('2022-Q4') if '2022-Q4' in quarters else None
        if cq is not None:
            ax.axvline(cq, color='#e53935', linewidth=0.8, linestyle='--', alpha=0.5)

        tick_pos = [j for j, q in enumerate(quarters) if q.endswith('Q1') and int(q[:4]) % 2 == 0]
        tick_lab = [q[:4] for q in quarters if q.endswith('Q1') and int(q[:4]) % 2 == 0]
        ax.set_xticks(tick_pos)
        ax.set_xticklabels(tick_lab, fontsize=8)
        ax.tick_params(axis='y', labelsize=8)
        ax.set_ylabel('%', fontsize=8)

        if i == 0:
            ax.legend(fontsize=7)

    plt.suptitle(title, fontsize=14, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, filename + '.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, filename + '.pdf'), bbox_inches='tight')
    plt.close()
    print(f"  {filename}", flush=True)


def plot_kr_bills_line(quarterly, quarters_kr, title, filename):
    """Korean bills as line chart with percentages."""
    # Filter quarters with data
    quarters_with_data = [q for q in quarters_kr
                          if sum(quarterly[attr].get(q, 0) for attr in EN_ATTRS) > 0]
    if not quarters_with_data:
        print(f"  {filename}: no data", flush=True)
        return

    pct = to_pct(quarterly, quarters_with_data)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={'height_ratios': [1, 1.2]})

    x = np.arange(len(quarters_with_data))

    # Top: absolute stacked bar
    totals_q = {attr: sum(quarterly[attr].get(q, 0) for q in quarters_with_data) for attr in EN_ATTRS}
    ordered = sorted(EN_ATTRS, key=lambda a: -totals_q[a])

    bottom = np.zeros(len(quarters_with_data))
    for attr in ordered:
        vals = np.array([quarterly[attr].get(q, 0) for q in quarters_with_data])
        if vals.sum() > 0:
            ax1.bar(x, vals, bottom=bottom, color=COLORS[attr],
                    label=ATTR_KR[attr], width=0.7)
            bottom += vals

    ax1.set_xticks(x)
    ax1.set_xticklabels(quarters_with_data, fontsize=8, rotation=45, ha='right')
    ax1.set_ylabel('법안 수', fontsize=10)
    ax1.set_title(title + ' (절대량)', fontsize=12, fontweight='bold')
    ax1.legend(loc='upper left', fontsize=8, ncol=3)

    # Bottom: percentage line chart
    for attr in ordered[:5]:
        vals = [pct[attr].get(q, 0) for q in quarters_with_data]
        if max(vals) > 0:
            ax2.plot(x, vals, color=COLORS[attr], linewidth=2,
                     marker='o', markersize=4, label=ATTR_KR[attr])

    ax2.set_xticks(x)
    ax2.set_xticklabels(quarters_with_data, fontsize=8, rotation=45, ha='right')
    ax2.set_ylabel('비율 (%)', fontsize=10)
    ax2.set_title(title + ' (비율)', fontsize=12, fontweight='bold')
    ax2.legend(loc='upper left', fontsize=8, ncol=3)

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, filename + '.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, filename + '.pdf'), bbox_inches='tight')
    plt.close()
    print(f"  {filename}", flush=True)


def main():
    print("=== Loading data ===", flush=True)
    guardian = load_news_paired(
        os.path.join(DATA_DIR, 'guardian_articles_raw.json'),
        os.path.join(DATA_DIR, 'processed/articles_classified_guardian.json'), 'url')
    nyt = load_news_paired(
        os.path.join(DATA_DIR, 'nyt_articles_raw.json'),
        os.path.join(DATA_DIR, 'processed/articles_classified_nyt.json'), 'url')
    kr_bills = load_kr_bills()
    print(f"  Guardian: {len(guardian)}, NYT: {len(nyt)}, KR bills: {len(kr_bills)}", flush=True)

    quarters = get_all_quarters()
    q_guardian = build_quarterly(guardian)
    q_nyt = build_quarterly(nyt)
    q_kr = build_quarterly(kr_bills)

    pct_g = to_pct(q_guardian, quarters)
    pct_n = to_pct(q_nyt, quarters)

    print("\n=== Generating figures ===", flush=True)

    # 100% stacked area
    plot_stacked_pct(pct_g, quarters, 'Guardian — 정책 속성 비율 추이 (분기별)', 'fig_pct_guardian')
    plot_stacked_pct(pct_n, quarters, 'NYT — 정책 속성 비율 추이 (분기별)', 'fig_pct_nyt')

    # Individual attribute lines (%)
    plot_individual_pct(pct_g, pct_n, quarters,
                        'Guardian vs NYT — 정책 속성별 비율 추이', 'fig_pct_attrs')

    # Korean bills
    quarters_kr = get_all_quarters('2024-Q2', '2026-Q2')
    plot_kr_bills_line(q_kr, quarters_kr,
                       '22대 국회 AI 법안 발의 추이', 'fig_pct_kr_bills')

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
