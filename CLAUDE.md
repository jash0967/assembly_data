# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Two distinct but related workstreams share this repo:

1. **Assembly Open API collector** — `collect/download_all.py` + `collect/collector.py` + `collect/api_client.py` harvest the Korean National Assembly Open API (37 endpoints, ~4.5M records) into `data/bills_kr/assembly_raw.duckdb`. This is the base data layer. All collection·extraction scripts live under [collect/](collect/) (see §Repository layout).

2. **AI policy comparative analysis** — classifies AI-related legislation and news from Korea, US, and EU into 10 policy attributes from Carvão et al. (2025) "Governance at a Crossroads" (Harvard Kennedy School). The comparison feeds `report_expanded_draft.md`.

[WORKFLOW.md](WORKFLOW.md) is the canonical living document for the analysis pipeline — update it whenever the pipeline changes. Skim it before making changes to collection/classification code.

## Canonical pipeline scripts (do not proliferate)

| File | Role |
|------|------|
| [prompts.py](prompts.py) | Single source of truth for the 10-attribute `SYSTEM_PROMPT`. Used by both classifiers. |
| [analyze/classify_articles.py](analyze/classify_articles.py) | News classifier (영문 소스: Guardian, NYT). Usage: `python analyze/classify_articles.py guardian`, `nyt`, or `all`. 한국 도메스틱 뉴스는 `data/news/news.duckdb` (별도 파이프라인). |
| [analyze/classify_bills.py](analyze/classify_bills.py) | Bill classifier. Usage: `python analyze/classify_bills.py {kr_19|kr_20|kr_21|kr_22|us_118|us_119|eu_act|eu_amendments|all}`. |
| [bill_loaders.py](bill_loaders.py) | Loader helpers `load_kr_bills()`, `load_us_bills()`, `load_eu_bills()` that join classification with source metadata. All downstream consumers (figures, reports, validators) go through this module — do not re-load `bills_classified_*.json` directly. |
| [collect/download_bills.py](collect/download_bills.py) | KR bill PDF downloader + fitz text extraction → `bill_text` table. Usage: `python collect/download_bills.py --age 22`. |
| [collect/download_documents.py](collect/download_documents.py) | Unified PDF/HWP/HWPX downloader + extractor → `document_text` table. Covers 연구단체 보고서 (ncrwiahparxrpodcv) and 5 회의록 API tables. Usage: `python collect/download_documents.py --source SRC` where SRC is one of `report`, `minutes_plenary`, `minutes_committee`, `minutes_committee_of_whole`, `minutes_subcommittee`. See §Document extraction pipeline below. |

Both classifiers load `prompts.SYSTEM_PROMPT`, retry 429s with exponential backoff, and cache successful results in their output JSON (errors get retried on re-run).

**Classification + bill text live in DuckDB (since 2026-04-18).** Phase 3~5 of the (now-removed) `PLAN_db_consolidation.md` moved them out of the filesystem:

- KR/US/EU bill classification → `bill_classifications` table (versioned by `prompt_versions.version`)
- KR Stage-2 filter cache → `bill_ai_filter` table
- KR bill text (extracted from PDF) → `bill_text` table
- KR analysis convenience view: `v_kr_bills_analysis`

`bill_loaders.py` is now a thin DB wrapper — its public signatures (`load_kr_bills`, `load_us_bills`, `load_eu_bills`) are preserved so all downstream consumers (figures, reports, validators) work unchanged. US sponsor metadata still comes from on-disk JSON (`replicate_carvao/data/bills_processed.json`); EU metadata from `data/bills_eu/eu_ai_act_articles.json` + `eu_amendments.json`. Only classification rows moved to DB.

## Document extraction pipeline (since 2026-04-19)

The (now-removed) `PLAN_doc_extraction.md` restored the lost PDF/HWP downloader → text-extractor pipeline for 6 API tables that carry attachment URLs. All output lives in `document_text` (PK `(doc_id, source)`) with raw files under `data/bills_kr/docs/{source}/` (gitignored, ~25 GB). See [CODEBOOK.md §13](CODEBOOK.md) for the full coverage matrix.

**Canonical script**:

- [collect/download_documents.py](collect/download_documents.py) — parallel downloader + extractor. Per-source registry (`SOURCES` dict). Single shared DuckDB write connection + lock (DuckDB forbids concurrent writers). Resume on `status IN ('extracted_ok','format_unsupported','url_404','no_url')`; automatic retry on `url_error`/`extract_failed`.

The Phase 1 seed (legacy `data/txt/*` and `data/minutes_txt/*` JSONs → `document_text`) was a one-shot migration executed 2026-04-19 and is no longer in the repo; see `git log --all -- collect/migrations/migrate_legacy_docs.py` if you need to revisit the seeding logic.

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

## News sources

Two distinct news layers since 2026-05-21:

1. **Foreign (Guardian / NYT)** — JSON on disk under `data/news/*.json`, classified with the unified 10-attribute prompt via `classify_articles.py` → `data/analysis/articles_classified_{guardian,nyt}.json`. Title filter (must contain `\bAI\b | artificial intelligence | A\.I\.`) is enforced in `classify_articles.py::title_has_kw` so cross-source comparison stays apples-to-apples.

2. **Domestic Korean (KBS, MBC, SBS, YTN, 중앙일보, 한겨레)** — 157,886 articles (2018~2026) consolidated into `data/news/news.duckdb` (`news_articles` table, PK = `news_id`). Source JSONs archived under `data/news/raw_news_archive/` (gitignored). The earlier Naver Search API pipeline was retired with this dataset.

### 한국 도메스틱 뉴스 Strict AI 필터

한국 도메스틱 뉴스는 4규칙 Strict 필터로 정제 후 10속성 분류 → `news.duckdb::news_classifications` (PK `news_id × prompt_version`).

- **필터 정의**: [analyze/news_cleaning.py](analyze/news_cleaning.py) — `STRICT_WHERE` SQL export
- **규칙·근거·매체별 통계**: [analyze/news_cleaning.md](analyze/news_cleaning.md)
- **실행 흐름**: [WORKFLOW.md §3.5](WORKFLOW.md)
- **분류 스크립트**: [analyze/classify_news_kr.py](analyze/classify_news_kr.py) (sync) / [analyze/classify_news_kr_batch.py](analyze/classify_news_kr_batch.py) (Batch API, 50% 할인)

추가 자료:

- 방법론 전문: [data/exports/news_filtering_process.md](data/exports/news_filtering_process.md)
- 공급사 이슈 정리: [data/exports/news_dataset_issues.md](data/exports/news_dataset_issues.md)
- 정화 후 descriptive stats: [data/exports/news_descriptive_strict.md](data/exports/news_descriptive_strict.md)

## Assembly DuckDB access (split since 2026-05-09)

The data lives in **two DuckDB files** under `data/bills_kr/`:

- `data/bills_kr/assembly_raw.duckdb` — 37 API tables + extracted text (`bill_text`, `document_text`, `speeches`) + 9 thin wrapper views (`v_bill`, `v_member`, etc.). Written by `collect/*` scripts only.
- `data/bills_kr/assembly_analysis.duckdb` — `bill_classifications`, `bill_ai_filter`, `prompt_versions`, `speech_issues` + 2 cross-DB views (`v_kr_bills_analysis`, `v_bill_classifications_current`). Written by `classify_bills.py`.

Domestic Korean news lives in a third DB: `data/news/news.duckdb` (single `news_articles` table). Not attached to the analysis/raw split — open it standalone or attach explicitly.

The MCP server at [duckdb_mcp_server.py](duckdb_mcp_server.py) opens analysis as the main DB and ATTACHes raw read-only as `raw`. From the user's side: analysis tables/views are unqualified, raw tables/views are referenced as `raw.<name>` (e.g. `raw.v_bill`, `raw.bill_text`). All `bill_loaders.py` and `classify_bills.py` connections follow the same pattern. Tables use cryptic API codes (e.g. `raw.nwvrqwxyaytdsfvhu` for member info) — [CODEBOOK.md](CODEBOOK.md) maps all 37 tables to human-readable descriptions. Prefer the `v_*` views for analysis.

`config.RAW_DB_PATH` and `config.ANALYSIS_DB_PATH` are the canonical paths. `config.DB_PATH` remains as an alias to `ANALYSIS_DB_PATH` for legacy code.

Every per-age table now has a standardized `age INTEGER` column; the per-API derivation rule lives in `config.py::ApiSpec.age_source` and is enforced post-collection by [collect/validate_collection.py](collect/validate_collection.py) (called automatically at the end of `collect/download_all.py`). When making schema-touching changes to the collector, run the validator before committing.

## Common commands

```bash
# Assembly API collection (collect/ 폴더)
python collect/download_all.py                # full collect (13~22대, respects stale cache)
python collect/download_all.py --status       # progress check
python collect/download_all.py --api BILLRCP  # one API
python collect/download_bills.py --age 22     # KR bill PDF download + text extract
python collect/download_documents.py --source minutes_committee  # doc extract
python collect/validate_collection.py         # post-collect drift check

# AI pipeline — news (analyze/ 폴더)
python analyze/classify_articles.py all                # Guardian + NYT
python analyze/export_titles.py all                    # group titles by attribute
python collect/build_news_db.py                        # (re)build data/news/news.duckdb from raw_news_archive/

# AI pipeline — bills (analyze/ 폴더)
python analyze/classify_bills.py kr_22                 # single KR Congress (Stage 1+2 auto)
python analyze/classify_bills.py all                   # KR 19-22 + US 118-119 + EU act + amendments
python analyze/export_bills.py all                     # markdown for KR/US/EU (per subgroup + combined)

# Report
pdflatex report.tex                           # build LaTeX report
```

No test suite, linter, or CI — this is a research/analysis repo.

## Repository layout

- **Root** — shared infrastructure & public API: shared config (`config.py`), classification prompt (`prompts.py`), data gateway (`bill_loaders.py`), MCP server (`duckdb_mcp_server.py`).
- **[analyze/](analyze/)** — analysis-side scripts: classifiers (`classify_*.py`), exporters (`export_*.py`), subtopic NLP (`subtopic_*.py`), `compare_models.py`. Scripts here use `import _bootstrap` to find root modules (`config`, `prompts`, `bill_loaders`).
- **[collect/](collect/)** — all data collection·extraction scripts (Open API, bill PDFs, document attachments, foreign legislation, news APIs). Scripts here use `import _bootstrap` (collect/_bootstrap.py) to add repo root to sys.path so `import config` works regardless of script depth.

  Past one-shot migrations (Phase 1~5 backfills, view creation, DB split, retired Naver collectors v1~v3) are not kept on disk. Recover via `git log --all -- collect/` or `collect/_legacy/` if needed.
- **[figures/](figures/)** — visualization regeneration (`regenerate_all.py`).
  - `figures/_legacy/` — superseded viz scripts (`generate_figures.py`, `generate_timeline*.py`, `build_treemap*.py`, `_viz_attr_law.py`). `regenerate_all.py` is the canonical figure generator.
- **[replicate_carvao/](replicate_carvao/)**, **[kr_analysis/](kr_analysis/)** — frozen reference folders (see below).

## Two frozen reference folders

- **`replicate_carvao/`** — strictly the US paper replication (Carvão et al. 2025). Contains `02_collect_bill_details.py` through `07_visualize.py`, `us119_run_all.py`, `gen_us_report.py`, and (for now) `gen_eu_report.py` + EU legacy data. Do not add Korean content here.
- **`kr_analysis/`** — frozen historical KR pipeline. Contains the original 3-step KR filter (`kr_01_prepare_data.py` → `kr_02_preprocess.py` → `kr_03_nlp_classify.py`) plus `validate_tfidf_lda.py` (TF-IDF/LDA vs GPT cross-validation). The 2-stage filter in the active `classify_bills.py` is ported from `kr_analysis/kr_01_prepare_data.py` — keep that file as the historical reference when revisiting filter logic.

Both folders are intended as frozen references. Active code lives in [analyze/](analyze/) (analysis), [collect/](collect/) (collection), [figures/](figures/) (visualization), and the repo root (shared infrastructure).

## Data folder layout (since 2026-05-21)

```
data/
├── bills_kr/   assembly_raw.duckdb, assembly_analysis.duckdb, pdf_archives/{19..22}/, docs/
├── bills_us/   congress.duckdb (+ on-disk JSON via replicate_carvao/data/)
├── bills_eu/   eu_ai_act_*.{html,json}, eu_amendments_*.{html,json}
├── news/       news.duckdb (KR domestic), nyt_*.json, guardian_*.json, nyt_archive/, raw_news_archive/ (gitignored)
├── analysis/   articles_classified_{guardian,nyt}.json, subtopics_*.json, treemap_*.json, etc.
├── exports/    bills_{kr,us,eu}_*.md, titles_*.md (human-readable)
├── _archive/   legacy txt/, pdf/, minutes_txt/, bill_txt_*/  (do not read; use DB)
└── _audit/
```

## Output-file hygiene

- News classification (영문) uses JSON files at `data/analysis/articles_classified_{guardian,nyt}.json`; bill classification uses DB rows. Both retry errors on re-run. `classify_articles.py` and `classify_bills.py` both support `--force` to purge caches/outputs and reclassify.
- News classification output filenames are stable — do not introduce v2/v3 suffixed siblings. Bill classification versioning is via `prompt_versions.version` instead.
- `data/_archive/` holds pre-migration JSONs (`bill_txt_*/`, `bills_classified_*.json`, `kr_*_ai_filtered.json`, retired Naver collection artifacts) for ~3 months. Do not read from there — use the DB.

## When reading Korean output

Classification labels are always the English Carvão attribute strings (e.g. `"Responsible and ethical AI"`, `"Market efficiency and power concentration (antitrust)"`) regardless of article language. Korean labels appear only in human-facing markdown exports and the report.
