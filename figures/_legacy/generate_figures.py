"""보고서용 시각화 생성.

Usage:
    python generate_figures.py
"""
import json
import os
import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np

# Korean font
for f in fm.findSystemFonts():
    if 'malgun' in f.lower():
        plt.rcParams['font.family'] = fm.FontProperties(fname=f).get_name()
        break
plt.rcParams['axes.unicode_minus'] = False

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FIG_DIR = os.path.join(os.path.dirname(__file__), "figures")
os.makedirs(FIG_DIR, exist_ok=True)

ATTR_ORDER = [
    'Safety', 'Responsible AI', 'Market/Antitrust', 'Industrial policy',
    'National security', 'Public interest', 'Labor', 'Copyright',
    'Intl collaboration', 'Elections',
]
ATTR_KR = [
    'AI 안전', '책임/윤리 AI', '시장경쟁/독과점', '산업정책',
    '국가안보', '공익/소비자보호', '노동/고용', '저작권/지식재산',
    '국제협력', '선거/민주주의',
]
COLORS_ATTR = ['#ef5350','#ab47bc','#42a5f5','#66bb6a','#ff7043',
               '#26c6da','#ffa726','#ec407a','#8d6e63','#78909c']


def load_comparison():
    """Load 8-way comparison data."""
    with open(os.path.join(DATA_DIR, 'treemap_data.json'), encoding='utf-8') as f:
        data = json.load(f)
    return data


def fig1_8way_heatmap(data):
    """8-way comparison heatmap."""
    datasets = ['Guardian', 'NYT', 'Naver', 'US 118th', 'US 119th', 'KR 22nd', 'EU Act', 'EU Amds']

    # Build matrix from treemap_data
    matrix = []
    for attr_node in data['children']:
        name = attr_node['name']
        row = []
        news_pct = attr_node.get('news_pct', {})
        leg_pct = attr_node.get('leg_pct', {})
        row.append(news_pct.get('guardian', 0))
        row.append(news_pct.get('nyt', 0))
        row.append(news_pct.get('naver', 0))
        row.append(leg_pct.get('us118', 0))
        row.append(leg_pct.get('us119', 0))
        row.append(leg_pct.get('kr22', 0))
        # EU - need to compute from raw
        eu_act = 0
        eu_amd = 0
        matrix.append(row + [eu_act, eu_amd])

    # Load EU data separately
    with open(os.path.join(DATA_DIR, 'eu_ai_act_classified.json'), encoding='utf-8') as f:
        eu_art = [a for a in json.load(f) if a.get('primary') != 'Procedural/Administrative']
    with open(os.path.join(DATA_DIR, 'eu_amendments_classified.json'), encoding='utf-8') as f:
        eu_amd_data = [a for a in json.load(f) if a.get('primary') != 'Procedural/Administrative']

    from collections import Counter
    eu_art_c = Counter(a['primary'] for a in eu_art)
    eu_amd_c = Counter(a['primary'] for a in eu_amd_data)
    eu_art_total = sum(eu_art_c.values())
    eu_amd_total = sum(eu_amd_c.values())

    EN_ATTRS_FULL = [
        'Safety', 'Responsible and ethical AI',
        'Market efficiency and power concentration (antitrust)',
        'Industrial policy', 'National security', 'Public interest',
        'Labor', 'Copyright', 'International collaboration', 'Elections',
    ]

    # Rebuild matrix properly
    matrix = []
    attr_names_used = []
    for i, full in enumerate(EN_ATTRS_FULL):
        # Find matching node in treemap data
        node = None
        for n in data['children']:
            if n['name'] == ATTR_ORDER[i]:
                node = n
                break
        if not node:
            continue

        attr_names_used.append(ATTR_KR[i])
        news_pct = node.get('news_pct', {})
        leg_pct = node.get('leg_pct', {})
        eu_a = round(eu_art_c.get(full, 0) / eu_art_total * 100, 1) if eu_art_total else 0
        eu_m = round(eu_amd_c.get(full, 0) / eu_amd_total * 100, 1) if eu_amd_total else 0

        matrix.append([
            news_pct.get('guardian', 0), news_pct.get('nyt', 0), news_pct.get('naver', 0),
            leg_pct.get('us118', 0), leg_pct.get('us119', 0), leg_pct.get('kr22', 0),
            eu_a, eu_m
        ])

    matrix = np.array(matrix)

    fig, ax = plt.subplots(figsize=(12, 6))
    im = ax.imshow(matrix, cmap='YlOrRd', aspect='auto')

    ax.set_xticks(range(len(datasets)))
    ax.set_xticklabels(datasets, fontsize=10, rotation=30, ha='right')
    ax.set_yticks(range(len(attr_names_used)))
    ax.set_yticklabels(attr_names_used, fontsize=10)

    # Annotate
    for i in range(len(attr_names_used)):
        for j in range(len(datasets)):
            val = matrix[i, j]
            color = 'white' if val > 25 else 'black'
            ax.text(j, i, f'{val:.1f}', ha='center', va='center', fontsize=8, color=color)

    # Dividers
    ax.axvline(2.5, color='white', linewidth=2)
    ax.axvline(5.5, color='white', linewidth=2)
    ax.text(1, -0.8, '뉴스', ha='center', fontsize=11, fontweight='bold', color='#333')
    ax.text(4, -0.8, '법안', ha='center', fontsize=11, fontweight='bold', color='#333')
    ax.text(6.5, -0.8, 'EU', ha='center', fontsize=11, fontweight='bold', color='#333')

    plt.colorbar(im, ax=ax, label='비중 (%)', shrink=0.8)
    ax.set_title('AI 정책 속성별 뉴스·법안 비중 비교', fontsize=14, fontweight='bold', pad=20)
    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, 'fig1_heatmap.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, 'fig1_heatmap.pdf'), bbox_inches='tight')
    plt.close()
    print("  fig1_heatmap", flush=True)


def fig2_gap_korea():
    """Korea gap chart."""
    with open(os.path.join(DATA_DIR, 'treemap_kr.json'), encoding='utf-8') as f:
        kr = json.load(f)

    sorted_attrs = sorted(kr['children'], key=lambda x: x['gap'])
    names = [a['name'] for a in sorted_attrs]
    news = [a['news_pct'] for a in sorted_attrs]
    leg = [a['leg_pct'] for a in sorted_attrs]
    gaps = [a['gap'] for a in sorted_attrs]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6), gridspec_kw={'width_ratios': [3, 2]})

    y = np.arange(len(names))
    h = 0.35
    ax1.barh(y - h/2, news, h, label='네이버 뉴스 (%)', color='#42a5f5')
    ax1.barh(y + h/2, leg, h, label='22대 법안 (%)', color='#66bb6a')
    ax1.set_yticks(y)
    ax1.set_yticklabels(names, fontsize=10)
    ax1.set_xlabel('비중 (%)', fontsize=11)
    ax1.legend(fontsize=10)
    ax1.set_title('한국: 뉴스 vs 법안 비중', fontsize=13, fontweight='bold')
    ax1.invert_yaxis()

    colors = ['#e53935' if g > 0 else '#1565c0' for g in gaps]
    ax2.barh(y, gaps, color=colors)
    ax2.set_yticks(y)
    ax2.set_yticklabels(names, fontsize=10)
    ax2.set_xlabel('격차 (%p)', fontsize=11)
    ax2.axvline(0, color='gray', linewidth=0.5)
    ax2.set_title('공론-입법 격차', fontsize=13, fontweight='bold')
    ax2.invert_yaxis()

    for i, g in enumerate(gaps):
        ax2.text(g + (0.3 if g >= 0 else -0.3), i, f'{g:+.1f}',
                va='center', ha='left' if g >= 0 else 'right', fontsize=9)

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, 'fig2_gap_korea.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, 'fig2_gap_korea.pdf'), bbox_inches='tight')
    plt.close()
    print("  fig2_gap_korea", flush=True)


def fig3_gap_us():
    """US 118th vs 119th gap comparison."""
    attrs = ATTR_KR
    # Guardian+NYT average as news
    news_avg = [15.0, 14.4, 20.1, 9.8, 9.6, 13.6, 10.0, 4.5, 1.3, 4.6]
    us118 = [7.8, 27.9, 3.2, 11.7, 16.2, 15.6, 3.2, 1.9, 2.6, 9.7]
    us119 = [37.7, 3.8, 3.8, 5.7, 28.3, 13.2, 1.9, 1.9, 1.9, 1.9]

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    y = np.arange(len(attrs))
    h = 0.35

    for ax, leg_data, title, congress in zip(axes, [us118, us119],
            ['미국 118th Congress (2023-24)', '미국 119th Congress (2025-)'],
            ['118th', '119th']):
        gaps = [n - l for n, l in zip(news_avg, leg_data)]
        order = np.argsort(gaps)

        ax.barh(y - h/2, [news_avg[i] for i in order], h, label='Guardian+NYT 뉴스', color='#42a5f5')
        ax.barh(y + h/2, [leg_data[i] for i in order], h, label=f'{congress} 법안', color='#66bb6a')
        ax.set_yticks(y)
        ax.set_yticklabels([attrs[i] for i in order], fontsize=9)
        ax.set_xlabel('비중 (%)', fontsize=10)
        ax.legend(fontsize=9)
        ax.set_title(title, fontsize=12, fontweight='bold')
        ax.invert_yaxis()

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, 'fig3_gap_us.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, 'fig3_gap_us.pdf'), bbox_inches='tight')
    plt.close()
    print("  fig3_gap_us", flush=True)


def fig4_timeline():
    """News volume timeline."""
    with open(os.path.join(DATA_DIR, 'guardian_articles_raw.json'), encoding='utf-8') as f:
        guardian = json.load(f)
    with open(os.path.join(DATA_DIR, 'nyt_articles_raw.json'), encoding='utf-8') as f:
        nyt = json.load(f)

    from collections import Counter
    g_year = Counter(a['pub_date'][:4] for a in guardian)
    n_year = Counter(a['pub_date'][:4] for a in nyt)

    years = sorted(set(g_year.keys()) | set(n_year.keys()))
    years = [y for y in years if '2016' <= y <= '2025']

    fig, ax = plt.subplots(figsize=(10, 5))
    x = np.arange(len(years))
    w = 0.35
    ax.bar(x - w/2, [g_year.get(y, 0) for y in years], w, label='Guardian', color='#42a5f5')
    ax.bar(x + w/2, [n_year.get(y, 0) for y in years], w, label='NYT', color='#ff7043')

    ax.set_xticks(x)
    ax.set_xticklabels(years, fontsize=10)
    ax.set_ylabel('기사 수', fontsize=11)
    ax.set_title('연도별 AI 뉴스 기사 수 (Guardian vs NYT)', fontsize=13, fontweight='bold')
    ax.legend(fontsize=11)

    # ChatGPT annotation
    ax.annotate('ChatGPT\n출시', xy=(6.8, max(g_year.get('2023', 0), 500)),
                fontsize=9, ha='center', color='#e53935', fontweight='bold',
                arrowprops=dict(arrowstyle='->', color='#e53935'))

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, 'fig4_timeline.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, 'fig4_timeline.pdf'), bbox_inches='tight')
    plt.close()
    print("  fig4_timeline", flush=True)


def fig5_gap_cross_country():
    """Cross-country gap comparison — Market/Antitrust highlighted."""
    attrs_short = ['Safety', 'Resp.AI', 'Market', 'Industry', 'NatSec',
                   'Public', 'Labor', 'Copy.', 'Intl', 'Elec.']

    # Gaps: news% - leg%
    kr_gap = [-4.7, -1.2, 8.6, 8.8, 7.5, -21.5, 1.7, -2.0, 3.3, -0.4]
    us118_gap = [7.2, -13.5, 16.9, -1.9, -6.6, -2.0, 6.8, 2.6, -1.3, -5.1]
    us119_gap = [-22.7, 10.6, 16.3, 4.1, -18.7, 0.4, 8.1, 2.6, -0.6, 2.7]

    fig, ax = plt.subplots(figsize=(12, 6))
    x = np.arange(len(attrs_short))
    w = 0.25

    ax.bar(x - w, kr_gap, w, label='한국', color='#42a5f5')
    ax.bar(x, us118_gap, w, label='미국 118th', color='#66bb6a')
    ax.bar(x + w, us119_gap, w, label='미국 119th', color='#ff7043')

    ax.set_xticks(x)
    ax.set_xticklabels(attrs_short, fontsize=10, rotation=30, ha='right')
    ax.set_ylabel('격차 (%p) — 양수=뉴스>법안', fontsize=11)
    ax.axhline(0, color='gray', linewidth=0.5)
    ax.set_title('국가별 공론-입법 격차 비교', fontsize=14, fontweight='bold')
    ax.legend(fontsize=10)

    # Highlight Market
    ax.axvspan(1.5, 2.5, alpha=0.1, color='red')
    ax.annotate('3국 모두\n입법 공백', xy=(2, 17), fontsize=9, ha='center',
                color='#e53935', fontweight='bold')

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, 'fig5_cross_country.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, 'fig5_cross_country.pdf'), bbox_inches='tight')
    plt.close()
    print("  fig5_cross_country", flush=True)


def fig6_subtopic_overlap():
    """Subtopic overlap rate distribution."""
    with open(os.path.join(DATA_DIR, 'subtopics_overlap_rate.json'), encoding='utf-8') as f:
        overlap = json.load(f)

    rates = []
    for key, matches in overlap.items():
        for m in matches:
            rates.append(m['count'])

    from collections import Counter
    dist = Counter(rates)

    fig, ax = plt.subplots(figsize=(8, 4))
    x = [0, 1, 2, 3, 4, 5]
    vals = [dist.get(i, 0) for i in x]
    colors = ['#e53935', '#ef9a9a', '#ffcc80', '#c8e6c9', '#a5d6a7', '#66bb6a']
    bars = ax.bar(x, vals, color=colors, edgecolor='white')

    ax.set_xticks(x)
    ax.set_xticklabels(['0/5', '1/5', '2/5', '3/5', '4/5', '5/5'], fontsize=11)
    ax.set_xlabel('5회 반복 중 등장 횟수', fontsize=11)
    ax.set_ylabel('소주제 수', fontsize=11)
    ax.set_title('소주제 반복 안정성 분포 (N=140)', fontsize=13, fontweight='bold')

    for bar, val in zip(bars, vals):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
                    str(val), ha='center', fontsize=10, fontweight='bold')

    # Annotation
    stable = sum(1 for r in rates if r >= 3)
    ax.text(4, max(vals) * 0.8, f'3/5 이상: {stable}/140\n({stable/140*100:.0f}%)',
            fontsize=10, ha='center', color='#2e7d32',
            bbox=dict(boxstyle='round', facecolor='#e8f5e9', alpha=0.8))

    plt.tight_layout()
    plt.savefig(os.path.join(FIG_DIR, 'fig6_overlap.png'), dpi=200, bbox_inches='tight')
    plt.savefig(os.path.join(FIG_DIR, 'fig6_overlap.pdf'), bbox_inches='tight')
    plt.close()
    print("  fig6_overlap", flush=True)


def main():
    print("=== Generating figures ===", flush=True)
    data = load_comparison()
    fig1_8way_heatmap(data)
    fig2_gap_korea()
    fig3_gap_us()
    fig4_timeline()
    fig5_gap_cross_country()
    fig6_subtopic_overlap()
    print(f"\nAll saved to {FIG_DIR}/", flush=True)


if __name__ == "__main__":
    main()
