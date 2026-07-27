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
| [analyze/classify_articles.py](analyze/classify_articles.py) | News classifier (영문 소스: Guardian, NYT). Usage: `python analyze/classify_articles.py guardian`, `nyt`, or `all`. 한국 도메스틱 뉴스는 `data/news/news_analysis.duckdb` (별도 파이프라인 — [analyze/news_cleaning.py](analyze/news_cleaning.py) + [analyze/classify_news_kr*.py](analyze/classify_news_kr.py)). |
| [analyze/classify_bills.py](analyze/classify_bills.py) | Bill classifier. Usage: `python analyze/classify_bills.py {kr_19|kr_20|kr_21|kr_22|us_118|us_119|eu_act|eu_amendments|all}`. |
| [bill_loaders.py](bill_loaders.py) | Loader helpers `load_kr_bills()`, `load_us_bills()`, `load_eu_bills()` that join classification with source metadata. All downstream consumers (figures, reports, validators) go through this module — do not re-load `bills_classified_*.json` directly. |
| [collect/download_bills.py](collect/download_bills.py) | KR bill PDF downloader + fitz text extraction → `bill_text` table. Usage: `python collect/download_bills.py --age 22`. |
| [db_audit.py](db_audit.py) | DB 변경 이력 기록·조회. writer 스크립트가 `with db_audit.audit_run(...)` 로 감싼다. `python db_audit.py --log \| --runs \| --check \| --selftest`. 자세한 내용은 §DB 업데이트 감사 로그. |
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

- DuckDB in the same process cannot open a read-only connection while a write connection is open. `download_documents.py` and `classify_bills.py` both use the `_reader_con()` helper (returns the shared write connection if open) — preserve this when editing. 같은 이유로 [db_audit.py](db_audit.py)는 **대상 DB를 read-only로만, 그것도 순간적으로** 연다 (§DB 업데이트 감사 로그).
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

2. **Domestic Korean (KBS, MBC, SBS, YTN, 중앙일보, 한겨레)** — 157,886 articles (2018~2026) raw가 `data/news/news.duckdb`에, 2단계 정화·필터 후 76,645 articles + 분류 결과가 `data/news/news_analysis.duckdb`에 거주 (2026-05-28 분리). Source JSONs archived under `data/news/raw_news_archive/` (gitignored). The earlier Naver Search API pipeline was retired with this dataset.

### 한국 도메스틱 뉴스 정화 파이프라인 — 2단계 (since 2026-05-28)

raw `news.duckdb` → `news_analysis.duckdb` 빌드 시 두 단계가 한 SQL 안에서 합성:

- **Stage 1 (Boilerplate Removal)** — 본문 정화 (행 보존). Rule B1 (YTN footer 라인 제거), Rule B2 (MBC `(AI학습 포함)` substring 제거)
- **Stage 2 (AI Relevance Filter)** — 행 단위 통과/탈락. Rule R1 (AI 키워드 매칭, sanitized content + raw title), R2 (영문 본문 제외), R3 (조류인플루엔자 약자 충돌), R4 (사이버대학 광고), R5 (일반대 모집 광고 footer 3중 조합). 결과 76,645건 (2026-05-30 재빌드 기준. 2026-05-28 최초 빌드는 81,121건이었고 룰 조정으로 감소 — 이력은 `news_cleaning_runs` 참조).

분류 결과는 `news_analysis.duckdb::news_classifications` (PK `news_id × prompt_version`, 추가 컬럼 `cleaning_version` — 어느 룰 위에서 만들어진 분류인지 추적).

- **룰 정의 + 빌드 + CLI**: [analyze/news_cleaning.py](analyze/news_cleaning.py) — 두 단계 SQL 식(`SANITIZE_CONTENT_SQL`, `RELEVANCE_WHERE`) export + 빌드 entry point. `python analyze/news_cleaning.py [--force|--dry-run|--stage1-only]`
- **빌드 메타·히스토리**: `news_analysis.duckdb::news_cleaning_runs` (cleaning_version, rules_applied, sanitize/relevance hash, git SHA 누적)
- **규칙·근거·매체별 통계**: [analyze/news_cleaning.md](analyze/news_cleaning.md)
- **실행 흐름**: [WORKFLOW.md §3.5](WORKFLOW.md)
- **분류 스크립트**: [analyze/classify_news_kr.py](analyze/classify_news_kr.py) (sync) / [analyze/classify_news_kr_batch.py](analyze/classify_news_kr_batch.py) (Batch API, 50% 할인). 둘 다 `news_analysis.duckdb`에서 읽고 씀.

추가 자료:

- 방법론 전문: [data/exports/news_filtering_process.md](data/exports/news_filtering_process.md)
- 공급사 이슈 정리: [data/exports/news_dataset_issues.md](data/exports/news_dataset_issues.md)
- 정화 후 descriptive stats: [data/exports/news_descriptive_strict.md](data/exports/news_descriptive_strict.md)

## Assembly DuckDB access (split since 2026-05-09)

The data lives in **two DuckDB files** under `data/bills_kr/`:

- `data/bills_kr/assembly_raw.duckdb` — 37 API tables + extracted text (`bill_text`, `document_text`, `speeches`) + 9 thin wrapper views (`v_bill`, `v_member`, etc.). Written by `collect/*` scripts only.
- `data/bills_kr/assembly_analysis.duckdb` — `bill_classifications`, `bill_ai_filter`, `prompt_versions`, `speech_issues` + 2 cross-DB views (`v_kr_bills_analysis`, `v_bill_classifications_current`). Written by `classify_bills.py`.

Domestic Korean news is **also split** since 2026-05-28:

- `data/news/news.duckdb` — raw 157,886 articles in `news_articles`. Written by `collect/build_news_db.py` only.
- `data/news/news_analysis.duckdb` — Stage 1·2 적용본 + 분류 + 빌드 메타. Tables: `news_articles` (76,645, content는 Stage 1 적용본), `news_classifications` (+ `cleaning_version` 컬럼), `news_prompt_versions`, `news_cleaning_runs`. Written by `analyze/news_cleaning.py` and `analyze/classify_news_kr*.py`.

The MCP server at [duckdb_mcp_server.py](duckdb_mcp_server.py) opens `assembly_analysis.duckdb` as the main DB and ATTACHes **3 more DBs read-only**: `raw` (assembly_raw.duckdb), `news_analysis` (news_analysis.duckdb), `news_raw` (news.duckdb). From the user's side: assembly_analysis tables/views are unqualified, others are referenced with explicit prefix — `raw.v_bill`, `news_analysis.news_articles`, `news_raw.news_articles`. Assembly raw tables use cryptic API codes (e.g. `raw.nwvrqwxyaytdsfvhu`) — [CODEBOOK.md](CODEBOOK.md) maps all 37 tables to human-readable descriptions. Prefer the `v_*` views for analysis.

`config.RAW_DB_PATH` and `config.ANALYSIS_DB_PATH` are the canonical Assembly paths. `config.NEWS_DB_PATH` (= `NEWS_RAW_DB_PATH`) and `config.NEWS_ANALYSIS_DB_PATH` are the canonical news paths. `config.DB_PATH` remains as an alias to `ANALYSIS_DB_PATH` for legacy code.

## DB 업데이트 감사 로그 (since 2026-07-27)

정본 writer 스크립트가 4개 DuckDB 중 하나를 바꿀 때마다, 그 실행이 **어느 테이블을 어떻게 바꿨는지** [db_audit.py](db_audit.py)가 `data/_audit/db_updates.jsonl`에 append 한다. `_progress`(task별 최신 상태만 유지)나 `assembly_progress.json`(매 실행 덮어쓰기)과 달리 이력이 남는다.

계측된 스크립트(총 11개): `collect/download_all.py`·`download_bills.py`·`download_documents.py`·`build_news_db.py`, `analyze/classify_bills.py`·`classify_news_kr.py`·`classify_news_kr_batch.py`(`collect` 서브커맨드만)·`news_cleaning.py`(`build()`만)·`subtopic_bertopic.py`(`write_assignments_to_db()`만), `working/run_batch_sequential.py`·`persist_market.py`. 새 writer를 만들면 같은 3줄을 추가한다:

```python
if __name__ == "__main__":
    import db_audit
    with db_audit.audit_run(__file__, config.RAW_DB_PATH, argv=sys.argv[1:]):
        main()
```

설계상 반드시 지켜야 할 두 가지 (근거는 `db_audit.py` 모듈 docstring — 전부 실측으로 확인된 손상 경로다):

- **감사는 대상 DB에 write 커넥션을 열지 않고, 파이프라인 커넥션도 쓰지 않는다.** 파이프라인 커넥션을 빌려 쓰면 DuckDB에서 제3자의 `commit()`이 남의 미완 트랜잭션을 확정시켜(`save_rows()`가 DELETE 직후 Ctrl-C로 멈춘 상황) 데이터가 유실된다. RW를 먼저 열면 같은 프로세스의 read-only 접속이 전부 죽는다.
- **로그는 대상 DB 밖(JSONL)에 쓴다.** `news_cleaning.py`는 실패 시 `.bak`을 DB 파일 위에 덮어쓰므로, 감사 테이블이 그 안에 있으면 이력이 되감기고 열린 커넥션이 복원본을 손상시킬 수 있다.

변경 판정은 관측된 사실만 쓴다 — run 전후 read-only 스냅샷의 `COUNT(*)`·`duckdb_tables().sql` 해시 차이, 그리고 행 단위 write 타임스탬프(`extracted_at`/`classified_at`/`ingested_at` 등)로 센 "이번 run이 실제로 남긴 행 수"(행 수가 그대로인 UPDATE·upsert를 여기서 잡는다). writer 함수는 계측하지 않는다(무시된 `INSERT OR IGNORE`·롤백된 task를 실제보다 부풀린다). 행 수·스키마가 모두 그대로인 전면 재작성(`news_cleaning`의 CTAS, `build_news_db --rebuild`)만 스크립트가 `run.mark_rebuilt(table, 사유)`로 직접 선언한다.

```bash
python db_audit.py --log            # 변경이 있었던 실행만 (기본)
python db_audit.py --runs           # 실행 목록 (중단된 run 포함)
python db_audit.py --check          # 계측 밖 변경(수동 SQL·working/ 스크립트) 탐지
python db_audit.py --selftest       # 자가 검증 (임시 DB, 18개 항목)
```

MCP·DuckDB에서 직접 조회 (서버 변경 없이 그대로 된다):

```sql
SELECT script, argv, status, changed_tables, tables
FROM read_json_auto('/home/jays0967/assembly_data/data/_audit/db_updates.jsonl')
WHERE event = 'run_end' ORDER BY finished_at DESC LIMIT 20;
```

Every per-age table now has a standardized `age INTEGER` column — literally true since 2026-07-26, when [collect/migrations/migrate_age_integer.py](collect/migrations/migrate_age_integer.py) converted the last 9 tables that were still holding the API-native `AGE` as VARCHAR (they broke a direct `WHERE age >= 20`); the per-API derivation rule lives in `config.py::ApiSpec.age_source` and both the derivation and the INTEGER type are enforced post-collection by [collect/validate_collection.py](collect/validate_collection.py) (called automatically at the end of `collect/download_all.py`). When making schema-touching changes to the collector, run the validator before committing.

## Common commands

```bash
# Assembly API collection (collect/ 폴더)
python collect/download_all.py                # full collect (13~22대, respects stale cache)
python collect/download_all.py --status       # progress check
python collect/download_all.py --api BILLRCP  # one API
python collect/download_bills.py --age 22     # KR bill PDF download + text extract
python collect/download_documents.py --source minutes_committee  # doc extract
python collect/validate_collection.py         # post-collect drift check

# DB 업데이트 감사 로그 (루트)
python db_audit.py --log                      # 무엇이 언제 바뀌었나
python db_audit.py --runs                     # 실행 목록
python db_audit.py --check                    # 계측 밖 변경 탐지

# AI pipeline — news (analyze/ 폴더)
python analyze/classify_articles.py all                # Guardian + NYT
python analyze/export_titles.py all                    # group titles by attribute
python collect/build_news_db.py                        # (re)build raw data/news/news.duckdb from raw_news_archive/
python analyze/news_cleaning.py                        # raw → news_analysis.duckdb (Stage 1+2 적용 + 81k classifications 이주)
python analyze/news_cleaning.py --stage1-only          # Stage 1 영향만 측정 (DB 변경 없음)
python analyze/classify_news_kr.py                     # sync 분류 (소규모, smoke test)
python analyze/classify_news_kr_batch.py submit        # Batch API 분류 (production, 50% 할인)

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
- **[analyze/](analyze/)** — analysis-side scripts: classifiers (`classify_*.py`), exporters (`export_*.py`), subtopic NLP (`subtopic_*.py`), 뉴스 데이터/리포트 백본 (`news_descriptive.py`), 보고서 그림 정본 생성기 (`make_figures.py`), `compare_models.py`. Scripts here use `import _bootstrap` to find root modules (`config`, `prompts`, `bill_loaders`).
- **[collect/](collect/)** — all data collection·extraction scripts (Open API, bill PDFs, document attachments, foreign legislation, news APIs). Scripts here use `import _bootstrap` (collect/_bootstrap.py) to add repo root to sys.path so `import config` works regardless of script depth.

  Past one-shot migrations (Phase 1~5 backfills, view creation, DB split, retired Naver collectors v1~v3) are not kept on disk. Recover via `git log --all -- collect/` or `collect/_legacy/` if needed.
- **[figures/](figures/)** — 그림 **산출물** 디렉터리 (`*.png` + `figures_data.xlsx`). 보고서가 `figures/*.png`를 고정 참조한다. 생성기는 더 이상 여기 없고 [analyze/make_figures.py](analyze/make_figures.py)가 정본 (구 `regenerate_all.py`; 데이터 갱신 후 그림이 필요하면 `python analyze/make_figures.py` 재실행).
  - 옛 viz 스크립트(`figures/_legacy/`, `figures/temporal_top10.py`)는 2026-07-27 삭제됨 — `analyze/make_figures.py`가 정본. 필요하면 `git log --all -- figures/` 로 복구.
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
├── news/       news.duckdb (KR raw 157k), news_analysis.duckdb (Stage 1+2 적용본 81k + classifications + cleaning_runs), nyt_*.json, guardian_*.json, nyt_archive/, raw_news_archive/ (gitignored)
├── analysis/   articles_classified_{guardian,nyt}.json, subtopics_*.json, treemap_*.json, etc.
├── exports/    bills_{kr,us,eu}_*.md, titles_*.md (human-readable)
├── _archive/   legacy txt/, pdf/, minutes_txt/, bill_txt_*/  (do not read; use DB)
└── _audit/     db_updates.jsonl (DB 변경 이력, append-only), state.json (마지막 스냅샷), pre_migration.json
```

## Output-file hygiene

- News classification (영문) uses JSON files at `data/analysis/articles_classified_{guardian,nyt}.json`; bill classification uses DB rows. Both retry errors on re-run. `classify_articles.py` and `classify_bills.py` both support `--force` to purge caches/outputs and reclassify.
- News classification output filenames are stable — do not introduce v2/v3 suffixed siblings. Bill classification versioning is via `prompt_versions.version` instead.
- `data/_archive/` holds pre-migration JSONs (`bill_txt_*/`, `bills_classified_*.json`, `kr_*_ai_filtered.json`, retired Naver collection artifacts) for ~3 months. Do not read from there — use the DB.

## When reading Korean output

Classification labels are always the English Carvão attribute strings (e.g. `"Responsible and ethical AI"`, `"Market efficiency and power concentration (antitrust)"`) regardless of article language. Korean labels appear only in human-facing markdown exports and the report.
