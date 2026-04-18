"""Generate EU AI Act policy attribute report markdown.

Loads classification via bill_loaders (project-root shared module).
No adapter file; reads data/bills_classified_eu_{act,amendments}.json directly.
"""
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bill_loaders import load_eu_bills

sys.stdout.reconfigure(encoding='utf-8')

bills = load_eu_bills()
# dict keyed by type+id for backward compat
data = {}
for b in bills:
    k = f"{b['type']}_{b['id']}"
    data[k] = b

ATTRS = [
    'Safety', 'Responsible and ethical AI', 'National security',
    'Industrial policy', 'Public interest', 'Elections',
    'Market efficiency and power concentration (antitrust)',
    'Labor', 'Copyright', 'International collaboration',
]

by_attr_type = defaultdict(lambda: defaultdict(list))
for key, v in data.items():
    attr = v.get('primary', 'Other')
    dtype = v.get('type', 'unknown')
    by_attr_type[attr][dtype].append(v)

lines = []
lines.append('# EU AI Act: Policy Attribute Classification')
lines.append('')
lines.append('> **Model**: GPT-4.1-mini')
lines.append('> **Framework**: Carvao et al. (2025) 10 Policy Attributes')
lines.append('> **Scope**: AI Act articles (112) + European Parliament amendments (770) = 882 provisions')
lines.append('> **Generated**: 2026-04-14')
lines.append('')

# Summary table
lines.append('## Distribution Summary')
lines.append('')
lines.append('| Policy Attribute | Articles | Amendments | Total | Share |')
lines.append('|------------------|------:|------:|------:|------:|')

total_all = len(data)
for attr in ATTRS:
    ca = len(by_attr_type[attr]['article'])
    cm = len(by_attr_type[attr]['amendment'])
    total = ca + cm
    if total == 0:
        continue
    share = total / total_all * 100
    lines.append(f'| {attr} | {ca} | {cm} | {total} | {share:.1f}% |')

ta = sum(len(by_attr_type[a]['article']) for a in ATTRS)
tm = sum(len(by_attr_type[a]['amendment']) for a in ATTRS)
lines.append(f'| **Total** | **{ta}** | **{tm}** | **{ta+tm}** | **100%** |')
lines.append('')
lines.append('---')
lines.append('')

# Trend analysis
lines.append('## Key Findings')
lines.append('')

trends = {
    'Safety': 'Dominant category in both articles (62/112, 55%) and amendments (319/770, 41%). The EU AI Act is fundamentally a safety regulation — risk classification, conformity assessment, post-market monitoring, and high-risk AI system requirements.',
    'Responsible and ethical AI': 'Second largest overall. Articles focus on transparency, human oversight, and governance structures. Amendments (328) are the single largest amendment category, reflecting intense parliamentary debate on accountability, bias prevention, and fundamental rights.',
    'National security': 'Minimal in articles (0) but significant in amendments (33). Parliament pushed to clarify military/law enforcement AI exemptions and safeguards — a contentious area the Commission initially sidestepped.',
    'Industrial policy': 'Articles (3) and amendments (25) address regulatory sandboxes, SME support, and innovation-friendly measures. Relatively small share reflects the Act is primarily a regulatory framework, not an industrial strategy.',
    'Market efficiency and power concentration (antitrust)': 'Articles (1) and amendments (20) address market surveillance, competition among notified bodies, and preventing regulatory capture by large AI providers.',
    'Public interest': 'Articles (1) and amendments (19) cover healthcare AI, education, environmental impact, and consumer protection applications.',
    'Labor': 'Amendments (9) address AI in workplace monitoring, hiring, and worker rights — a growing concern that received relatively modest attention.',
    'International collaboration': 'Amendments (9) on mutual recognition, third-country cooperation, and global AI governance standards alignment.',
    'Elections': 'Amendments (5) specifically targeting deepfake political content and AI-generated disinformation in democratic processes.',
    'Copyright': 'Amendments (3) on training data transparency and IP rights — relatively minor given the EU Copyright Directive handles this separately.',
}

for attr in ATTRS:
    ca = len(by_attr_type[attr]['article'])
    cm = len(by_attr_type[attr]['amendment'])
    total = ca + cm
    if total == 0:
        continue
    trend = trends.get(attr, '')
    lines.append(f'### {attr} ({total} provisions)')
    lines.append('')
    lines.append(f'{trend}')
    lines.append('')

lines.append('---')
lines.append('')

# Articles list
lines.append('## AI Act Articles by Policy Attribute')
lines.append('')

for attr in ATTRS:
    articles = sorted(by_attr_type[attr]['article'], key=lambda x: x.get('article_num', 0))
    if not articles:
        continue
    lines.append(f'### {attr} ({len(articles)} articles)')
    lines.append('')
    lines.append('| # | Article | Title | Summary |')
    lines.append('|--:|---------|-------|---------|')
    for i, a in enumerate(articles, 1):
        title = a.get('title', '').replace('|', '/')
        summary = a.get('summary', '').replace('|', '/')
        lines.append(f'| {i} | Art. {a.get("article_num","")} | {title} | {summary} |')
    lines.append('')

lines.append('---')
lines.append('')

# Amendments summary (top per attribute, not all 770)
lines.append('## Amendment Distribution by Policy Attribute')
lines.append('')
lines.append('770 amendments are too numerous to list individually. Below is the distribution and representative examples.')
lines.append('')

for attr in ATTRS:
    amendments = sorted(by_attr_type[attr]['amendment'], key=lambda x: x.get('amendment_num', 0))
    if not amendments:
        continue
    lines.append(f'### {attr} ({len(amendments)} amendments)')
    lines.append('')
    # Show first 5 as examples
    lines.append('| # | Amendment | Target | Summary |')
    lines.append('|--:|-----------|--------|---------|')
    for i, a in enumerate(amendments[:10], 1):
        target = a.get('target', '').replace('|', '/')
        summary = a.get('summary', '').replace('|', '/')
        lines.append(f'| {i} | Am. {a.get("amendment_num","")} | {target} | {summary} |')
    if len(amendments) > 10:
        lines.append(f'| ... | *{len(amendments)-10} more* | | |')
    lines.append('')

lines.append('---')
lines.append('')

out = 'replicate_carvao/data/eu_policy_attr_report.md'
with open(out, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f'Saved: {out}')
print(f'Articles: {ta}, Amendments: {tm}, Total: {ta+tm}')
