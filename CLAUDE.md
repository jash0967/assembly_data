# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Two distinct but related workstreams share this repo:

1. **Assembly Open API collector** — `download_all.py` + `collector.py` + `api_client.py` harvest the Korean National Assembly Open API (37 endpoints, ~4.5M records) into `data/assembly.duckdb`. This is the base data layer.

2. **AI policy comparative analysis** — classifies AI-related legislation and news from Korea, US, and EU into 10 policy attributes from Carvão et al. (2025) "Governance at a Crossroads" (Harvard Kennedy School). The comparison feeds `report_expanded_draft.md`.

[WORKFLOW.md](WORKFLOW.md) is the canonical living document for the analysis pipeline — update it whenever the pipeline changes. Skim it before making changes to collection/classification code.

## Canonical pipeline scripts (do not proliferate)

| File | Role |
|------|------|
| [prompts.py](prompts.py) | Single source of truth for the 10-attribute `SYSTEM_PROMPT`. Used by both classifiers. |
| [classify.py](classify.py) | News classifier. Usage: `python classify.py {guardian|nyt|naver|all}`. |
| [classify_bills.py](classify_bills.py) | Bill classifier. Usage: `python classify_bills.py {kr_19|kr_20|kr_21|kr_22|us_118|us_119|eu_act|eu_amendments|all}`. |
| [bill_loaders.py](bill_loaders.py) | Loader helpers `load_kr_bills()`, `load_us_bills()`, `load_eu_bills()` that join classification with source metadata. All downstream consumers (figures, reports, validators) go through this module — do not re-load `bills_classified_*.json` directly. |

Both classifiers load `prompts.SYSTEM_PROMPT`, retry 429s with exponential backoff, and cache successful results in their output JSON (errors get retried on re-run).

**No adapter/intermediate JSONs.** Classification outputs (`bills_classified_*.json`) are the single source of truth; consumers use `bill_loaders.py` to pull in metadata (age/congress/type, proposer, date, etc.) directly from primary sources (`bill_txt_{age}/`, `bills_processed.json`, `eu_ai_act_articles.json`).

## Korean AI bill selection — 2-stage filter (critical)

Do not replace this with a simple keyword count. Naive counting produces inflated lists (e.g., ~331 vs the correct ~200 for the 22nd Assembly) because bills like "조류인플루엔자(AI)" and background mentions ("AI 시대에...") get swept in.

**Stage 1** — keyword filter: text contains `인공지능|AI|A\.I` ≥3 times, then dedup by `(bill_name, lead_proposer)` keeping latest.

**Stage 2** — `gpt-4.1-mini` classifies each candidate as `core` / `adjacent` / `unrelated` using the prompt in `classify_bills.py::AI_FILTER_PROMPT`. Key test: *"if you delete AI content from this bill, does the bill still have reason to exist?"* Only `core + adjacent` survive.

Both stages are baked into `classify_bills.load_kr_bills()`. Stage 2 results cache to `data/kr_{age}_ai_filtered.json` — deleting that file forces re-running the GPT filter.

US bills (Brennan Center pre-filtered) and EU AI Act (inherently AI-specific) skip both filter stages.

## News title filter

All three news sources (`guardian/nyt/naver`) apply the same title filter before 10-attribute classification: title must contain `\bAI\b | artificial intelligence | A\.I\. | 인공지능 | 인공 지능`. This is enforced in `classify.py::title_has_kw` so cross-source comparison stays apples-to-apples.

Naver collection itself is multi-stage (see WORKFLOW §2.4); the canonical input JSON is `data/naver_articles_title_filtered.json` (~449 articles, 8 publishers). Do not re-collect Naver from scratch without reading that section.

## Assembly DuckDB access

`data/assembly.duckdb` is queried via the MCP server at [duckdb_mcp_server.py](duckdb_mcp_server.py). Configured in `.mcp.json`; Claude Code sees tools `mcp__assembly-db__{list_tables,describe_table,query}` (SELECT only). Tables use cryptic API codes (e.g. `nwvrqwxyaytdsfvhu` for member info) — [CODEBOOK.md](CODEBOOK.md) maps all 37 tables to human-readable descriptions. Prefer the `v_*` views (e.g. `v_bill`, `v_member`, `v_ai_profile`) for analysis.

## Common commands

```bash
# Assembly API collection
python download_all.py                        # full collect (13~22대, respects stale cache)
python download_all.py --status               # progress check
python download_all.py --api BILLRCP          # one API

# AI pipeline — news
python classify.py all                        # 3 sources
python export_titles.py all                   # group titles by attribute

# AI pipeline — bills
python classify_bills.py kr_22                # single KR Congress (Stage 1+2 auto)
python classify_bills.py all                  # KR 19-22 + US 118-119 + EU act + amendments
python export_bills_kr.py                     # markdown per KR Assembly + combined

# Report
pdflatex report.tex                           # build LaTeX report
```

No test suite, linter, or CI — this is a research/analysis repo.

## Two frozen reference folders

- **`replicate_carvao/`** — strictly the US paper replication (Carvão et al. 2025). Contains `02_collect_bill_details.py` through `07_visualize.py`, `us119_run_all.py`, `gen_us_report.py`, and (for now) `gen_eu_report.py` + EU legacy data. Do not add Korean content here.
- **`kr_analysis/`** — frozen historical KR pipeline. Contains the original 3-step KR filter (`kr_01_prepare_data.py` → `kr_02_preprocess.py` → `kr_03_nlp_classify.py`) plus `validate_tfidf_lda.py` (TF-IDF/LDA vs GPT cross-validation). The 2-stage filter in the active `classify_bills.py` is ported from `kr_analysis/kr_01_prepare_data.py` — keep that file as the historical reference when revisiting filter logic.

Both folders are intended as frozen references. Active code lives at the repo root.

## Output-file hygiene

- Success results accumulate in output JSON; errors get retried on re-run (the classifier drops `primary:"error"` rows before processing).
- Classification output filenames are stable (`news_{source}_classified.json`, `bills_classified_{target}.json`) — do not introduce v2/v3 suffixed siblings. If the prompt changes, overwrite in place.
- Intermediate Naver files (`naver_articles_v3_raw.json`, `*_clean.json`, `*_filtered.json`, `*_final.json`) are byproducts of the multi-stage pipeline; the analysis-ready one is `naver_articles_title_filtered.json`.

## When reading Korean output

Classification labels are always the English Carvão attribute strings (e.g. `"Responsible and ethical AI"`, `"Market efficiency and power concentration (antitrust)"`) regardless of article language. Korean labels appear only in human-facing markdown exports and the report.
