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
| [classify_articles.py](classify_articles.py) | News classifier. Usage: `python classify_articles.py {guardian|nyt|naver|all}`. |
| [classify_bills.py](classify_bills.py) | Bill classifier. Usage: `python classify_bills.py {kr_19|kr_20|kr_21|kr_22|us_118|us_119|eu_act|eu_amendments|all}`. |
| [bill_loaders.py](bill_loaders.py) | Loader helpers `load_kr_bills()`, `load_us_bills()`, `load_eu_bills()` that join classification with source metadata. All downstream consumers (figures, reports, validators) go through this module — do not re-load `bills_classified_*.json` directly. |
| [download_bills.py](download_bills.py) | KR bill PDF downloader + fitz text extraction → `bill_text` table. Usage: `python download_bills.py --age 22`. |
| [download_documents.py](download_documents.py) | Unified PDF/HWP/HWPX downloader + extractor → `document_text` table. Covers 연구단체 보고서 (ncrwiahparxrpodcv) and 5 회의록 API tables. Usage: `python download_documents.py --source SRC` where SRC is one of `report`, `minutes_plenary`, `minutes_committee`, `minutes_committee_of_whole`, `minutes_subcommittee`. See §Document extraction pipeline below. |

Both classifiers load `prompts.SYSTEM_PROMPT`, retry 429s with exponential backoff, and cache successful results in their output JSON (errors get retried on re-run).

**Classification + bill text live in DuckDB (since 2026-04-18).** Phase 3~5 of [PLAN_db_consolidation.md](PLAN_db_consolidation.md) moved them out of the filesystem:

- KR/US/EU bill classification → `bill_classifications` table (versioned by `prompt_versions.version`)
- KR Stage-2 filter cache → `bill_ai_filter` table
- KR bill text (extracted from PDF) → `bill_text` table
- KR analysis convenience view: `v_kr_bills_analysis`

`bill_loaders.py` is now a thin DB wrapper — its public signatures (`load_kr_bills`, `load_us_bills`, `load_eu_bills`) are preserved so all downstream consumers (figures, reports, validators) work unchanged. US/EU sponsor metadata still comes from on-disk JSON (`bills_processed.json`, `eu_ai_act_articles.json`, `eu_amendments.json`); only classification rows moved to DB. Original JSONs are archived under `data/_archive/`.

## Document extraction pipeline (since 2026-04-19)

[PLAN_doc_extraction.md](PLAN_doc_extraction.md) restored the lost PDF/HWP downloader → text-extractor pipeline for 6 API tables that carry attachment URLs. All output lives in `document_text` (PK `(doc_id, source)`) with raw files under `data/docs/{source}/` (gitignored, ~25 GB). See [CODEBOOK.md §13](CODEBOOK.md) for the full coverage matrix.

**Canonical scripts**:

- [migrate_legacy_docs.py](migrate_legacy_docs.py) — one-shot seed from `data/txt/*/{research,report,conf}/*.json` + `data/minutes_txt/*/{본회의,소위원회}/*.json`. Idempotent; ON CONFLICT keeps longer text.
- [download_documents.py](download_documents.py) — parallel downloader + extractor. Per-source registry (`SOURCES` dict). Single shared DuckDB write connection + lock (DuckDB forbids concurrent writers). Resume on `status IN ('extracted_ok','format_unsupported','url_404','no_url')`; automatic retry on `url_error`/`extract_failed`.

**Format handling**:

- Format detected by magic bytes, not URL extension (`%PDF-` / OLE2 / `PK\x03\x04` + HWPX mimetype check).
- PDF → fitz (PyMuPDF, `extractor_version='fitz-1.0'`).
- HWP → `venv/Scripts/hwp5txt.exe` subprocess (`pyhwp 0.1b15`, `extractor_version='hwp5txt-0.1'`). Only needed if any source returns HWP; 2026-04-19 initial run returned 100% PDF.
- HWPX → zipfile + ElementTree over `Contents/section*.xml`.

**Source coverage (2026-04-19 initial run)**:

| source | API table | rows | status |
|--------|-----------|------|--------|
| `research` | `nfvmtaqoaldzhobsw` (소규모 연구용역) | 295 | **seed only — URL cannot be derived** (no direct URL column, `FILE_ID` → portal B0000108 pages do not expose attachments via any scrape-friendly endpoint). Keep as-is; don't retry. |
| `report` | `ncrwiahparxrpodcv` (연구단체 연구활동) | 1,948 | complete |
| `minutes_plenary` | `nzbyfwhwaoanttzje` | 1,832 (-3 url_error) | complete |
| `minutes_committee_of_whole` | `ngytonzwavydlbbha` | 8 | complete |
| `minutes_subcommittee` | `vconfsubcconflist` | 189 | complete |
| `minutes_committee` | `ncwgseseafwbuheph` | 22,275 (-8 url_error) | complete |

Total: 26,547 rows, 26,536 `extracted_ok`. All 11 `url_error` entries are persistent 119-byte placeholder responses from `record.assembly.go.kr` for IDs that no longer exist — do not retry.

**Things to remember when touching this pipeline**:

- DuckDB in the same process cannot open a read-only connection while a write connection is open. `download_documents.py` and `classify_bills.py` both use the `_reader_con()` helper (returns the shared write connection if open) — preserve this when editing.
- `prompt_versions` / `bill_classifications` pattern does NOT apply to `document_text` (extraction is deterministic; `extractor_version` is logged but not PK-versioned). Bumping `fitz` or `hwp5txt` does not require re-extraction unless the new version fixes a known bug.
- `nfvmtaqoaldzhobsw` URL derivation was investigated in Phase 0 and is not feasible via the public Assembly portal. Do not re-litigate without a new lead (e.g., NARS internal mapping).

## Korean AI bill selection — 2-stage filter (critical)

Do not replace this with a simple keyword count. Naive counting produces inflated lists (e.g., ~331 vs the correct ~200 for the 22nd Assembly) because bills like "조류인플루엔자(AI)" and background mentions ("AI 시대에...") get swept in.

**Stage 1** — keyword filter: text contains `인공지능|AI|A\.I` ≥3 times, then dedup by `(bill_name, lead_proposer)` keeping latest.

**Stage 2** — `gpt-4.1-mini` classifies each candidate as `core` / `adjacent` / `unrelated` using the prompt in `classify_bills.py::AI_FILTER_PROMPT`. Key test: *"if you delete AI content from this bill, does the bill still have reason to exist?"* Only `core + adjacent` survive.

Both stages are baked into `classify_bills.load_kr_bills()`. Stage 2 results cache to the `bill_ai_filter` table; `classify_bills.py ... --force` drops cached rows for the targeted source(s) at the current `PROMPT_VERSION` and re-classifies. Bump `PROMPT_VERSION` in `classify_bills.py` when changing `AI_FILTER_PROMPT` or `SYSTEM_PROMPT` so old and new results coexist for comparison.

US bills (Brennan Center pre-filtered) and EU AI Act (inherently AI-specific) skip both filter stages.

## News title filter

All three news sources (`guardian/nyt/naver`) apply the same title filter before 10-attribute classification: title must contain `\bAI\b | artificial intelligence | A\.I\. | 인공지능 | 인공 지능`. This is enforced in `classify_articles.py::title_has_kw` so cross-source comparison stays apples-to-apples.

Naver collection itself is multi-stage (see WORKFLOW §2.4); the canonical input JSON is `data/naver_articles_title_filtered.json` (~449 articles, 8 publishers). Do not re-collect Naver from scratch without reading that section.

## Assembly DuckDB access

`data/assembly.duckdb` is queried via the MCP server at [duckdb_mcp_server.py](duckdb_mcp_server.py). Configured in `.mcp.json`; Claude Code sees tools `mcp__assembly-db__{list_tables,describe_table,query}` (SELECT only). Tables use cryptic API codes (e.g. `nwvrqwxyaytdsfvhu` for member info) — [CODEBOOK.md](CODEBOOK.md) maps all 37 tables to human-readable descriptions. Prefer the `v_*` views (e.g. `v_bill`, `v_member`, `v_ai_profile`, `v_kr_bills_analysis`) for analysis.

Every per-age table now has a standardized `age INTEGER` column; the per-API derivation rule lives in `config.py::ApiSpec.age_source` and is enforced post-collection by [validate_collection.py](validate_collection.py) (called automatically at the end of `download_all.py`). When making schema-touching changes to the collector, run the validator before committing.

## Common commands

```bash
# Assembly API collection
python download_all.py                        # full collect (13~22대, respects stale cache)
python download_all.py --status               # progress check
python download_all.py --api BILLRCP          # one API

# AI pipeline — news
python classify_articles.py all                        # 3 sources
python export_titles.py all                   # group titles by attribute

# AI pipeline — bills
python classify_bills.py kr_22                # single KR Congress (Stage 1+2 auto)
python classify_bills.py all                  # KR 19-22 + US 118-119 + EU act + amendments
python export_bills.py all                    # markdown for KR/US/EU (per subgroup + combined)

# Report
pdflatex report.tex                           # build LaTeX report
```

No test suite, linter, or CI — this is a research/analysis repo.

## Two frozen reference folders

- **`replicate_carvao/`** — strictly the US paper replication (Carvão et al. 2025). Contains `02_collect_bill_details.py` through `07_visualize.py`, `us119_run_all.py`, `gen_us_report.py`, and (for now) `gen_eu_report.py` + EU legacy data. Do not add Korean content here.
- **`kr_analysis/`** — frozen historical KR pipeline. Contains the original 3-step KR filter (`kr_01_prepare_data.py` → `kr_02_preprocess.py` → `kr_03_nlp_classify.py`) plus `validate_tfidf_lda.py` (TF-IDF/LDA vs GPT cross-validation). The 2-stage filter in the active `classify_bills.py` is ported from `kr_analysis/kr_01_prepare_data.py` — keep that file as the historical reference when revisiting filter logic.

Both folders are intended as frozen references. Active code lives at the repo root.

## Output-file hygiene

- News classification uses JSON files at `data/processed/articles_classified_{guardian,nyt,naver}.json`; bill classification uses DB rows. Both retry errors on re-run. `classify_articles.py` and `classify_bills.py` both support `--force` to purge caches/outputs and reclassify.
- News classification output filenames are stable — do not introduce v2/v3 suffixed siblings. Bill classification versioning is via `prompt_versions.version` instead.
- Intermediate Naver files (`naver_articles_v3_raw.json`, `*_clean.json`, `*_filtered.json`, `*_final.json`) are byproducts of the multi-stage pipeline; the analysis-ready one is `naver_articles_title_filtered.json`.
- `data/_archive/` holds pre-migration JSONs (`bill_txt_*/`, `bills_classified_*.json`, `kr_*_ai_filtered.json`) for ~3 months. Do not read from there — use the DB.

## When reading Korean output

Classification labels are always the English Carvão attribute strings (e.g. `"Responsible and ethical AI"`, `"Market efficiency and power concentration (antitrust)"`) regardless of article language. Korean labels appear only in human-facing markdown exports and the report.
