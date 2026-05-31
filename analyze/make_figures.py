"""보고서용 전체 그래프 생성 — 단일 정본 스크립트 (구 figures/regenerate_all.py).

법안 그림 (bill_loaders 경유):
  [fig01]  연도별 AI 관련 입법 추이 (KR)
  [fig01b] 22대 월별 AI 입법 추이 (2024.05~)
  [fig03]  대수별 정책 속성 구성 스택바 (KR 19~22대)
  [fig02b] 22대 개정 대상 법률 × 정책 속성 히트맵 (상위 10개)
  [fig03_eu] EU AI Act 조문 vs 수정안 속성 분포
  [fig05_6src] 한·미·EU 6소스 속성 비중 히트맵
  [fig06]  입법-공론 격차 비교 (KR22-KR뉴스, US119-NYT, EU Act-Guardian)

뉴스 그림 (news_descriptive 데이터 로더 경유 — news_analysis.duckdb cleaned subset):
  [fig04]      한·영·미 3소스 보도 속성 분포 (KR 뉴스 + Guardian + NYT)
  [report41a]  국내 6개 매체 10년 보도 추이 (월 stacked area + 12M MA)
  [report41b]  AI 기본법 통과 ±24개월 윈도우 (매체별 전/후 월평균)
  [report41c]  국내 매체별·연도별 정책 속성 구성
  [fig05_discourse_legislation_gap] KR22 입법 vs KR뉴스 격차

산출: figures/*.png + figures/figures_data.xlsx (편집자 인계용 수치).
실행: `python analyze/make_figures.py`. 데이터(news_descriptive)를 갱신한 뒤
그림 갱신이 필요하면 이 스크립트를 재실행한다.
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np
import json, os, re, sys
from collections import Counter, defaultdict
from openpyxl import Workbook

import _bootstrap  # noqa: F401  -- analyze/_bootstrap.py: repo root + analyze/ on sys.path
import config
from bill_loaders import load_kr_bills, load_us_bills, load_eu_bills
import news_descriptive as nd

sys.stdout.reconfigure(encoding='utf-8')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIG_DIR = os.path.join(ROOT, 'figures')  # 보고서가 고정 참조하는 그림 산출 디렉터리

# ── 그림 데이터 수치 엑셀 저장 ──
# 편집자가 새로 표·차트를 만들 때 참조할 수 있도록 모든 그림의 입력 수치를
# figures_data.xlsx의 시트별로 기록. 그림 추가/수정 시 add_data_sheet 호출 필수.
DATA_XLSX = os.path.join(FIG_DIR, 'figures_data.xlsx')
_data_wb = Workbook()
_data_wb.remove(_data_wb.active)

def add_data_sheet(name, header, rows, note=None):
    """그림에 사용된 수치를 엑셀 시트로 저장.
    name: 시트명 (예: 'fig01_연도별')
    header: 첫 행 (열 헤더 리스트)
    rows: 데이터 행 리스트
    note: 시트 상단에 들어갈 그림 캡션
    """
    ws = _data_wb.create_sheet(name[:31])
    if note:
        ws.append([note])
        ws.append([])
    ws.append(list(header))
    for r in rows:
        ws.append(list(r))

def _resolve_korean_font():
    """한글 TTF 경로 1개 반환. config.KO_FONT_PATH(캐시) → 시스템 → WSL 마운트 순.
    마운트본을 찾으면 캐시(KO_FONT_PATH)로 복사해 다음 실행부터 안정적으로 쓴다.
    못 찾으면 None (한글이 □로 보일 수 있음)."""
    cands = [
        config.KO_FONT_PATH,
        '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/mnt/d/backup_2025/나눔 글꼴/나눔고딕/NanumFontSetup_TTF_GOTHIC/NanumGothic.ttf',
    ]
    for c in cands:
        if not os.path.exists(c):
            continue
        if c != config.KO_FONT_PATH and not os.path.exists(config.KO_FONT_PATH):
            try:
                os.makedirs(os.path.dirname(config.KO_FONT_PATH), exist_ok=True)
                import shutil
                shutil.copy(c, config.KO_FONT_PATH)
                return config.KO_FONT_PATH
            except Exception:
                pass
        return c
    print('  [warn] 한글 폰트 미발견 — 그림의 한글이 깨질 수 있음', flush=True)
    return None

font_path = _resolve_korean_font()
plt.rcParams['axes.unicode_minus'] = False
if font_path:
    fm.fontManager.addfont(font_path)
    plt.rcParams['font.family'] = fm.FontProperties(fname=font_path).get_name()
    fp = fm.FontProperties(fname=font_path)
    fp13 = fm.FontProperties(fname=font_path, size=13)
else:
    fp = fm.FontProperties()
    fp13 = fm.FontProperties(size=13)

# EN→KR 정책 속성 라벨 (표 1 기준 단일 용어)
EN_TO_KR = {
    'Market efficiency and power concentration (antitrust)': '시장경쟁',
    'Safety': '안전성', 'Responsible and ethical AI': '책임과 윤리',
    'National security': '안보', 'Industrial policy': '산업정책',
    'Public interest': '공익', 'Labor': '노동',
    'Copyright': '저작권', 'International collaboration': '국제협력',
    'Elections': '선거', 'none': '미분류',
}
def kr_label(attr):
    return EN_TO_KR.get(attr, attr)

# ── 데이터 로드 ──
kr_bills = load_kr_bills(enrich=True)
us_bills = load_us_bills(enrich=True)
eu_bills = load_eu_bills(enrich=True)

with open(os.path.join(config.ANALYSIS_DIR, 'articles_classified_guardian.json'), encoding='utf-8') as f:
    guardian = json.load(f)
with open(os.path.join(config.ANALYSIS_DIR, 'articles_classified_nyt.json'), encoding='utf-8') as f:
    nyt = json.load(f)

# 대수별 KR 법안
by_age = defaultdict(list)
for b in kr_bills:
    by_age[b['age']].append(b)

kr22_dist = Counter(kr_label(b['primary']) for b in by_age[22])
kr22_total = len(by_age[22])


# ════════════════════════════════════════
# fig01: 연도별 AI 입법 추이
# ════════════════════════════════════════
print('fig01...', flush=True)

yr_count = Counter()
for b in kr_bills:
    d = b.get('propose_date', '') or ''
    if len(d) >= 4 and d[:4].isdigit():
        yr_count[int(d[:4])] += 1

years = sorted(yr_count.keys())
counts = [yr_count[y] for y in years]

# 2026 연간 추정 (4월 18일 기준, 108일 경과)
# 현재까지 실제 건수에서 선형 연장
import datetime
today = datetime.date.today()
days_elapsed = (today - datetime.date(today.year, 1, 1)).days + 1
cur_2026 = yr_count.get(2026, 0)
forecast_2026 = round(cur_2026 * 365 / days_elapsed) if cur_2026 > 0 and days_elapsed > 0 else cur_2026

fig, ax = plt.subplots(figsize=(14, 6))
bars = ax.bar(years, counts, color='#4472C4', width=0.7, edgecolor='white', linewidth=0.5)

events = {
    2018: ('AlphaGo\n이후\n착수기', 'red'),
    2023: ('ChatGPT\n확산', 'red'),
    2026: ('AI 기본법\n시행', 'darkgreen'),
}
for yr, (label, color) in events.items():
    if yr in years:
        idx = years.index(yr)
        ax.annotate(label, xy=(yr, counts[idx]), xytext=(yr, counts[idx] + 15),
                    ha='center', fontsize=11, fontproperties=fp, color=color,
                    arrowprops=dict(arrowstyle='->', color=color, lw=1.5))

# 대수 구분 음영
spans = [
    (2016.5, 2020.5, '20대'),
    (2020.5, 2024.5, '21대'),
    (2024.5, 2026.5, '22대'),
]
for x0, x1, label in spans:
    ax.axvspan(x0, x1, alpha=0.1, color='gray', zorder=0)
    ax.text((x0 + x1) / 2, max(counts) * 0.95, label,
            ha='center', fontsize=13, fontproperties=fp, alpha=0.5)

for bar, val in zip(bars, counts):
    if val > 0:
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                str(val), ha='center', va='bottom', fontsize=10, fontweight='bold')

# 2026 forecast 점선
if forecast_2026 > cur_2026:
    ax.bar(2026, forecast_2026 - cur_2026, width=0.7, bottom=cur_2026,
           color='none', edgecolor='#4472C4', linewidth=2, linestyle='--', zorder=5)
    ax.text(2026, forecast_2026 + 2, f'~{forecast_2026}\n(연간 추정)',
            ha='center', va='bottom', fontsize=10, fontproperties=fp,
            color='#4472C4', fontstyle='italic')

ax.set_xlabel('연도', fontproperties=fp, fontsize=15)
ax.set_ylabel('AI 관련 법안 수', fontproperties=fp, fontsize=15)
ax.set_title(f'연도별 AI 관련 입법 추이 (KR 19~22대, 누적 {len(kr_bills)}건)',
             fontproperties=fp, fontsize=17, fontweight='bold')
ax.set_xticks(years)
ax.tick_params(axis='both', labelsize=11)
ax.set_xlim(min(years) - 0.6, max(years) + 0.6)
ax.set_ylim(0, max(max(counts), forecast_2026) * 1.2)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig01_ai_legislation_trend.png'), dpi=200, bbox_inches='tight')
plt.close()

_rows = [(y, c, '실측') for y, c in zip(years, counts)]
if forecast_2026 > cur_2026:
    _rows.append((2026, forecast_2026, f'연간 추정 (실측 {cur_2026}건 → 365/{days_elapsed}일 환산)'))
add_data_sheet('fig01_연도별_입법',
               ['연도', '법안수', '비고'], _rows,
               note='[그림 1] 연도별 AI 관련 입법 추이 (KR 19~22대)')
print('  fig01 done')


# ════════════════════════════════════════
# fig01b: 22대 월별 AI 입법 추이
# ════════════════════════════════════════
print('fig01b...', flush=True)

bills_22 = by_age[22]
month_count = Counter()
for b in bills_22:
    d = b.get('propose_date', '') or ''
    if len(d) >= 7:
        month_count[d[:7]] += 1

START = datetime.date(2024, 5, 1)
end_d = max((datetime.date(int(m[:4]), int(m[5:7]), 1) for m in month_count), default=START)
months_seq = []
y, m = START.year, START.month
while (y, m) <= (end_d.year, end_d.month):
    months_seq.append(f'{y:04d}-{m:02d}')
    m += 1
    if m > 12:
        m = 1; y += 1

vals = [month_count.get(mm, 0) for mm in months_seq]
cum = np.cumsum(vals)

fig, ax = plt.subplots(figsize=(14, 6))
x = np.arange(len(months_seq))
bars = ax.bar(x, vals, color='#4472C4', width=0.75, edgecolor='white', linewidth=0.5, zorder=2, label='월별 발의')

for xi, v in zip(x, vals):
    if v > 0:
        ax.text(xi, v + 0.3, str(v), ha='center', va='bottom', fontsize=9, fontweight='bold')

ax2 = ax.twinx()
ax2.plot(x, cum, 'o-', color='#d9534f', linewidth=2, markersize=5, zorder=3, label='누적')
ax2.set_ylabel('누적 법안 수', fontproperties=fp, fontsize=12, color='#d9534f')
ax2.tick_params(axis='y', labelsize=10, colors='#d9534f')
ax2.set_ylim(0, max(cum) * 1.15 if len(cum) else 10)

milestones = {
    '2024-12': ('AI 기본법\n국회 통과\n(2024.12.26)', 'darkgreen'),
    '2026-01': ('AI 기본법\n시행\n(2026.01.22)', 'darkgreen'),
}
y_top = max(vals) * 1.3 if vals else 5
for mm, (label, color) in milestones.items():
    if mm in months_seq:
        idx = months_seq.index(mm)
        ax.annotate(label, xy=(idx, vals[idx]), xytext=(idx, y_top),
                    ha='center', fontsize=10, fontproperties=fp, color=color,
                    arrowprops=dict(arrowstyle='->', color=color, lw=1.3))

ax.set_xticks(x)
ax.set_xticklabels([mm[2:] for mm in months_seq], rotation=45, ha='right', fontsize=9)
ax.set_xlabel('월 (YY-MM)', fontproperties=fp, fontsize=13)
ax.set_ylabel('월별 법안 수', fontproperties=fp, fontsize=13, color='#4472C4')
ax.tick_params(axis='y', labelsize=10, colors='#4472C4')
ax.set_ylim(0, y_top * 1.15)
ax.set_title(f'22대 국회 월별 AI 입법 추이 (2024.05~{months_seq[-1]}, 총 {sum(vals)}건)',
             fontproperties=fp, fontsize=16, fontweight='bold')
ax.spines['top'].set_visible(False)
ax.grid(axis='y', alpha=0.25, zorder=1)

# 범례 (양 축 결합)
lines1, labels1 = ax.get_legend_handles_labels()
lines2, labels2 = ax2.get_legend_handles_labels()
ax.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=11, prop=fp)

plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig01b_kr22_monthly_trend.png'), dpi=200, bbox_inches='tight')
plt.close()

add_data_sheet('fig02_22대_월별',
               ['월(YYYY-MM)', '월별 발의', '누적'],
               [(m, int(v), int(c)) for m, v, c in zip(months_seq, vals, cum)],
               note='[그림 2] 22대 국회 월별 AI 입법 추이 (2024.05~)')
print('  fig01b done')


# ════════════════════════════════════════
# fig03: 대수별 정책 속성 구성 절대수 스택바 (막대 길이가 N에 비례)
#   (구: 표 2 + 10-subplot 그림을 대체)
# ════════════════════════════════════════
print('fig03...', flush=True)

colors_attr = {
    '산업정책': '#66bb6a', '공익': '#26c6da', '책임과 윤리': '#ab47bc',
    '안전성': '#ef5350', '선거': '#78909c', '저작권': '#ec407a',
    '노동': '#ffa726', '안보': '#ff7043', '국제협력': '#8d6e63',
    '시장경쟁': '#42a5f5'
}

ages_c = [19, 20, 21, 22]
age_labels_c = ['19대', '20대', '21대', '22대']
attrs_c = ['산업정책', '공익', '책임과 윤리', '안전성', '안보',
           '선거', '저작권', '노동', '시장경쟁', '국제협력']

age_totals_c = [len(by_age[a]) for a in ages_c]
count_matrix = np.zeros((len(attrs_c), len(ages_c)), dtype=int)
for j, a in enumerate(ages_c):
    dist = Counter(kr_label(b['primary']) for b in by_age[a])
    for i, attr in enumerate(attrs_c):
        count_matrix[i, j] = dist.get(attr, 0)

x_max = max(age_totals_c)
fig, ax = plt.subplots(figsize=(14, 5))
y = np.arange(len(ages_c))
left = np.zeros(len(ages_c))
for i, attr in enumerate(attrs_c):
    color = colors_attr.get(attr, '#888')
    row = count_matrix[i]
    ax.barh(y, row, left=left, color=color, edgecolor='white',
            linewidth=0.8, label=attr, height=0.65)
    # 세그먼트 내 라벨 (절대수 5건 이상)
    for j, v in enumerate(row):
        if v >= 5:
            ax.text(left[j] + v/2, y[j], f'{v}',
                    ha='center', va='center', fontsize=9,
                    fontweight='bold', color='white')
    left += row

# 막대 끝에 총 N
for j, (a, total) in enumerate(zip(ages_c, age_totals_c)):
    ax.text(total + x_max * 0.01, y[j], f'N = {total}',
            ha='left', va='center', fontproperties=fp, fontsize=11,
            fontweight='bold')

# 19대 "발의 없음" 표시
if age_totals_c[0] == 0:
    ax.text(x_max * 0.05, y[0], '(19대: AI 관련 법률안 없음)',
            ha='left', va='center', fontproperties=fp, fontsize=11,
            color='#555', fontstyle='italic')

ax.set_yticks(y)
ax.set_yticklabels(age_labels_c, fontproperties=fp, fontsize=13)
ax.invert_yaxis()
ax.set_xlabel('AI 관련 법률안 수 (건)', fontproperties=fp, fontsize=12)
ax.set_xlim(0, x_max * 1.1)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.set_title('대한민국 국회 대수별 AI 법률안 정책 속성 구성',
             fontproperties=fp, fontsize=15, fontweight='bold')

ax.legend(prop=fp, loc='lower center', bbox_to_anchor=(0.5, -0.35),
          ncol=5, frameon=False, fontsize=10)

plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig03_kr_composition_stacked.png'),
            dpi=200, bbox_inches='tight')
plt.close()

_rows = []
for i, attr in enumerate(attrs_c):
    _rows.append([attr] + [int(count_matrix[i, j]) for j in range(len(ages_c))])
_rows.append(['총계 (N)'] + age_totals_c)
add_data_sheet('fig03_대수별_속성구성',
               ['속성'] + age_labels_c, _rows,
               note='[그림 3] KR 대수별 AI 법률안 정책 속성 구성 (절대수)')
print('  fig03 stacked bar done')


# ════════════════════════════════════════
# fig02b: 22대 개정 대상 법률 × 정책 속성 히트맵 (상위 10개)
# ════════════════════════════════════════
print('fig02b...', flush=True)

def base_law(title):
    t = title.replace(' 일부개정법률안', '').replace(' 전부개정법률안', '')
    t = t.replace(' 제정안', '').replace('법률안', '법')
    t = re.sub(r'법안$', '법', t)
    return re.sub(r'\s+', ' ', t).strip()

BASE_SHORT = {
    '인공지능 발전과 신뢰 기반 조성 등에 관한 기본법': 'AI 기본법',
    '인공지능산업 육성 및 신뢰 확보에 관한 법률': 'AI 산업육성법',
    '인공지능 산업 육성 및 신뢰 확보에 관한 법률': 'AI 산업육성법',
    '정보통신망 이용촉진 및 정보보호 등에 관한 법률': '정통망법',
    '개인정보 보호법': '개인정보보호법',
    '전자상거래 등에서의 소비자보호에 관한 법률': '전자상거래소비자보호법',
    '공공데이터의 제공 및 이용 활성화에 관한 법률': '공공데이터법',
    '식품 등의 표시ㆍ광고에 관한 법률': '식품표시광고법',
    '분산에너지 활성화 특별법': '분산에너지특별법',
    '초·중등교육법': '초중등교육법',
}
def short(bl):
    return BASE_SHORT.get(bl, bl[:20])

ATTRS_HM = ['Industrial policy', 'Public interest', 'Responsible and ethical AI',
            'Copyright', 'Safety', 'Elections', 'Labor', 'National security']
ATTRS_HM_KR = ['산업정책', '공익', '책임과 윤리', '저작권', '안전성',
               '선거', '노동', '안보']

mat = defaultdict(lambda: Counter())
totals = Counter()
for b in by_age[22]:
    bl = base_law(b['title'])
    attr = b.get('primary', '?')
    if attr not in ATTRS_HM:
        continue
    mat[bl][attr] += 1
    totals[bl] += 1

top_laws = [bl for bl, _ in totals.most_common(10)]
data = np.zeros((len(top_laws), len(ATTRS_HM)), dtype=int)
for i, bl in enumerate(top_laws):
    for j, a in enumerate(ATTRS_HM):
        data[i, j] = mat[bl][a]

fig, ax = plt.subplots(figsize=(10, 7))
im = ax.imshow(data, cmap='YlOrRd', aspect='auto')

ax.set_xticks(range(len(ATTRS_HM)))
ax.set_xticklabels(ATTRS_HM_KR, rotation=35, ha='right', fontsize=10, fontproperties=fp)
ax.set_yticks(range(len(top_laws)))
ax.set_yticklabels([f'{short(bl)}  ({totals[bl]})' for bl in top_laws], fontsize=10, fontproperties=fp)

for i in range(len(top_laws)):
    for j in range(len(ATTRS_HM)):
        v = data[i, j]
        if v > 0:
            color = 'white' if v >= 6 else 'black'
            ax.text(j, i, str(v), ha='center', va='center', color=color,
                    fontsize=10, fontweight='bold')

ax.set_title(f'22대 AI 법안: 개정 대상 법률 × 정책 속성 (상위 10개, N={kr22_total})',
             fontproperties=fp, fontsize=13, fontweight='bold', pad=12)
cbar = plt.colorbar(im, ax=ax, shrink=0.7)
cbar.set_label('법안 수', fontsize=10, fontproperties=fp)
plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig02b_kr22_law_attr_heatmap.png'), dpi=200, bbox_inches='tight')
plt.close()

_rows = []
for i, bl in enumerate(top_laws):
    _rows.append([short(bl)] + [int(data[i, j]) for j in range(len(ATTRS_HM))] + [totals[bl]])
add_data_sheet('fig04_22대_법률x속성',
               ['법률(상위10)'] + ATTRS_HM_KR + ['해당 법률 총계'], _rows,
               note='[그림 4] 22대 AI 법률안 개정 대상 법률 × 정책 속성 (상위 10개)')
print('  fig02b done')


# ════════════════════════════════════════
# fig03: EU AI Act 조문 vs 수정안의 속성 분포
# ════════════════════════════════════════
print('fig03...', flush=True)

eu_by_type = defaultdict(list)
for b in eu_bills:
    eu_by_type[b['type']].append(b)

ATTRS_EN = ['Safety', 'Responsible and ethical AI', 'Public interest',
            'International collaboration', 'Industrial policy',
            'Market efficiency and power concentration (antitrust)',
            'National security', 'Labor', 'Elections', 'Copyright']
ATTRS_EU_KR = ['안전성', '책임과 윤리', '공익', '국제협력', '산업정책',
               '시장경쟁', '안보', '노동', '선거', '저작권']

art_dist = Counter(b['primary'] for b in eu_by_type['article'])
am_dist = Counter(b['primary'] for b in eu_by_type['amendment'])
art_total = len(eu_by_type['article'])
am_total = len(eu_by_type['amendment'])

art_pct = [art_dist.get(a, 0) / art_total * 100 if art_total else 0 for a in ATTRS_EN]
am_pct = [am_dist.get(a, 0) / am_total * 100 if am_total else 0 for a in ATTRS_EN]

fig, ax = plt.subplots(figsize=(13, 7))
y = np.arange(len(ATTRS_EU_KR))
height = 0.4

bars_art = ax.barh(y + height/2, art_pct, height,
                   label=f'AI Act 조문 (N={art_total})', color='#2E86AB', edgecolor='white')
bars_am = ax.barh(y - height/2, am_pct, height,
                  label=f'유럽의회 수정안 (N={am_total})', color='#A23B72', edgecolor='white')

for bar, pct in zip(bars_art, art_pct):
    if pct > 0.3:
        ax.text(bar.get_width() + 0.8, bar.get_y() + bar.get_height()/2,
                f'{pct:.1f}%', va='center', fontsize=9)
for bar, pct in zip(bars_am, am_pct):
    if pct > 0.3:
        ax.text(bar.get_width() + 0.8, bar.get_y() + bar.get_height()/2,
                f'{pct:.1f}%', va='center', fontsize=9)

ax.set_yticks(y)
ax.set_yticklabels(ATTRS_EU_KR, fontproperties=fp, fontsize=12)
ax.set_xlabel('비율 (%)', fontproperties=fp, fontsize=14)
ax.set_title('EU AI Act 조문 vs 유럽의회 수정안 — 정책 속성 분포',
             fontproperties=fp, fontsize=16, fontweight='bold')
ax.legend(prop=fp13, loc='lower right')
ax.invert_yaxis()
ax.tick_params(axis='x', labelsize=11)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.set_xlim(0, max(max(art_pct), max(am_pct)) * 1.15)

plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig03_eu_article_vs_amendment.png'), dpi=200, bbox_inches='tight')
plt.close()

_rows = []
for i, attr in enumerate(ATTRS_EU_KR):
    _rows.append([attr,
                  art_dist.get(ATTRS_EN[i], 0),
                  round(art_pct[i], 2),
                  am_dist.get(ATTRS_EN[i], 0),
                  round(am_pct[i], 2)])
add_data_sheet('fig06_EU_조문vs수정안',
               ['속성', f'AI Act 조문 건수 (N={art_total})', '비율 (%)',
                f'유럽의회 수정안 건수 (N={am_total})', '비율 (%)'],
               _rows,
               note='[그림 6] EU AI Act 조문 vs 유럽의회 수정안 정책 속성 분포')
print('  fig03 done')


# ════════════════════════════════════════
# fig05: 6소스 × 10속성 정책 속성 비중 히트맵 (구 표 3 대체)
# ════════════════════════════════════════
print('fig05 heatmap...', flush=True)

SIX_SRC_ATTRS_EN = ['Industrial policy', 'Public interest', 'Responsible and ethical AI',
                    'Safety', 'National security', 'Elections', 'Copyright', 'Labor',
                    'Market efficiency and power concentration (antitrust)', 'International collaboration']
SIX_SRC_ATTRS_KR = ['산업정책', '공익', '책임과 윤리', '안전성', '안보',
                    '선거', '저작권', '노동', '시장경쟁', '국제협력']

# 6 sources
by_age_kr = defaultdict(list)
for b in kr_bills:
    by_age_kr[b['age']].append(b)
by_cg_us = defaultdict(list)
for b in us_bills:
    by_cg_us[b.get('congress')].append(b)
eu_art_list = [b for b in eu_bills if b.get('type') == 'article']
eu_am_list = [b for b in eu_bills if b.get('type') == 'amendment']

src_groups = [
    ('韓 21대', by_age_kr[21]),
    ('韓 22대', by_age_kr[22]),
    ('美 118', by_cg_us[118]),
    ('美 119', by_cg_us[119]),
    ('EU Act', eu_art_list),
    ('EU 수정안', eu_am_list),
]

# 행렬: [속성 idx, 소스 idx] = %
mat = np.zeros((len(SIX_SRC_ATTRS_EN), len(src_groups)))
totals = []
for si, (sname, slist) in enumerate(src_groups):
    dist = Counter(b['primary'] for b in slist)
    total = len(slist)
    totals.append(total)
    for ai, attr_en in enumerate(SIX_SRC_ATTRS_EN):
        mat[ai, si] = dist.get(attr_en, 0) / total * 100 if total else 0

src_labels_with_n = [f'{name}\n(N={t})' for (name, _), t in zip(src_groups, totals)]

# --- A. 히트맵 ---
fig, ax = plt.subplots(figsize=(10, 8))
im = ax.imshow(mat, cmap='YlOrRd', aspect='auto', vmin=0, vmax=80)
ax.set_xticks(np.arange(len(src_groups)))
ax.set_xticklabels(src_labels_with_n, fontproperties=fp, fontsize=11)
ax.set_yticks(np.arange(len(SIX_SRC_ATTRS_KR)))
ax.set_yticklabels(SIX_SRC_ATTRS_KR, fontproperties=fp, fontsize=12)

for ai in range(len(SIX_SRC_ATTRS_KR)):
    for si in range(len(src_groups)):
        v = mat[ai, si]
        if v == 0:
            txt = '—'
            color = '#999'
        else:
            txt = f'{v:.1f}'
            color = 'white' if v > 40 else 'black'
        ax.text(si, ai, txt, ha='center', va='center',
                fontsize=10, color=color, fontweight='bold' if v > 15 else 'normal')

ax.set_title('정책 속성별 비중 히트맵 (한·미·EU 6 소스)',
             fontproperties=fp, fontsize=15, fontweight='bold')
cbar = plt.colorbar(im, ax=ax, shrink=0.7, pad=0.02)
cbar.set_label('비중 (%)', fontproperties=fp, fontsize=11)
cbar.ax.tick_params(labelsize=9)
ax.set_xticks(np.arange(len(src_groups) + 1) - 0.5, minor=True)
ax.set_yticks(np.arange(len(SIX_SRC_ATTRS_KR) + 1) - 0.5, minor=True)
ax.grid(which='minor', color='white', linewidth=1.5)
ax.tick_params(which='minor', length=0)
plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig05_6src_heatmap.png'),
            dpi=200, bbox_inches='tight')
plt.close()

_src_names = [g[0] for g in src_groups]
_header = ['속성'] + [f'{n} (N={t})' for n, t in zip(_src_names, totals)]
_rows = []
for ai, attr in enumerate(SIX_SRC_ATTRS_KR):
    _rows.append([attr] + [round(mat[ai, si], 2) for si in range(len(src_groups))])
add_data_sheet('fig05_6소스_히트맵',
               _header, _rows,
               note='[그림 5] 한·미·EU 6 소스 정책 속성 비중 히트맵 (단위: %)')
print('  fig05 heatmap done')


# ════════════════════════════════════════
# fig04: 뉴스 3소스 coverage (국내 6개 매체 + Guardian + NYT)
#   국내 뉴스는 news_analysis.duckdb cleaned subset (76,645건 전량 분류, none 제외).
# ════════════════════════════════════════
print('fig04...', flush=True)

def news_dist(articles):
    raw = Counter(kr_label(a.get('primary', 'none')) for a in articles)
    raw.pop('미분류', None)
    return raw

def kr_news_dist():
    """국내 6개 매체 1차 속성 분포 — 한글 키, none 제외 (g_dist/n_dist와 동형 Counter).
    nd.load_attr_distribution()는 영문 키 반환 → kr_label로 한글 변환해 맞춘다."""
    out = Counter()
    for attr_en, cnt, _share in nd.load_attr_distribution():
        if attr_en == 'none':
            continue
        out[kr_label(attr_en)] = cnt
    return out

g_dist = news_dist(guardian)
n_dist = news_dist(nyt)
kr_dist = kr_news_dist()

_ALL_CATS = ['시장경쟁', '안전성', '책임과 윤리', '안보', '산업정책',
             '공익', '노동', '저작권', '국제협력', '선거']
# 국내 뉴스 빈도 내림차순 (국내 산업정책 편향을 전면에 — 보고서 §4.1 그림8 서술 순서)
NEWS_CATS = sorted(_ALL_CATS, key=lambda c: -kr_dist.get(c, 0))

def to_pct(d, cats):
    t = sum(d.get(c, 0) for c in cats)
    return [d.get(c, 0) / t * 100 if t > 0 else 0 for c in cats]

kr_pct = to_pct(kr_dist, NEWS_CATS)
g_pct = to_pct(g_dist, NEWS_CATS)
n_pct = to_pct(n_dist, NEWS_CATS)

_kr_n = sum(kr_dist.get(c, 0) for c in NEWS_CATS)
_g_n = sum(g_dist.get(c, 0) for c in NEWS_CATS)
_n_n = sum(n_dist.get(c, 0) for c in NEWS_CATS)

fig, ax = plt.subplots(figsize=(14, 7))
x = np.arange(len(NEWS_CATS))
width = 0.27
ax.bar(x - width, kr_pct, width, label=f'국내 6개 매체 ({_kr_n:,}건)',
       color='#E8973A', edgecolor='white')
ax.bar(x, g_pct, width, label=f'Guardian ({_g_n:,}건)',
       color='#2E86AB', edgecolor='white')
ax.bar(x + width, n_pct, width, label=f'NYT ({_n_n:,}건)',
       color='#A23B72', edgecolor='white')

ax.set_xlabel('정책 속성', fontproperties=fp, fontsize=15)
ax.set_ylabel('비율 (%)', fontproperties=fp, fontsize=15)
ax.set_title('AI 관련 언론 보도의 정책 속성별 분포 (국내 cleaned subset vs 영·미 제목필터, none 제외)',
             fontproperties=fp, fontsize=15, fontweight='bold')
ax.set_xticks(x)
ax.set_xticklabels(NEWS_CATS, fontproperties=fp, fontsize=11, rotation=30, ha='right')
ax.tick_params(axis='y', labelsize=12)
ax.legend(prop=fp13)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.set_ylim(0, max(max(kr_pct), max(g_pct), max(n_pct)) * 1.2)

plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig04_news_coverage.png'), dpi=200, bbox_inches='tight')
plt.close()

_rows = []
for i, c in enumerate(NEWS_CATS):
    _rows.append([c,
                  kr_dist.get(c, 0), round(kr_pct[i], 2),
                  g_dist.get(c, 0), round(g_pct[i], 2),
                  n_dist.get(c, 0), round(n_pct[i], 2)])
add_data_sheet('fig04_뉴스_3소스',
               ['속성',
                f'국내 건수 (N={_kr_n:,})', '비율 (%)',
                f'Guardian 건수 (N={_g_n:,})', '비율 (%)',
                f'NYT 건수 (N={_n_n:,})', '비율 (%)'],
               _rows,
               note='[그림 8] 뉴스 3소스 (국내 6개 매체 / Guardian / NYT) 정책 속성 분포 (none 제외)')
print('  fig04 done')


# ════════════════════════════════════════
# fig05_discourse_legislation_gap: KR22 입법 vs 국내 뉴스 공론 (§4.4 그림8)
#   국내 뉴스 76,645건 전량 재분류 완료로 활성화 (구: 재분류 대기 SKIP).
#   입법 분포=by_age[22], 공론 분포=국내 cleaned subset (kr_dist, fig04에서 정의).
# ════════════════════════════════════════
print('fig05 discourse-legislation gap...', flush=True)

CATS10 = ['산업정책', '공익', '안전성', '책임과 윤리',
          '시장경쟁', '안보', '저작권', '노동',
          '선거', '국제협력']

kr_leg_total = sum(kr22_dist.get(c, 0) for c in CATS10)
kr_news_total = sum(kr_dist.get(c, 0) for c in CATS10)
_kr_leg_pct = {c: (kr22_dist.get(c, 0) / kr_leg_total * 100 if kr_leg_total else 0) for c in CATS10}
_kr_news_pct = {c: (kr_dist.get(c, 0) / kr_news_total * 100 if kr_news_total else 0) for c in CATS10}
_g5_order = sorted(CATS10, key=lambda c: -_kr_leg_pct[c])

fig, ax = plt.subplots(figsize=(11, 7))
_y = np.arange(len(_g5_order))
_h = 0.38
_leg_vals = [_kr_leg_pct[c] for c in _g5_order]
_news_vals = [_kr_news_pct[c] for c in _g5_order]
ax.barh(_y - _h / 2, _leg_vals, _h, label=f'22대 입법 (N={kr_leg_total})',
        color='#4472C4', edgecolor='white')
ax.barh(_y + _h / 2, _news_vals, _h, label=f'국내 뉴스 (N={kr_news_total:,})',
        color='#E8973A', edgecolor='white')
for _yi, c in enumerate(_g5_order):
    _gap = _kr_leg_pct[c] - _kr_news_pct[c]
    _xm = max(_kr_leg_pct[c], _kr_news_pct[c])
    ax.text(_xm + 0.8, _yi, f'{_gap:+.1f}%p', va='center', fontsize=9,
            fontproperties=fp, color='#1f4e79' if _gap >= 0 else '#c0392b')
ax.set_yticks(_y)
ax.set_yticklabels(_g5_order, fontproperties=fp, fontsize=12)
ax.invert_yaxis()
ax.set_xlabel('비중 (%, 미분류 제외)', fontproperties=fp, fontsize=13)
ax.set_title('국내 AI 입법(22대) vs 공론(국내 뉴스) 정책 속성 비교\n'
             '(우측 %p = 입법 - 공론, 양수=입법 추월)',
             fontproperties=fp, fontsize=14, fontweight='bold')
ax.legend(prop=fp13, loc='lower right')
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.set_xlim(0, max(max(_leg_vals), max(_news_vals)) * 1.18)
plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig05_discourse_legislation_gap.png'),
            dpi=200, bbox_inches='tight')
plt.close()

_rows = [[c, round(_kr_leg_pct[c], 2), round(_kr_news_pct[c], 2),
          round(_kr_leg_pct[c] - _kr_news_pct[c], 2)] for c in _g5_order]
add_data_sheet('fig05_KR입법vs공론',
               ['속성', f'22대 입법 % (N={kr_leg_total})',
                f'국내 뉴스 % (N={kr_news_total:,})', '격차 %p(입법-공론)'],
               _rows,
               note='[그림 8/§4.4] KR22 입법 vs 국내 뉴스 정책 속성 격차 (입법-공론)')
print('  fig05 done')

# ════════════════════════════════════════
# fig06: 입법-공론 격차 비교 (US119-NYT, EU Act-Guardian)
# KR22-한국뉴스 페어는 한국 뉴스 재분류 후 추가.
# ════════════════════════════════════════
print('fig06...', flush=True)

# 입법 분포
us119_bills = [b for b in us_bills if b.get('congress') == 119]
us119_dist = Counter(kr_label(b['primary']) for b in us119_bills)
us119_total = sum(us119_dist.get(c, 0) for c in CATS10)

eu_act_bills = [b for b in eu_bills if b.get('type') == 'article']
eu_act_dist = Counter(kr_label(b['primary']) for b in eu_act_bills)
eu_act_total = sum(eu_act_dist.get(c, 0) for c in CATS10)

# 뉴스 분포 (g_dist=guardian, n_dist=nyt는 fig04에서 이미 정의됨)
nyt_total_c = sum(n_dist.get(c, 0) for c in CATS10)
guardian_total_c = sum(g_dist.get(c, 0) for c in CATS10)
kr22_total_c = sum(kr22_dist.get(c, 0) for c in CATS10)

def gap_table(leg_dist, leg_total, news_dist_, news_total_):
    """각 속성에 대해 (입법%, 뉴스%, 격차=입법-뉴스) 반환"""
    out = {}
    for c in CATS10:
        lp = leg_dist.get(c, 0) / leg_total * 100 if leg_total else 0
        npct = news_dist_.get(c, 0) / news_total_ * 100 if news_total_ else 0
        out[c] = (lp, npct, lp - npct)
    return out

kr_gaps = gap_table(kr22_dist, kr22_total_c, kr_dist, kr_news_total)
us_gaps = gap_table(us119_dist, us119_total, n_dist, nyt_total_c)
eu_gaps = gap_table(eu_act_dist, eu_act_total, g_dist, guardian_total_c)

# 정렬: 3국 평균 절대 격차 큰 순
def mean_abs_gap(c):
    return (abs(kr_gaps[c][2]) + abs(us_gaps[c][2]) + abs(eu_gaps[c][2])) / 3
order = sorted(CATS10, key=mean_abs_gap, reverse=True)

regions = [
    ('한국', '22대 입법', '국내뉴스', kr_gaps, kr22_total_c, kr_news_total),
    ('미국', '119대 입법', 'NYT', us_gaps, us119_total, nyt_total_c),
    ('EU', 'AI Act 조문', '가디언', eu_gaps, eu_act_total, guardian_total_c),
]

# heatmap matrix: rows=속성, cols=국가
mat = np.array([[g[c][2] for _, _, _, g, _, _ in regions] for c in order])

# 색상 스케일: EU 안전성 +69.7 outlier로 인해 ±20에서 클립 (그래야 다른 셀 색상이 살아남)
VCLIP = 20

fig, ax = plt.subplots(figsize=(11, 7.5))
im = ax.imshow(mat, cmap='RdBu_r', vmin=-VCLIP, vmax=VCLIP, aspect='auto')

# x축 라벨 (국가 + 표본 크기)
ax.set_xticks(range(len(regions)))
ax.set_xticklabels([f'{r}\n{ln_name} N={ln:,}\n{nn_name} N={nn:,}'
                    for r, ln_name, nn_name, _, ln, nn in regions],
                   fontproperties=fp, fontsize=11)
# y축 라벨
ax.set_yticks(range(len(order)))
ax.set_yticklabels(order, fontproperties=fp, fontsize=12)

# 셀 값 텍스트
for i, c in enumerate(order):
    for j, (_, _, _, g, _, _) in enumerate(regions):
        v = g[c][2]
        text_color = 'white' if abs(v) > VCLIP * 0.55 else 'black'
        weight = 'bold' if abs(v) >= 5 else 'normal'
        ax.text(j, i, f'{v:+.1f}', ha='center', va='center',
                color=text_color, fontsize=12, fontweight=weight,
                fontproperties=fp)

# 색상바
cbar = plt.colorbar(im, ax=ax, fraction=0.04, pad=0.03, extend='both')
cbar.set_label('격차 (입법% - 뉴스%) %p', fontproperties=fp, fontsize=11)
cbar.ax.tick_params(labelsize=10)

# 셀 사이 흰선
ax.set_xticks(np.arange(-0.5, len(regions), 1), minor=True)
ax.set_yticks(np.arange(-0.5, len(order), 1), minor=True)
ax.grid(which='minor', color='white', linewidth=2)
ax.tick_params(which='minor', length=0)

ax.set_title('한·미·EU 입법-공론 격차 비교\n빨강: 입법이 공론을 추월     파랑: 공론이 입법을 추월',
             fontproperties=fp, fontsize=13, fontweight='bold', pad=12)

# outlier 주석 (EU 안전성 +69.7)
fig.text(0.5, -0.02,
         '* 색상 척도는 ±20%p에서 클립 (EU 안전성 실값 +69.7%p, 한국·미국 안보 등 모든 외부값은 텍스트로 표기)',
         ha='center', fontproperties=fp, fontsize=9, color='gray')

plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'fig06_gap_3region.png'),
            dpi=200, bbox_inches='tight')
plt.close()

_rows = []
for c in CATS10:
    kr_l, kr_n_, kr_g = kr_gaps[c]
    us_l, us_n_, us_g = us_gaps[c]
    eu_l, eu_n_, eu_g = eu_gaps[c]
    _rows.append([c,
                  round(kr_l, 2), round(kr_n_, 2), round(kr_g, 2),
                  round(us_l, 2), round(us_n_, 2), round(us_g, 2),
                  round(eu_l, 2), round(eu_n_, 2), round(eu_g, 2)])

add_data_sheet('fig09_3국격차',
               ['속성',
                f'KR22 입법 % (N={kr22_total_c})', '국내뉴스 %', '격차 KR %p',
                f'US119 입법 % (N={us119_total})', 'NYT 뉴스 %', '격차 US %p',
                f'EU Act % (N={eu_act_total})', '가디언 뉴스 %', '격차 EU %p'],
               _rows,
               note='[그림 9] 한·미·EU 입법-공론 격차 비교 (KR22-국내뉴스, US119-NYT, EU Act-Guardian)')
print('  fig06 done')


# ════════════════════════════════════════════════════════════════════════
# 국내 6개 매체 뉴스 그림 (report41a/b/c)
#   데이터: news_descriptive 로더 → news_analysis.duckdb cleaned subset
#   (76,645건 전량 분류, none 제외 share). 그림 PNG는 보고서 §4.1이 참조.
# ════════════════════════════════════════════════════════════════════════
print('\nnews figures (report41a/b/c)...', flush=True)

from datetime import datetime as _dt

NEWS_PROVS = nd.PROVIDER_ORDER  # ['KBS','MBC','SBS','YTN','중앙일보','한겨레']
PROV_COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']
_NEWS_MARKERS = [
    (_dt(2022, 11, 30), 'ChatGPT 출시\n(2022.11)'),
    (_dt(2024, 12, 26), 'AI 기본법 통과\n(2024.12)'),
    (_dt(2026, 1, 22), 'AI 기본법 시행\n(2026.01)'),
]

def _ym_add(ym, months):
    t = int(ym[:4]) * 12 + (int(ym[5:7]) - 1) + months
    return f'{t // 12:04d}-{t % 12 + 1:02d}'

_monthly = nd.load_monthly_counts()
_all_ym = sorted({ym for v in _monthly.values() for ym, _ in v})
_dates = [_dt.strptime(ym, '%Y-%m') for ym in _all_ym]
_N_TOTAL = nd.load_classification_coverage()['total_articles']

# ── report41a: 10년 보도 추이 (매체 stacked area + 12M 이동평균 + 마커) ──
print('  report41a decade trend...', flush=True)
_areas = np.array([[dict(_monthly[p]).get(ym, 0) for ym in _all_ym] for p in NEWS_PROVS])
_total_y = _areas.sum(axis=0).astype(float)

fig, ax = plt.subplots(figsize=(13, 6))
ax.stackplot(_dates, _areas, labels=NEWS_PROVS, colors=PROV_COLORS,
             alpha=0.78, edgecolor='white', linewidth=0.3)
_win = 12
if len(_total_y) >= _win:
    _ma = np.convolve(_total_y, np.ones(_win) / _win, mode='valid')
    ax.plot(_dates[_win - 1:], _ma, color='black', linewidth=1.8,
            label='12개월 이동평균', alpha=0.85)
_ymax = (max(_total_y) * 1.18) if len(_total_y) else 1
for d, lbl in _NEWS_MARKERS:
    ax.axvline(d, color='red', linestyle='--', linewidth=1.0, alpha=0.65)
    ax.text(d, _ymax * 0.97, '  ' + lbl, fontproperties=fp, fontsize=9,
            color='red', alpha=0.92, rotation=90, ha='left', va='top')
ax.set_xlabel('연·월', fontproperties=fp, fontsize=12)
ax.set_ylabel('월별 AI 보도량', fontproperties=fp, fontsize=12)
ax.set_title(f'국내 6개 언론사 AI 보도 추이 ({_all_ym[0]}~{_all_ym[-1]}, n={_N_TOTAL:,})',
             fontproperties=fp, fontsize=14, fontweight='bold')
ax.legend(prop=fp, loc='upper left', ncol=4, fontsize=10)
ax.set_ylim(0, _ymax)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'report41a_decade_trend.png'), dpi=200, bbox_inches='tight')
plt.close()

_rows = [[ym, int(_total_y[j])] + [int(_areas[i, j]) for i in range(len(NEWS_PROVS))]
         for j, ym in enumerate(_all_ym)]
add_data_sheet('report41a_10년추이', ['연월', '월합계'] + NEWS_PROVS, _rows,
               note=f'[그림 5] 국내 6개 매체 월별 AI 보도 (n={_N_TOTAL:,}, {_all_ym[0]}~{_all_ym[-1]})')

# ── report41b: AI 기본법 통과 ±24개월 (좌 zoom 라인 + 우 전/후 월평균) ──
print('  report41b AI-law window...', flush=True)
_winw = nd.load_event_window(nd.AIBASIC_DATE, 24)
_LAW = _dt.strptime(nd.AIBASIC_DATE, '%Y-%m-%d')
_ev_ym = nd.AIBASIC_DATE[:7]
_lo_ym, _hi_ym = _ym_add(_ev_ym, -24), _ym_add(_ev_ym, 24)
_w_ym = [ym for ym in _all_ym if _lo_ym <= ym < _hi_ym]
_w_dates = [_dt.strptime(ym, '%Y-%m') for ym in _w_ym]
_w_series = {p: [dict(_monthly[p]).get(ym, 0) for ym in _w_ym] for p in NEWS_PROVS}

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6),
                               gridspec_kw={'width_ratios': [2.0, 1.0]})
for i, p in enumerate(NEWS_PROVS):
    ax1.plot(_w_dates, _w_series[p], color=PROV_COLORS[i], label=p,
             linewidth=1.6, alpha=0.92)
_w_total = [sum(_w_series[p][j] for p in NEWS_PROVS) for j in range(len(_w_dates))]
ax1.fill_between(_w_dates, _w_total, alpha=0.08, color='gray', label='월별 합계')
ax1.axvline(_LAW, color='red', linestyle='--', linewidth=1.4, alpha=0.85)
_y1 = (max(_w_total) * 1.05) if _w_total else 1
ax1.text(_LAW, _y1, '  AI 기본법 통과\n  (2024.12.26)', fontproperties=fp,
         fontsize=10, color='red', va='top', ha='left')
ax1.axvline(_dt(2026, 1, 22), color='green', linestyle=':', linewidth=1.2, alpha=0.7)
ax1.text(_dt(2026, 1, 22), _y1 * 0.85, '  시행\n  (2026.01)', fontproperties=fp,
         fontsize=9, color='green', va='top', ha='left')
ax1.set_xlabel('연·월', fontproperties=fp, fontsize=12)
ax1.set_ylabel('월별 AI 보도량 (매체별)', fontproperties=fp, fontsize=12)
ax1.set_title('AI 기본법 통과 전후 ±24개월 보도 추이', fontproperties=fp,
              fontsize=13, fontweight='bold')
ax1.legend(prop=fp, loc='upper left', ncol=4, fontsize=9)
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)
ax1.set_ylim(0, _y1 * 1.05)

_x = np.arange(len(NEWS_PROVS))
_wb = 0.38
_pre = [_winw['providers'][p]['before_avg'] for p in NEWS_PROVS]
_post = [_winw['providers'][p]['after_avg'] for p in NEWS_PROVS]
ax2.bar(_x - _wb / 2, _pre, _wb, label='통과 전 24개월', color='#7F7F7F', edgecolor='white')
ax2.bar(_x + _wb / 2, _post, _wb, label='통과 후 24개월', color='#1565C0', edgecolor='white')
for i, p in enumerate(NEWS_PROVS):
    _chg = _winw['providers'][p]['pct']
    ax2.text(i, max(_pre[i], _post[i]) + max(_pre + _post) * 0.03, f'{_chg:+.0f}%',
             ha='center', fontsize=9, fontproperties=fp,
             color='darkgreen' if _chg > 0 else 'darkred', fontweight='bold')
ax2.set_xticks(_x)
ax2.set_xticklabels(NEWS_PROVS, fontproperties=fp, fontsize=10, rotation=20)
ax2.set_ylabel('월평균 보도량', fontproperties=fp, fontsize=12)
_sub = ' (부분 윈도우)' if _winw['partial'] else ''
ax2.set_title('통과 전후 24개월 월평균 변화' + _sub, fontproperties=fp,
              fontsize=13, fontweight='bold')
ax2.legend(prop=fp, fontsize=10, loc='upper right')
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)
ax2.set_ylim(0, max(_pre + _post) * 1.22)
plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'report41b_aibasic_window.png'), dpi=200, bbox_inches='tight')
plt.close()

_rows = [[ym] + [_w_series[p][j] for p in NEWS_PROVS] for j, ym in enumerate(_w_ym)]
add_data_sheet('report41b_window월별', ['연월'] + NEWS_PROVS, _rows,
               note='[그림 6] AI 기본법 ±24개월 월별 매체 보도량')
_ov = _winw['overall']
_rows2 = [[p, round(_winw['providers'][p]['before_avg'], 1),
           round(_winw['providers'][p]['after_avg'], 1),
           f"{_winw['providers'][p]['pct']:+.1f}%"] for p in NEWS_PROVS]
_rows2.append(['전체', round(_ov['before_avg'], 1), round(_ov['after_avg'], 1),
               f"{_ov['pct']:+.1f}%"])
add_data_sheet('report41b_전후평균', ['매체', '통과 전 월평균', '통과 후 월평균', '변화율'],
               _rows2,
               note=f"[그림 6] AI 기본법 통과 전·후 24개월 월평균{' (부분 윈도우)' if _winw['partial'] else ''}")

# ── report41c: 매체별 + 연도별 정책 속성 구성 (듀얼패널, none 제외 share) ──
print('  report41c attributes...', flush=True)
_p_provs, _p_attrs, _p_mat = nd.load_attr_by_provider()
_y_years, _y_attrs, _y_share, _y_counts = nd.load_attr_by_year()
_pmat = np.array(_p_mat)
_attrs_no_none = [a for a in _p_attrs if a != 'none']
_cmap = plt.get_cmap('tab10')

fig, (axL, axR) = plt.subplots(1, 2, figsize=(16, 6.5),
                               gridspec_kw={'width_ratios': [1.0, 1.25]})
_left = np.zeros(len(_p_provs))
for k, a in enumerate(_attrs_no_none):
    ai = _p_attrs.index(a)
    vals = np.array([
        (_pmat[pi][ai] / max(1, sum(_pmat[pi][aj] for aj, aa in enumerate(_p_attrs) if aa != 'none')) * 100)
        for pi in range(len(_p_provs))])
    axL.barh(range(len(_p_provs)), vals, left=_left, color=_cmap(k % 10),
             label=kr_label(a), edgecolor='white', height=0.7)
    _left += vals
axL.set_yticks(range(len(_p_provs)))
axL.set_yticklabels(_p_provs, fontproperties=fp, fontsize=11)
axL.invert_yaxis()
axL.set_xlim(0, 100)
axL.set_xlabel('속성 비중 (%, 미분류 제외)', fontproperties=fp, fontsize=12)
axL.set_title('매체별 정책 속성 구성', fontproperties=fp, fontsize=13, fontweight='bold')
axL.legend(prop=fp, ncol=2, fontsize=8, loc='lower right')

_ymat = np.array(_y_share)
for k, a in enumerate(_y_attrs):
    axR.plot(_y_years, _ymat[:, k], marker='o', ms=3, color=_cmap(k % 10), label=kr_label(a))
axR.set_xlabel('연도', fontproperties=fp, fontsize=12)
axR.set_ylabel('속성 비중 (%, 미분류 제외)', fontproperties=fp, fontsize=12)
axR.set_title('연도별 정책 속성 추세', fontproperties=fp, fontsize=13, fontweight='bold')
axR.set_xticks(_y_years)
axR.legend(prop=fp, ncol=2, fontsize=8)
axR.spines['top'].set_visible(False)
axR.spines['right'].set_visible(False)
plt.tight_layout()
plt.savefig(os.path.join(FIG_DIR, 'report41c_kr_attributes.png'), dpi=200, bbox_inches='tight')
plt.close()

_rows = []
for pi, prov in enumerate(_p_provs):
    denom = sum(_pmat[pi][aj] for aj, aa in enumerate(_p_attrs) if aa != 'none')
    for ai, a in enumerate(_p_attrs):
        share = (_pmat[pi][ai] / denom * 100) if (denom and a != 'none') else ''
        _rows.append([prov, kr_label(a), int(_pmat[pi][ai]),
                      round(share, 2) if share != '' else ''])
add_data_sheet('report41c_매체별속성', ['매체', '속성', '건수', '비중%(none제외)'], _rows,
               note='[그림 7] 매체별 정책 속성 구성 (none 제외 share)')
_rows = []
for yi, y in enumerate(_y_years):
    for k, a in enumerate(_y_attrs):
        _rows.append([y, kr_label(a), int(_y_counts[yi][k]), round(_y_share[yi][k], 2)])
add_data_sheet('report41c_연도별속성', ['연도', '속성', '건수', '비중%(none제외)'], _rows,
               note='[그림 7] 연도별 정책 속성 추세 (none 제외 share)')
print('  report41c done')

# ════════════════════════════════════════
# 모든 그림 데이터를 단일 엑셀 워크북으로 저장
# ════════════════════════════════════════
_data_wb.save(DATA_XLSX)
print(f'\n그림 데이터 엑셀 저장: {DATA_XLSX}')

print('\nAll figures regenerated.')
