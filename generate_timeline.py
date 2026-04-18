"""Policy Attribute별 시계열 그래프 생성.

Guardian, NYT: 분기별 기사 수
한국 법안: 월별 발의 수

Usage:
    python generate_timeline.py
"""
import json
import os
import sys
from collections import Counter, defaultdict

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
import numpy as np
from datetime import datetime

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
ATTR_SHORT = {
    'Safety': 'Safety', 'Responsible and ethical AI': 'Responsible AI',
    'Market efficiency and power concentration (antitrust)': 'Market/Antitrust',
    'Industrial policy': 'Industrial policy', 'National security': 'National security',
    'Public interest': 'Public interest', 'Labor': 'Labor', 'Copyright': 'Copyright',
    'International collaboration': 'Intl collab.', 'Elections': 'Elections',
}
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
    """Convert date string to quarter label like '2023-Q1'."""
    try:
        if 'T' in datestr:
            d = datetime.fromisoformat(datestr.replace('Z', '+00:00').split('+')[0])
        else:
            d = datetime.strptime(datestr[:10], '%Y-%m-%d')
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}"
    except:
        return None


def to_month(datestr):
    try:
        if 'T' in datestr:
            d = datetime.fromisoformat(datestr.replace('Z', '+00:00').split('+')[0])
        else:
            d = datetime.strptime(datestr[:10], '%Y-%m-%d')
        return f"{d.year}-{d.month:02d}"
    except:
        return None


def load_news_paired(raw_path, cls_path, id_field, date_field='pub_date'):
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
                paired.append({'attr': attr, 'date': r.get(date_field, '')})
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
    """Build {attr: {quarter: count}} from paired data."""
    result = defaultdict(lambda: Counter())
    for d in data:
        q = to_quarter(d['date'])
        if q:
            result[d['attr']][q] += 1
    return result


def build_monthly(data):
    result = defaultdict(lambda: Counter())
    for d in data:
        m = to_month(d['date'])
        if m:
            result[d['attr']][m] += 1
    return result


def get_all_quarters(start='2016-Q1', end='2026-Q2'):
    quarters = []
    for y in range(2016, 2027):
        for q in range(1, 5):
            label = f"{y}-Q{q}"
            if label >= start and label <= end:
                quarters.append(label)
    return quarters


def plot_stacked_area(quarterly, title, filename, quarters=None):
    if quarters is None:
        quarters = get_all_quarters()

    # Filter to top 5 attributes by total volume
    totals = {attr: sum(quarterly[attr].values()) for attr in EN_ATTRS if attr in quarterly}
    top5 = sorted(totals, key=totals.get, reverse=True)[:5]
    other_attrs = [a for a in EN_ATTRS if a in quarterly and a not in top5]

    fig, ax = plt.subplots(figsize=(14, 6))
    x = np.arange(len(quarters))

    # Stack data
    bottom = np.zeros(len(quarters))
    for attr in top5:
        vals = np.array([quarterly[attr].get(q, 0) for q in quarters], dtype=float)
        ax.fill_between(x, bottom, bottom + vals, alpha=0.7,
                        color=COLORS.get(attr, '#999'),
                        label=ATTR_KR.get(attr, attr))
        bottom += vals

    # Other as gray
    if other_attrs:
        other_vals = np.zeros(len(quarters))
        for attr in other_attrs:
            other_vals += np.array([quarterly[attr].get(q, 0) for q in quarters], dtype=float)
        ax.fill_between(x, bottom, bottom + other_vals, alpha=0.3, color='#999', label='기타')

    # X axis
    tick_positions = [i for i, q in enumerate(quarters) if q.endswith('Q1')]
    tick_labels = [q.split('-')[0] for q in quarters if q.endswith('Q1')]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, fontsize=10)

    # ChatGPT line
    chatgpt_q = quarters.index('2022-Q4') if '2022-Q4' in quarters else None
    if chatgpt_q is not None:
        ax.axvline(chatgpt_q, color='#e53935', linewidth=1.5, linestyle='--', alpha=0.7)
        ax.text(chatgpt_q + 0.5, ax.get_ylim()[1] * 0.9, 'ChatGPT', fontsize=9,
                color='#e53935', fontweight='bold')

    ax.set_ylabel('기사 수 (분기별)', fontsize=11)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.legend(loc='upper left', fontsize=9, ncol=2)
    ax.set_xlim(0, len(quarters) - 1)

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, filename + '.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, filename + '.pdf'), bbox_inches='tight')
    plt.close()
    print(f"  {filename}", flush=True)


def plot_kr_bills_monthly(monthly, title, filename):
    months = sorted(set(m for attr in monthly for m in monthly[attr]))
    if not months:
        print(f"  {filename}: no data", flush=True)
        return

    fig, ax = plt.subplots(figsize=(14, 5))
    x = np.arange(len(months))

    totals = {attr: sum(monthly[attr].values()) for attr in EN_ATTRS if attr in monthly}
    top5 = sorted(totals, key=totals.get, reverse=True)[:5]

    bottom = np.zeros(len(months))
    for attr in top5:
        vals = np.array([monthly[attr].get(m, 0) for m in months], dtype=float)
        ax.bar(x, vals, bottom=bottom, color=COLORS.get(attr, '#999'),
               label=ATTR_KR.get(attr, attr), width=0.8)
        bottom += vals

    # Other
    other_attrs = [a for a in EN_ATTRS if a in monthly and a not in top5]
    if other_attrs:
        other_vals = np.zeros(len(months))
        for attr in other_attrs:
            other_vals += np.array([monthly[attr].get(m, 0) for m in months], dtype=float)
        ax.bar(x, other_vals, bottom=bottom, color='#bbb', label='기타', width=0.8)

    # X axis - show every 3rd month
    tick_pos = [i for i in range(0, len(months), 3)]
    tick_lab = [months[i] for i in tick_pos]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_lab, fontsize=9, rotation=45, ha='right')

    ax.set_ylabel('법안 발의 수 (월별)', fontsize=11)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.legend(loc='upper left', fontsize=9, ncol=2)

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, filename + '.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, filename + '.pdf'), bbox_inches='tight')
    plt.close()
    print(f"  {filename}", flush=True)


def plot_individual_attrs(quarterly_g, quarterly_n, title_prefix, filename_prefix):
    """Individual line charts per attribute — Guardian vs NYT."""
    quarters = get_all_quarters()

    fig, axes = plt.subplots(2, 5, figsize=(20, 8), sharex=True)
    axes = axes.flatten()

    for i, attr in enumerate(EN_ATTRS):
        ax = axes[i]
        x = np.arange(len(quarters))
        g_vals = [quarterly_g[attr].get(q, 0) for q in quarters]
        n_vals = [quarterly_n[attr].get(q, 0) for q in quarters]

        ax.plot(x, g_vals, color='#42a5f5', linewidth=1.2, label='Guardian')
        ax.plot(x, n_vals, color='#ff7043', linewidth=1.2, label='NYT')
        ax.fill_between(x, g_vals, alpha=0.2, color='#42a5f5')
        ax.fill_between(x, n_vals, alpha=0.2, color='#ff7043')

        ax.set_title(ATTR_KR[attr], fontsize=10, fontweight='bold', color=COLORS[attr])

        # ChatGPT line
        cq = quarters.index('2022-Q4') if '2022-Q4' in quarters else None
        if cq is not None:
            ax.axvline(cq, color='#e53935', linewidth=0.8, linestyle='--', alpha=0.5)

        tick_pos = [j for j, q in enumerate(quarters) if q.endswith('Q1') and int(q[:4]) % 2 == 0]
        tick_lab = [q[:4] for q in quarters if q.endswith('Q1') and int(q[:4]) % 2 == 0]
        ax.set_xticks(tick_pos)
        ax.set_xticklabels(tick_lab, fontsize=8)
        ax.tick_params(axis='y', labelsize=8)

        if i == 0:
            ax.legend(fontsize=7)

    plt.suptitle(f'{title_prefix} — 정책 속성별 분기별 추이', fontsize=14, fontweight='bold', y=1.02)
    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, filename_prefix + '.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, filename_prefix + '.pdf'), bbox_inches='tight')
    plt.close()
    print(f"  {filename_prefix}", flush=True)


def main():
    print("=== Loading data ===", flush=True)

    guardian = load_news_paired(
        os.path.join(DATA_DIR, 'guardian_articles_raw.json'),
        os.path.join(DATA_DIR, 'news_guardian_classified.json'),
        'url')
    nyt = load_news_paired(
        os.path.join(DATA_DIR, 'nyt_articles_raw.json'),
        os.path.join(DATA_DIR, 'news_nyt_classified.json'),
        'url')
    kr_bills = load_kr_bills()

    print(f"  Guardian: {len(guardian)}, NYT: {len(nyt)}, KR bills: {len(kr_bills)}", flush=True)

    print("\n=== Building timeseries ===", flush=True)
    q_guardian = build_quarterly(guardian)
    q_nyt = build_quarterly(nyt)
    m_kr = build_monthly(kr_bills)

    print("\n=== Generating figures ===", flush=True)

    # Stacked area: Guardian
    plot_stacked_area(q_guardian, 'Guardian — AI 뉴스 정책 속성별 추이 (분기별)', 'fig_ts_guardian')

    # Stacked area: NYT
    plot_stacked_area(q_nyt, 'NYT — AI 뉴스 정책 속성별 추이 (분기별)', 'fig_ts_nyt')

    # Korean bills monthly stacked bar
    plot_kr_bills_monthly(m_kr, '22대 국회 AI 법안 발의 추이 (월별)', 'fig_ts_kr_bills')

    # Individual attribute line charts
    plot_individual_attrs(q_guardian, q_nyt, 'Guardian vs NYT', 'fig_ts_attrs')

    print(f"\nDone.", flush=True)


if __name__ == "__main__":
    main()
