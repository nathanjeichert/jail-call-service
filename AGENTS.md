# AGENTS.md - Jail Call Service Context

Codebase context for AI coding agents working on `jail-call-service`
([github.com/nathanjeichert/jail-call-service](https://github.com/nathanjeichert/jail-call-service)).
Read this before changing anything; update it when you change the architecture.

* **What it is:** a local-first batch tool that turns folders of G.729 jail call recordings (thousands per job) into a delivery zip: per-call transcript PDFs, MP3 audio, one offline HTML page (searchable call index + synced audio viewer), a case report, and a user guide.
* **Who runs it:** the operator (Nathan) on a Mac. Deliveries are opened by attorneys on locked-down government machines with no network, so every client-facing artifact is a self-contained file that works from `file://`.
* **Repo hygiene:** single `main` branch. `test files/` holds real recordings and is gitignored; the repo is public, so never commit call data, job output, or `.env`.

## Layout

```
pyproject.toml         dependencies (extras: local = mlx-lm, dev = pytest + ruff), ruff + pytest config
.github/workflows/     ci.yml (ruff, pytest on macOS, tsc, next build); build-fluidaudio.yml
backend/
  server.py            FastAPI routes (thin; delegates to job_store / pipeline)
  pipeline.py          job orchestration: generic stage runner, delivery assets, zip, repackage
  events.py            per-job SSE progress queues
  job_store.py         SQLite persistence (SQLAlchemy); the only module touching db tables
  db.py                engine + ORM tables (DBJob, DBCall)
  models.py            pydantic domain models (Job, CallResult, TranscriptTurn, WordTimestamp), call_stem()
  config.py            settings from env/.env
  prompts.py           the per-call summary prompt + note-count guidance
  engine_registry.py   EngineSpec / EngineRegistry: label, local/cloud, readiness, factory per engine
  job_settings.py      runtime selection (local flags, worker caps, validation via the registries), prompt composition
  audio_converter.py   ffmpeg discovery, audio file discovery, WAV -> MP3 (with wav_repair.py)
  icm_parser.py        GTL/ViaPath ICM_report.xml -> per-call metadata
  transcript_layout.py page:line layout core shared by every surface (see "Alignment")
  summaries.py         canonical summary text: parse, normalize, render
  system_audio.py      strip/relabel automated telecom turns given engine markers
  formatting.py        durations, timestamps, dates, truncation
  html_json.py         script-safe JSON embedding
  transcription/       TranscriptionEngine protocol; REGISTRY; assemblyai_engine, parakeet_engine
  summarization/       SummarizationEngine ABC; REGISTRY; gemini_engine (JSON), gemma_engine (text);
                       schemas.py (typed results), json_protocol.py, text_protocol.py
  delivery/            everything inside the client zip (see "Delivery artifacts")
    call_view.py       CallView: one parsed/laid-out presentation per call, built once for every surface
    transcript_pdf.py, case_report.py, guide_pdf.py, index_html.py
    theme.py           composes fonts + assets/css/* into the one <style> block every template injects
    pdf_render.py (headless Chromium), templates.py, fonts.py, font_metrics.py, summary_layout.py
    templates/*.html   Jinja, all four (transcript_pdf, case_report, guide, index)
    assets/css/        tokens.css, print.css, screen.css, components.css (the design system)
    assets/            fonts/, vendor/paged.polyfill.js, guide/ screenshots
frontend/src/
  lib/api.ts           typed client for every endpoint + shared formatting helpers
  components/ToggleGroup.tsx
  app/page.tsx         job list + new-job form
  app/jobs/[id]/page.tsx        job progress (SSE with polling fallback), controls, call table
  app/jobs/[id]/review/page.tsx transcript review + summary editing
tests/                 pytest suite + make_test_package.py (synthetic delivery builder)
  golden/              digests of every synthetic artifact; test_delivery_golden.py diffs against them
```

**Tech stack:** FastAPI + SQLAlchemy/SQLite backend; Next.js 16 / React 19 / Tailwind 4 / TypeScript frontend (Node >= 20.9); ffmpeg CLI; Playwright headless Chromium for PDFs; AssemblyAI and google-genai SDKs; FluidAudio CoreML and mlx-lm for local engines. Install with `pip install -e ".[dev]"` (add `,local` for Gemma); `pyproject.toml` is the only dependency list.

**Import rules:** `config`, `prompts`, `models`, `formatting`, `engine_registry` are leaves. `summaries` may import `delivery.summary_layout` (it needs the page budget) but nothing else in `delivery`. `delivery.call_view` is the only delivery module that imports `summaries` and `transcript_layout`; the generators read views. `job_settings` imports the engine packages lazily inside functions. `pipeline` imports `delivery.*` lazily inside functions so the API server starts without Playwright. Adding a top-level import that closes a cycle will show up as an ImportError on `import backend.server`; run the test suite after touching imports.

## Pipeline (`backend/pipeline.py`)

Assembly line: `[convert] -> [transcribe] -> [summarize] -> [pdf]`, four worker pools joined by asyncio queues, then a batch stage builds delivery assets and zips.

* **Stage runner.** Each stage is a `_Stage(name, job_stage, workers, process, on_error, queue, downstream)`. One `_worker` loop serves all four: pull an item, run `process`, emit a `call_update` event, forward the result downstream. `on_error` is the per-stage failure policy: conversion/transcription/PDF failures mark the call `ERROR` and stop it; a summarization failure is a soft failure (placeholder summary, `error` set, call continues to the PDF stage). Sentinels released by an upstream stage tell the downstream pool to drain.
* **Worker counts.** Conversion = CPU count - 1 (thread pool); transcription/summarization from `job_settings.RuntimeSelection` (`MAX_TRANSCRIPTION_CONCURRENT` / `MAX_SUMMARIZATION_CONCURRENT`, default 50, tuned to AssemblyAI's 100 new streams/min and Gemini Flash Tier 1's 300 RPM; local engines cap at `MAX_PARAKEET_CONCURRENT`=2 and 1 for Gemma); PDF = min(8, calls). All-local jobs (Parakeet + Gemma) run in two phases so both models never share unified memory.
* **Resumability.** Every stage transition is checkpointed via `job_store.update_call`. `_run_pipeline` routes each call into the queue matching its stored `CallStatus`, keyed by `original_path`, so pause/resume/crash recovery never re-spends API credits. **Resume the same job id; do not re-upload.** Pausing is cooperative: workers poll `job_store.get_job_stage` between items. `job_store.pause_orphaned_jobs()` runs at server startup so a job killed mid-run shows as paused, not running.
* **Validation up front.** `validate_runtime_selection` raises before any conversion if a selected cloud engine's key is missing or an engine is unavailable.
* **Engines are created once per run** and passed into the stages; the summarization engine stays loaded through case-report synthesis, then `unload()` frees local model memory.
* **Delivery assets** (`_stage_generate_delivery_assets`): builds one `CallView` per completed call (`delivery/call_view.build_call_views`: summary parsed, cues hydrated, page:line layout computed, filenames derived, once), then writes index.html, guide.pdf, case-report.pdf in parallel threads from the same views; a failed asset emits a warning and does not fail the job. The PDF stage (`_generate_pdf_one`) builds a view per call and renders both transcript variants from it. `gen_date` overrides the "Generated" stamp (tests pin it); production leaves it as today. `_stage_package` zips `output/` as `<SafeCaseName>/...`. `repackage_job` re-runs both after summary edits, re-creating the same summarization engine the job used.
* **Events** (`backend/events.py`): thread-safe bounded queue per job; `server.job_events` streams it as SSE with 30 s pings. The frontend reloads the job on every event and falls back to 3 s polling.
* **Speaker labels.** `_build_channel_labels`: channel 1 (left) = inmate by default; the job's `speaker_assignment` (`left_inmate` / `right_inmate`) swaps sides. The inmate label is the ICM inmate name, else the job's defendant name, else `INMATE`; the transcript cover uses the same fallback.

## Persistence (`backend/job_store.py`, `backend/db.py`)

SQLite at `jobs/jail_calls.db` with WAL mode and a 30 s busy timeout for concurrent writers. `job_store` maps rows to pydantic models generically from the table columns (add a column to `db.py` and it flows through; `_ensure_schema()` adds missing columns to older local databases at import). Prefer the narrow readers: `get_job_lite` / `list_jobs` skip the transcript JSON, `get_call` loads one call, `get_job` loads everything. Per-job files live in `jobs/<id>/` (`output/` is what gets zipped; `source-working/` holds repaired copies of the source audio so originals are never mutated).

## Engine registries (`backend/engine_registry.py`)

Each stage package exposes `REGISTRY`, an `EngineRegistry` of `EngineSpec`s: `id`, UI `label`, `local`, `installed()` (dependency present), `requirement()` (None when it can run now, else the missing key/binary as a sentence), `create(**kw)`, `install_hint`, and `max_concurrency` for local models. `describe()` feeds `GET /api/config` (`transcription_engines` / `summarization_engines`, each row `{id, label, local, installed, ready, requirement}`); the frontend renders the menu and any "not ready" warning from those rows and names no engine itself. `job_settings.RuntimeSelection` reads `local` and `max_concurrency` from the spec; `validate_runtime_selection` raises the spec's `requirement`. **Adding an engine = write its module and add one `EngineSpec` to the stage's `REGISTRY`.** Nothing else in the backend or UI should name an engine. `tests/test_engine_registry.py` covers the shape, readiness, and validation with fake registries.

## Transcription engines (`backend/transcription/`)

`get_engine(name)` (= `REGISTRY.create`) returns an object with `async transcribe(audio_path, channel_labels) -> List[TranscriptTurn]`. Both engines return the same shape: ordered turns with `speaker`, `text`, `timestamp` (`[MM:SS]`), optional word-level `words` (ms), and `is_continuation` (same speaker as previous turn).

* **AssemblyAI (cloud):** stereo MP3 in, `multichannel=True`, submit-then-poll bounded by `ASSEMBLYAI_TRANSCRIPTION_TIMEOUT_SEC` (900 s; a stuck transcript raises `TimeoutError` and errors only that call). No `prompt` parameter (it caused spurious `[SPEAKER]` tags).
* **Parakeet (local):** splits stereo into two 16 kHz mono WAVs with ffmpeg, runs `bin/fluidaudiocli` (CoreML, gitignored ~7 MB; models download to `~/Library/Application Support/FluidAudio/Models` on first use) per channel, segments each word stream on 1.5 s gaps, and interleaves the segments by start time. Turn merging is simple; see `todo.md` item 5.

## Summarization engines (`backend/summarization/`)

Every engine subclasses `SummarizationEngine` (`base.py`); the pipeline and case report only use that interface. Three async operations, each returning its result plus `TokenUsage`:

* `detect_system_audio(turns, metadata)` — only when `system_audio_prepass = True` (Gemini). Runs before summarization so the summary sees a filtered transcript.
* `summarize_call(turns, prompt, metadata, *, detect_system_audio)` — returns `CallSummary` with either `structured` (a `SummaryResponse`) or `text` (raw model output). Inline-detecting engines (Gemma) fold detection into this one call and return `system_audio_markers`.
* `synthesize_case_report(CaseReportInputs)` — findings + outside-party identities as `CaseReportResponse`.

`pipeline._summarize_with_engine` is the only place that sequences these (prepass filter → summarize → post-filter for inline engines → normalize), and `delivery/case_report._run_synthesis` is the only caller of synthesis (3-attempt tenacity retry, 120 s per-attempt timeout). **Adding an engine = subclass the ABC and add an `EngineSpec` to `summarization.REGISTRY` (see "Engine registries"). Nothing in the pipeline or delivery code should branch on engine type.** `tests/test_summarize_stage.py` drives the stage with fake engines of both shapes.

* **Gemini** (`gemini_engine.py`, prompts in `json_protocol.py`): `gemini-3-flash-preview`; native JSON output against the pydantic schemas in `schemas.py`; `thinkingLevel` low for detection, medium for summary and synthesis (`GEMINI_*_THINKING_LEVEL`). Summary notes carry only `line_ref` + `reason` + `importance_rank`; every rendered timestamp, speaker, and pull quote is derived from the cited transcript lines (`transcript_layout.hydrate_review_cues`), never model-authored text.
* **Gemma** (`gemma_engine.py`, prompts + parsers in `text_protocol.py`): Gemma 4 E2B 4-bit via mlx-lm; lazy load with a warm-up pass, `unload()` after the run; strips the `<|channel>thought…<channel|>` reasoning prelude and raises if the budget ran out mid-thinking (recorded as a partial failure rather than saving reasoning as the summary). Case-report output is `FINDING_START…FINDING_END` / `IDENTITY_START…IDENTITY_END` blocks parsed into the same schemas. Local-only mode is parked (`todo.md` item 6).

**Summary shape** (`backend/prompts.py`): `RELEVANCE: HIGH/MEDIUM/LOW`; `NOTES:` timestamped attorney-relevant moments only or exactly `NOTES: NONE`; `IDENTITY OF OUTSIDE PARTY:` only when supported; `BRIEF SUMMARY:` one or two sentences. Do not make the model add notes merely to orient the reader to routine conversation. Prefer colons over em dashes in prompts and template copy.

**Normalization** (`backend/summaries.py`): before persistence every summary becomes the canonical text format: leaked preambles stripped, notes capped by tier (`SUMMARY_NOTE_GUIDANCE`, hard max 21), duplicates and weak notes dropped by importance rank, identity/brief text shortened to card length, and the kept note set reduced until it fits the summary-sheet page budget (`SUMMARY_PAGE_LIMITS`, via `delivery/summary_layout.paginate_structured_summary`). All delivery surfaces read summaries back through `summaries.parse_summary_sections` (which also accepts older REVIEW CUES / KEY FINDINGS formats). `DUMMY_SUMMARY_PREFIX` marks skip-summary test output, which the surfaces treat as "no summary".

## Automated messages (`backend/system_audio.py`)

Provider IVR prompts, time warnings, and sign-offs play into both channels, so they appear on both speakers' turns. Engines return markers `{"turn": int, "text": str}` (Gemini: separate prepass on the raw turn transcript; Gemma: `SYSTEM_AUDIO: [...]` tail parsed by `text_protocol.parse_system_audio_response`). The job's `auto_message_mode` decides what happens: `None`/"Keep" leaves turns alone; `"exclude"` removes the system text (splitting mixed turns); `"label"` relabels it as speaker `AUTOMATED MESSAGE`, collapsing both channel copies into one turn. Default in the UI/API is `"label"`. Filtering only runs when summarization is active; `remove_system_audio_notes` also drops NOTES bullets that merely restate a detected message. Matching is fuzzy on purpose (a note is dropped when it contains the message or shares two or more content words within 3 s of it). A deterministic, model-free preamble pass is parked as `todo.md` item 4.

## Job metadata & ICM XML

Per-call metadata (inmate name/PIN, outside number, date/time, housing unit, outcome, call type) comes from the provider's `ICM_report.xml` via `backend/icm_parser.py`, matched to audio by `<recordfilename>`; `CallMeta`'s fields are named identically to `CallResult`'s so the pipeline copies them with `asdict`. Case-level metadata (case name, defendant, case context) is operator-entered. `POST /api/upload/xml` and `/api/xml/preview` return a parsed preview the UI shows and uses to prefill the defendant name.

## Alignment: page:line is one computation

`backend/transcript_layout.compute_line_entries(turns, duration)` is the **single source of truth** for how transcript text wraps into 62-character lines and 25-line pages. It feeds:

* the transcript PDF sheets (`delivery/transcript_pdf.py`),
* `index.html`'s printed transcript pages and its full-text search (the page embeds each call's line entries once and re-derives speaker turns from them),
* every `Tr. page:line` cite in `index.html` and the case report,
* the `[Page:Line]` references the summarization prompt asks the model to cite (`summarization/base.build_transcript_text`), which later resolve back to lines, timestamps, and pull quotes via `resolve_line_ref_context` / `hydrate_review_cues`.

`MAX_LINE_CHARS = 62` is pinned there; `delivery/transcript_pdf.py` asserts at import that its ruled-corridor geometry still yields 62 columns. **Changing the wrapping rules shifts every citation in the product.** `create_pdf` asserts the rendered page count equals the emitted sheet count and refuses to deliver on mismatch.

## Delivery artifacts (`backend/delivery/`)

Every generator takes the `CallView` list the delivery stage built (`call_view.py`): `view.call` (the `CallResult`), `stem` / `audio_filename` / `pdf_filename`, `line_entries`, `summary` (stored text) with `is_dummy`, `sections` (parsed) with `structured` / `relevance` / `brief` / `identity`, and `cues` (hydrated dicts with `timestamp`, `speaker`, `quote`, `note`, `line_cite`, plus `seconds` and `page`). Parse or lay a call out anywhere else and the surfaces can drift; add a derived field to the view instead.

All four templates are Jinja (`templates.render_template`, `autoescape=False`: pass pre-escaped strings and `html_json.dump_script_safe_json` output). All PDFs are Jinja HTML rendered by headless Chromium via `pdf_render.render_pdf(html, *, paged=False)`: a thread-safe sync facade over async Playwright on a dedicated daemon thread (one shared browser, fresh context per render, semaphore-gated, single relaunch-and-retry on crash). Setup: `python -m playwright install chromium` once. `paged=True` injects the vendored Paged.js polyfill for `@page` margin boxes, running footers, and page counters (case report only); `paged=False` prints explicit fixed-size sheets (transcript, guide).

* **Transcript PDF** (`transcript_pdf.py`, `templates/transcript_pdf.html`): cover sheet, summary sheet(s), then legal-deposition transcript sheets (Courier Prime, line numbers, double-rule corridor, 25 lines/page). Every page is an explicit `8.5in x 11in` sheet with `overflow: hidden`; Python decides all pagination. Summary pagination is height-based (`summary_layout.py`: measured advance widths in `font_metrics.py` times a 1.06 safety factor); identity and brief summary stay on page 1, overflow pages carry notes only, cap 3 summary pages. If you edit the summary layout, update both the estimator and the CSS: an under-estimate clips content inside the fixed sheet. Visually verify with `pdftoppm` before calling it done.
* **Case report** (`case_report.py`, `templates/case_report.html`, Paged.js): parses every summary once, buckets by relevance, selects synthesis inputs (HIGH first, topping up from MEDIUM to `TARGET_FINDINGS_INPUT_COUNT`=10), runs one synthesis call (findings + identities), then renders at-a-glance metrics, an adaptive-granularity call timeline (day/week/month/year by span; inline SVG precomputed in `_build_timeline_svg`), top findings, HIGH call cards, MEDIUM rows, and caller stats with inferred identities. `synthesis_state` (`ok` / `no_input` / `synth_unavailable` / `parse_failed`) drives a differentiated empty state so a failed synthesis still yields a usable report. **Link portability:** hrefs are relative (`index.html#call=…&t=…` from `_viewer_link`, `transcripts/<stem>.pdf`); Chromium bakes them as absolute `file:///tmp/...`, so `_rewrite_local_links_to_launch_actions` strips them back and converts to `/Launch` actions (`tests/test_case_report_links.py`). Paged.js does not replicate `position: fixed` per page: per-page chrome is styled on `.pagedjs_page`. The call card deliberately lacks `page-break-inside: avoid` (only `.cc-head`/`.cc-foot` do) so long cue tables flow across pages. Dry-run a job and eyeball the PDF after template edits.
* **Guide** (`guide_pdf.py`, `templates/guide.html`): 6 fixed sheets (cover, contents, the index view, the call view, the case report, reading the analysis); screenshot PNGs in `assets/guide/` are synthetic ("State v. Marcus Reeves"), safe to ship; missing PNGs render as dashed placeholders, so keep them present. Copy is usage information only, no review-order advice (client preference). `tests/test_guide_layout.py` pins the page count and footer tokens.
* **Index page** (`index_html.py`, `templates/index.html`): the one HTML deliverable, self-contained, two views switched by the URL hash. `#index` (or no hash) is the call index, the client's home page: masthead stats, sticky filter bar (full text, date range, number, relevance chips and the clickable tally), one row per call with relevance marker, expandable summary/cues/full transcript with match stepping, Viewer/PDF buttons. `#call=<audio filename>&t=MM:SS` is the call view: left call rail, transport + printed transcript pages, right analysis rail with clickable cues, `← Call Index` control in the band. Native `<audio>` behind a small `ws` shim (no WaveSurfer, no remote scripts: Web Audio cannot decode local files over `file://`; pinned by `tests/test_delivery_html.py`). Both rails collapse (`localStorage` `jcs_calls_collapsed` / `jcs_summary_collapsed`); present mode (`P`, Esc exits) shows only transport + transcript sheets; words are clickable `span.word-ts` with `data-ws`/`data-we`; transcript search shows all pages and hides non-matching ones. A `t` deep link reveals and highlights the line immediately and seeks the audio once its metadata loads, staying paused. The legacy `?call=…&t=…` query form (already-shipped case reports) is rewritten to the hash form on load with `history.replaceState`. Navigation is `location.hash` only, so back/forward replay views via `hashchange`; the index's filter, query, sort, open row, and scroll position live in its own state and survive a round trip through a call. **Data ships once:** `build_call_payload` embeds each call's `line_entries` (the printed pages) and the page re-derives speaker turns from them for search and excerpts; there is no second transcript copy. The script is one ES2017 IIFE in five parts: data layer (`Data`), shared helpers (`esc`, `highlight`, `relMark`, `formatTime`, `hashTime`, `parseTime`, date formatters), `Router`, `IndexView`, `CallView`; `window.JCS` exposes them for the browser tests only. CSS for each view is scoped to `.view-index` / `.view-call` (the call view runs `--lbl-size`/`--rv-*` one notch smaller and uses `font-size: 1rem`), shared rules come first. Lint errors inside the template's JS are expected (Jinja placeholders).
* **Zip layout is flat:** `index.html`, `guide.pdf`, `case-report.pdf` at the root beside `audio/`, `transcripts/`, `transcripts-no-summary/`. No subdirectory for the page; its audio URL is `audio/<mp3>` and the case report links to `index.html#call=…`. Per-call filenames come only from `models.call_stem(index, filename)`.

**Design system ("Record")** lives in `delivery/assets/css/` and is injected by `theme.theme_css(medium)` as the first thing in every template's `<style>` (`{{ theme_css }}`), in four layers: `tokens.css` (palette + type stacks), `print.css` or `screen.css` (sizes for the medium, paper color, and for print the sheet chrome the PDFs share: spine, cover block, `.facts` rows, chapter headers), and `components.css` (relevance marker `.rv` / `.rv--high|medium|low|none`, `.lbl`, `.eyebrow`, `.cell .n` numerals). A template's own CSS follows and may override a token (index.html's call view runs `--lbl-size`/`--rv-*` one notch smaller) or a rule (the guide's larger cover title) in a short block at the end of its style; **change the look in the css/ files, touch a template only for layout.** Tokens: paper `#F5F2EA` / cream `#FBF9F3` / sheet white (print sets paper white); ink `#16140F`, body `#45413A`, muted `#807A6E`; rules `rgba(22,20,15,.18)` and `.09`. One signal color, crimson `#A8271E` (text `#871F18`), reserved for HIGH relevance, review-cue timestamps, chart peaks, and the playhead; MEDIUM is a hollow ochre square (`#8F6400`), LOW hollow gray (`#76796E`). Type: Fraunces (display, optical-size axis) for titles and big numerals, Public Sans for UI/body, IBM Plex Mono for timestamps/numbers/cites, Courier Prime metric-locked to transcript sheets (its rules stay in `transcript_pdf.html`; the 62-column geometry depends on them). `fonts.py` serves `@font-face` as `file://` URIs for PDFs and base64 data URIs for index.html (all SIL OFL; licenses beside the files); index.html inlines Courier Prime too, for its printed pages. Headings are flat labels ("At a Glance"); no explanatory ledes inside artifacts; header bands carry only the document/case label (the "Privileged & Confidential" line was removed from headers at client request and survives only in footers). Charts are hand-rolled inline SVG. To verify a styling change is visual-only: build the synthetic package before and after, screenshot both of index.html's views with Playwright (1400x900 from a `file://` URL; `#call=…&t=…` for the call view) and rasterize the PDFs with `pdftoppm`, and pixel-compare; the golden test catches everything that changes text or pagination.

## Frontend

Next.js app under `frontend/`, proxying `/api/*` to the backend (`next.config.js`). `lib/api.ts` is the typed client and the only place URLs live; page components handle UI state only. Engine menus and "not ready" warnings come from `/api/config`'s registry rows (`EngineInfo`); the frontend hardcodes no engine names or labels. Tailwind 4 uses `@import "tailwindcss"` and `@tailwindcss/postcss`; do not restore Tailwind 3 directives. `next.config.js` pins `turbopack.root` so Next does not infer the home directory as the workspace. Verify with `npx tsc --noEmit` and `npx next build` in `frontend/`.

## Testing

Run with the pyenv Python (system `python3` is stale). `python -m ruff check backend tests` lints (pyflakes, pycodestyle, isort; long lines allowed). CI runs lint, the suite (on macOS, because the golden PDF digests depend on the platform's font metrics), `tsc`, and `next build` on every push.

1. **Unit / regression suite:** `python -m pytest tests/` (fast, no network, no keys; Chromium is used for the PDF tests).
   * `test_delivery_golden.py` — **the golden package:** builds the synthetic delivery with a pinned date and no audio, digests every artifact (HTML verbatim with font payloads masked; per-page PDF text plus every link annotation), and diffs against `tests/golden/`. Any change to a template, the layout core, summaries, or delivery code shows up here. When the change is intended, review the diff, then `UPDATE_GOLDEN=1 python -m pytest tests/test_delivery_golden.py`; on failure the actual digests are in `test-output/golden-actual/`.
   * `test_engine_registry.py` — registry description shape, readiness, runtime-selection flags and validation
   * `test_summarize_stage.py` — the summarize stage with fake JSON and text engines (filter order, note removal, token accounting)
   * `test_text_protocol.py` — Gemma block parsers
   * `test_transcript_summary_layout.py` — summary pagination, page budget, note curation
   * `test_quote_line_refs.py` — line-ref → quote hydration
   * `test_case_report_links.py` — `/Launch` link rewriting (the `index.html#call=…` hash form)
   * `test_delivery_browser.py` — index.html in headless Chromium from `file://` (synthetic package, no audio): rows and stats, search highlighting, relevance chips, expanded cues with `Tr. page:line` cites, cue click opens the call view at the time, back keeps the query, deep link and legacy query redirect, present mode, rail collapse; every test asserts no console errors
   * `test_delivery_html.py` — script-safe JSON embedding; no remote scripts in index.html; one transcript copy and inlined Courier; theme layers per medium; lowercase marker classes
   * `test_guide_layout.py` — guide page count + footer clearance
   * `test_pdf_render.py` — the Chromium facade
   * `test_pipeline_audio_regressions.py` — conversion works on a copy, discovery, labels
2. **Synthetic delivery package:** `python tests/make_test_package.py` (`--calls N`, `--zip`, `--no-audio`) builds a complete delivery at `test-output/REEVES_TEST_PACKAGE/` through the real delivery code with a stub synthesis engine and no job database (`build_package()` is what the golden test calls). **Build it and review every artifact by hand after any significant change** to the delivery code or templates; the golden test tells you what changed, your eyes tell you whether it should have.
3. **End to end without API spend:** create a job with `skip_summary` and the Parakeet engine on one recording; it exercises conversion, transcription, both PDFs, every delivery asset, and the zip.

## Parked work

See `todo.md`: offline preamble stripping, Parakeet turn merging, revisiting local-only mode, plus older product ideas.
