"""Generate US policy attribute report markdown.

Loads classification via bill_loaders (project-root shared module).
No adapter file; reads data/bills_classified_us_{118,119}.json directly.
"""
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bill_loaders import load_us_bills

sys.stdout.reconfigure(encoding='utf-8')

bills = load_us_bills()
# dict keyed by bill_id for backward compat with existing loops
data = {b["id"]: b for b in bills}

ATTRS = [
    'Safety', 'Public interest', 'Responsible and ethical AI',
    'National security', 'Industrial policy', 'Elections',
    'Market efficiency and power concentration (antitrust)',
    'Labor', 'Copyright', 'International collaboration',
]

by_attr_congress = defaultdict(lambda: defaultdict(list))
for bid, v in data.items():
    attr = v.get('primary', 'Other')
    congress = v.get('congress', 0)
    by_attr_congress[attr][congress].append(v)

lines = []
lines.append('# 118th-119th Congress AI Bills: Policy Attribute Classification')
lines.append('')
lines.append('> **Model**: GPT-4.1')
lines.append('> **Framework**: Carvao et al. (2025) 10 Policy Attributes')
lines.append('> **Scope**: 118th 154 bills + 119th 53 bills = 207 bills')
lines.append('> **Generated**: 2026-04-14')
lines.append('')

# Summary table
lines.append('## Distribution Summary')
lines.append('')
lines.append('| Policy Attribute | 118th | 119th | Total | Change |')
lines.append('|------------------|------:|------:|------:|--------|')

trend_data = {}
for attr in ATTRS:
    c118 = len(by_attr_congress[attr][118])
    c119 = len(by_attr_congress[attr][119])
    total = c118 + c119
    if total == 0:
        continue
    if c118 > 0:
        ratio_ann = (c119 * 2) / c118
        if ratio_ann > 2:
            change = f'Increasing ({ratio_ann:.1f}x pace)'
        elif ratio_ann > 1.2:
            change = 'Increasing'
        elif ratio_ann < 0.5:
            change = 'Declining'
        elif ratio_ann < 0.8:
            change = 'Slightly declining'
        else:
            change = 'Stable'
    else:
        change = 'New' if c119 > 0 else '-'
    lines.append(f'| {attr} | {c118} | {c119} | {total} | {change} |')
    trend_data[attr] = (c118, c119, change)

t118 = sum(len(by_attr_congress[a][118]) for a in ATTRS)
t119 = sum(len(by_attr_congress[a][119]) for a in ATTRS)
lines.append(f'| **Total** | **{t118}** | **{t119}** | **{t118+t119}** | |')
lines.append('')
lines.append('---')
lines.append('')

# Trend analysis
lines.append('## Trend Analysis')
lines.append('')
lines.append('| Policy Attribute | 118th to 119th | Key Trends |')
lines.append('|------------------|----------------|------------|')

trends = {
    'Safety': 'Consistently the largest category. Deepfake protection, AI incident reporting, child safety, biosecurity. 119th maintains this focus with consumer safety and health applications.',
    'Public interest': 'Broad coverage: privacy, children, healthcare, education, government transparency. 119th adds wildfire response, maternal health, and peacebuilding.',
    'Responsible and ethical AI': 'Strong in 118th (21 bills) with algorithmic accountability, bias, transparency. 119th sees a sharp decline, possibly as comprehensive frameworks are absorbed into other categories.',
    'National security': '118th: 14 bills on defense AI, biosecurity, foreign adversaries. 119th: 12 bills - accelerating pace with China decoupling, fentanyl detection, border tech.',
    'Industrial policy': '118th: 17 bills on R&D, NSF, DOE, small business AI. 119th: stable pace with supply chain resilience and standards leadership.',
    'Elections': '118th: 13 bills on deepfake political ads, election integrity. 119th: sharp decline to 1 bill.',
    'Market efficiency and power concentration (antitrust)': '118th: 5 bills on algorithmic pricing, platform regulation. 119th: 7 bills - increasing pace, adding AI procurement competition and regulatory review.',
    'Labor': '118th: 6 bills on workforce training, workplace surveillance. 119th: 1 bill - significant decline.',
    'Copyright': '118th: 3 bills (NO FAKES, generative AI disclosure). 119th: 1 bill (NO FAKES reintroduced).',
    'International collaboration': '118th: 4 bills on alliances, research partnerships. 119th: 0 - pivot to national security focus.',
}

for attr in ATTRS:
    if attr not in trend_data:
        continue
    c118, c119, change = trend_data[attr]
    trend_text = trends.get(attr, '')
    lines.append(f'| {attr} | {c118} to {c119} ({change}) | {trend_text} |')

lines.append('')
lines.append('---')
lines.append('')

# Per-attribute bill lists
for attr in ATTRS:
    bills_118 = sorted(by_attr_congress[attr][118], key=lambda x: x.get('introduced_date', ''))
    bills_119 = sorted(by_attr_congress[attr][119], key=lambda x: x.get('introduced_date', ''))
    total = len(bills_118) + len(bills_119)
    if total == 0:
        continue

    lines.append(f'## {attr} ({total} bills)')
    lines.append('')

    if bills_118:
        lines.append(f'### 118th Congress ({len(bills_118)} bills)')
        lines.append('')
        lines.append('| # | Date | Party | Bill | Summary |')
        lines.append('|--:|------|-------|------|---------|')
        for i, b in enumerate(bills_118, 1):
            party = b.get('sponsor_party', '')
            name = b.get('title', '').replace('|', '/')
            summary = b.get('summary', '').replace('|', '/')
            lines.append(f'| {i} | {b.get("introduced_date","")} | {party} | {name} | {summary} |')
        lines.append('')

    if bills_119:
        lines.append(f'### 119th Congress ({len(bills_119)} bills)')
        lines.append('')
        lines.append('| # | Date | Party | Bill | Summary |')
        lines.append('|--:|------|-------|------|---------|')
        for i, b in enumerate(bills_119, 1):
            party = b.get('sponsor_party', '')
            name = b.get('title', '').replace('|', '/')
            summary = b.get('summary', '').replace('|', '/')
            lines.append(f'| {i} | {b.get("introduced_date","")} | {party} | {name} | {summary} |')
        lines.append('')

    lines.append('---')
    lines.append('')

out = 'replicate_carvao/data/us_policy_attr_report.md'
with open(out, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f'Saved: {out}')
for congress in [118, 119]:
    cbills = {k: v for k, v in data.items() if v.get('congress') == congress}
    dist = Counter(v.get('primary', '?') for v in cbills.values())
    print(f'\n[{congress}th] {len(cbills)} bills')
    for attr, cnt in dist.most_common():
        print(f'  {attr}: {cnt}')
