# AGENTS.md - Jail Call Service Context

Codebase context for AI coding agents working on `jail-call-service`
([github.com/nathanjeichert/jail-call-service](https://github.com/nathanjeichert/jail-call-service)).
Read this before changing anything; update it when you change the architecture.

* **What it is:** a local-first batch tool that turns folders of G.729 jail call recordings (thousands per job) into a delivery zip: per-call transcript PDFs, MP3 audio, an offline viewer, a searchable index, a case report, and a user guide.
* **Who runs it:** the operator (Nathan) on a Mac. Deliveries are opened by attorneys on locked-down government machines with no network, so every client-facing artifact is a self-contained file that works from `file://`.
* **Repo hygiene:** single `main` branch. `test files/` holds real recordings and is gitignored; the repo is public, so never commit call data, job output, or `.env`.

## Layout

```
backend/
  server.py            FastAPI routes (thin; delegates to job_store / pipeline)
  pipeline.py          job orchestration: generic stage runner, delivery assets, zip, repackage
  events.py            per-job SSE progress queues
  job_store.py         SQLite persistence (SQLAlchemy); the only module touching db tables
  db.py                engine + ORM tables (DBJob, DBCall)
  models.py            pydantic domain models (Job, CallResult, TranscriptTurn, WordTimestamp), call_stem()
  config.py            settings from env/.env
  prompts.py           the per-call summary prompt + note-count guidance
  job_settings.py      engine/runtime selection, case-context prompt composition
  audio_converter.py   ffmpeg discovery, audio file discovery, WAV -> MP3 (with wav_repair.py)
  icm_parser.py        GTL/ViaPath ICM_report.xml -> per-call metadata
  transcript_layout.py page:line layout core shared by every surface (see "Alignment")
  summaries.py         canonical summary text: parse, normalize, render
  system_audio.py      strip/relabel automated telecom turns given engine markers
  formatting.py        durations, timestamps, dates, truncation
  html_json.py         script-safe JSON embedding
  transcription/       TranscriptionEngine protocol; assemblyai_engine, parakeet_engine
  summarization/       SummarizationEngine ABC; gemini_engine (JSON), gemma_engine (text);
                       schemas.py (typed results), json_protocol.py, text_protocol.py
  delivery/            everything inside the client zip (see "Delivery artifacts")
    transcript_pdf.py, case_report.py, guide_pdf.py, viewer.py, search_html.py
    pdf_render.py (headless Chromium), templates.py, fonts.py, font_metrics.py, summary_layout.py
    templates/*.html   Jinja (transcript_pdf, case_report, guide) and static (viewer, search)
    assets/            fonts/, vendor/paged.polyfill.js, guide/ screenshots
frontend/src/
  lib/api.ts           typed client for every endpoint + shared formatting helpers
  components/ToggleGroup.tsx
  app/page.tsx         job list + new-job form
  app/jobs/[id]/page.tsx        job progress (SSE with polling fallback), controls, call table
  app/jobs/[id]/review/page.tsx transcript review + summary editing
tests/                 pytest suite + make_test_package.py (synthetic delivery builder)
```

**Tech stack:** FastAPI + SQLAlchemy/SQLite backend; Next.js 16 / React 19 / Tailwind 4 / TypeScript frontend (Node >= 20.9); ffmpeg CLI; Playwright headless Chromium for PDFs; AssemblyAI and google-genai SDKs; FluidAudio CoreML and mlx-lm for local engines.

**Import rules:** `config`, `prompts`, `models`, `formatting` are leaves. `summaries` may import `delivery.summary_layout` (it needs the page budget) but nothing else in `delivery`. `pipeline` imports `delivery.*` lazily inside functions so the API server starts without Playwright. Adding a top-level import that closes a cycle will show up as an ImportError on `import backend.server`; run the test suite after touching imports.

## Pipeline (`backend/pipeline.py`)

Assembly line: `[convert] -> [transcribe] -> [summarize] -> [pdf]`, four worker pools joined by asyncio queues, then a batch stage builds delivery assets and zips.

* **Stage runner.** Each stage is a `_Stage(name, job_stage, workers, process, on_error, queue, downstream)`. One `_worker` loop serves all four: pull an item, run `process`, emit a `call_update` event, forward the result downstream. `on_error` is the per-stage failure policy: conversion/transcription/PDF failures mark the call `ERROR` and stop it; a summarization failure is a soft failure (placeholder summary, `error` set, call continues to the PDF stage). Sentinels released by an upstream stage tell the downstream pool to drain.
* **Worker counts.** Conversion = CPU count - 1 (thread pool); transcription/summarization from `job_settings.RuntimeSelection` (`MAX_TRANSCRIPTION_CONCURRENT` / `MAX_SUMMARIZATION_CONCURRENT`, default 50, tuned to AssemblyAI's 100 new streams/min and Gemini Flash Tier 1's 300 RPM; local engines cap at `MAX_PARAKEET_CONCURRENT`=2 and 1 for Gemma); PDF = min(8, calls). All-local jobs (Parakeet + Gemma) run in two phases so both models never share unified memory.
* **Resumability.** Every stage transition is checkpointed via `job_store.update_call`. `_run_pipeline` routes each call into the queue matching its stored `CallStatus`, keyed by `original_path`, so pause/resume/crash recovery never re-spends API credits. **Resume the same job id; do not re-upload.** Pausing is cooperative: workers poll `job_store.get_job_stage` between items. `job_store.pause_orphaned_jobs()` runs at server startup so a job killed mid-run shows as paused, not running.
* **Validation up front.** `validate_runtime_selection` raises before any conversion if a selected cloud engine's key is missing or an engine is unavailable.
* **Engines are created once per run** and passed into the stages; the summarization engine stays loaded through case-report synthesis, then `unload()` frees local model memory.
* **Delivery assets** (`_stage_generate_delivery_assets`): search.html, viewer.html, guide.pdf, case-report.pdf written in parallel threads; a failed asset emits a warning and does not fail the job. `_stage_package` zips `output/` as `<SafeCaseName>/...`. `repackage_job` re-runs both after summary edits, re-creating the same summarization engine the job used.
* **Events** (`backend/events.py`): thread-safe bounded queue per job; `server.job_events` streams it as SSE with 30 s pings. The frontend reloads the job on every event and falls back to 3 s polling.
* **Speaker labels.** `_build_channel_labels`: channel 1 (left) = inmate by default; the job's `speaker_assignment` (`left_inmate` / `right_inmate`) swaps sides. The inmate label is the ICM inmate name, else the job's defendant name, else `INMATE`; the transcript cover uses the same fallback.

## Persistence (`backend/job_store.py`, `backend/db.py`)

SQLite at `jobs/jail_calls.db` with WAL mode and a 30 s busy timeout for concurrent writers. `job_store` maps rows to pydantic models generically from the table columns (add a column to `db.py` and it flows through; `_ensure_schema()` adds missing columns to older local databases at import). Prefer the narrow readers: `get_job_lite` / `list_jobs` skip the transcript JSON, `get_call` loads one call, `get_job` loads everything. Per-job files live in `jobs/<id>/` (`output/` is what gets zipped; `source-working/` holds repaired copies of the source audio so originals are never mutated).

## Transcription engines (`backend/transcription/`)

`get_engine(name)` returns an object with `async transcribe(audio_path, channel_labels) -> List[TranscriptTurn]`. Both engines return the same shape: ordered turns with `speaker`, `text`, `timestamp` (`[MM:SS]`), optional word-level `words` (ms), and `is_continuation` (same speaker as previous turn).

* **AssemblyAI (cloud):** stereo MP3 in, `multichannel=True`, submit-then-poll bounded by `ASSEMBLYAI_TRANSCRIPTION_TIMEOUT_SEC` (900 s; a stuck transcript raises `TimeoutError` and errors only that call). No `prompt` parameter (it caused spurious `[SPEAKER]` tags).
* **Parakeet (local):** splits stereo into two 16 kHz mono WAVs with ffmpeg, runs `bin/fluidaudiocli` (CoreML, gitignored ~7 MB; models download to `~/Library/Application Support/FluidAudio/Models` on first use) per channel, segments each word stream on 1.5 s gaps, and interleaves the segments by start time. Turn merging is simple; see `todo.md` item 5.

## Summarization engines (`backend/summarization/`)

Every engine subclasses `SummarizationEngine` (`base.py`); the pipeline and case report only use that interface. Three async operations, each returning its result plus `TokenUsage`:

* `detect_system_audio(turns, metadata)` — only when `system_audio_prepass = True` (Gemini). Runs before summarization so the summary sees a filtered transcript.
* `summarize_call(turns, prompt, metadata, *, detect_system_audio)` — returns `CallSummary` with either `structured` (a `SummaryResponse`) or `text` (raw model output). Inline-detecting engines (Gemma) fold detection into this one call and return `system_audio_markers`.
* `synthesize_case_report(CaseReportInputs)` — findings + outside-party identities as `CaseReportResponse`.

`pipeline._summarize_with_engine` is the only place that sequences these (prepass filter → summarize → post-filter for inline engines → normalize), and `delivery/case_report._run_synthesis` is the only caller of synthesis (3-attempt tenacity retry, 120 s per-attempt timeout). **Adding an engine = subclass the ABC, register it in `get_engine`, add it to the UI labels in `frontend/src/app/page.tsx`. Nothing in the pipeline or delivery code should branch on engine type.** `tests/test_summarize_stage.py` drives the stage with fake engines of both shapes.

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
* the viewer's printed pages (`viewer.html` embeds the line entries),
* every `Tr. page:line` cite in `search.html` and the case report,
* the `[Page:Line]` references the summarization prompt asks the model to cite (`summarization/base.build_transcript_text`), which later resolve back to lines, timestamps, and pull quotes via `resolve_line_ref_context` / `hydrate_review_cues`.

`MAX_LINE_CHARS = 62` is pinned there; `delivery/transcript_pdf.py` asserts at import that its ruled-corridor geometry still yields 62 columns. **Changing the wrapping rules shifts every citation in the product.** `create_pdf` asserts the rendered page count equals the emitted sheet count and refuses to deliver on mismatch.

## Delivery artifacts (`backend/delivery/`)

All PDFs are Jinja HTML rendered by headless Chromium via `pdf_render.render_pdf(html, *, paged=False)`: a thread-safe sync facade over async Playwright on a dedicated daemon thread (one shared browser, fresh context per render, semaphore-gated, single relaunch-and-retry on crash). Setup: `python -m playwright install chromium` once. `paged=True` injects the vendored Paged.js polyfill for `@page` margin boxes, running footers, and page counters (case report only); `paged=False` prints explicit fixed-size sheets (transcript, guide).

* **Transcript PDF** (`transcript_pdf.py`, `templates/transcript_pdf.html`): cover sheet, summary sheet(s), then legal-deposition transcript sheets (Courier Prime, line numbers, double-rule corridor, 25 lines/page). Every page is an explicit `8.5in x 11in` sheet with `overflow: hidden`; Python decides all pagination. Summary pagination is height-based (`summary_layout.py`: measured advance widths in `font_metrics.py` times a 1.06 safety factor); identity and brief summary stay on page 1, overflow pages carry notes only, cap 3 summary pages. If you edit the summary layout, update both the estimator and the CSS: an under-estimate clips content inside the fixed sheet. Visually verify with `pdftoppm` before calling it done.
* **Case report** (`case_report.py`, `templates/case_report.html`, Paged.js): parses every summary once, buckets by relevance, selects synthesis inputs (HIGH first, topping up from MEDIUM to `TARGET_FINDINGS_INPUT_COUNT`=10), runs one synthesis call (findings + identities), then renders at-a-glance metrics, an adaptive-granularity call timeline (day/week/month/year by span; inline SVG precomputed in `_build_timeline_svg`), top findings, HIGH call cards, MEDIUM rows, and caller stats with inferred identities. `synthesis_state` (`ok` / `no_input` / `synth_unavailable` / `parse_failed`) drives a differentiated empty state so a failed synthesis still yields a usable report. **Link portability:** hrefs are relative (`viewer.html?call=…&t=…`, `transcripts/<stem>.pdf`); Chromium bakes them as absolute `file:///tmp/...`, so `_rewrite_local_links_to_launch_actions` strips them back and converts to `/Launch` actions (`tests/test_case_report_links.py`). Paged.js does not replicate `position: fixed` per page: per-page chrome is styled on `.pagedjs_page`. The call card deliberately lacks `page-break-inside: avoid` (only `.cc-head`/`.cc-foot` do) so long cue tables flow across pages. Dry-run a job and eyeball the PDF after template edits.
* **Guide** (`guide_pdf.py`, `templates/guide.html`): 6 fixed sheets; screenshot PNGs in `assets/guide/` are synthetic ("State v. Marcus Reeves"), safe to ship; missing PNGs render as dashed placeholders, so keep them present. Copy is usage information only, no review-order advice (client preference). `tests/test_guide_layout.py` pins the page count and footer tokens.
* **Viewer** (`viewer.py`, `templates/viewer.html`, `{{PLACEHOLDER}}` substitution): self-contained; native `<audio>` behind a small `ws` shim (no WaveSurfer, no remote scripts: Web Audio cannot decode local files over `file://`; pinned by `tests/test_delivery_html.py`). Left call rail, main transport + printed-transcript pane, right analysis rail with clickable cues; both rails collapse (`localStorage` `jcs_calls_collapsed` / `jcs_summary_collapsed`). Present mode (`P`, Esc exits) shows only transport + transcript sheets. Deep links `?call=…&t=…` seek and reveal the line while staying paused. Words are clickable `span.word-ts` with `data-ws`/`data-we`; search mode shows all pages and hides non-matching ones. Lint errors inside the template's JS are expected (placeholders).
* **Search page** (`search_html.py`, `templates/search.html`, `__PLACEHOLDER__` substitution): the delivery's home page: masthead, sticky filter bar (full text, date range, number, relevance), one row per call with relevance marker, expandable summary/cues/full transcript with match stepping, Viewer/PDF buttons. All data inline JSON; fonts embedded as base64.
* **Zip layout is flat:** `viewer.html`, `search.html`, `guide.pdf`, `case-report.pdf` at the root beside `audio/`, `transcripts/`, `transcripts-no-summary/`. No `viewer/` subdirectory; the viewer's audio URL is `audio/<mp3>`. Per-call filenames come only from `models.call_stem(index, filename)`.

**Design system ("Record"):** paper `#F5F2EA` / cream `#FBF9F3` / sheet white; ink `#16140F`, body `#45413A`, muted `#807A6E`; rules `rgba(22,20,15,.18)` and `.09`. One signal color, crimson `#A8271E` (text `#871F18`), reserved for HIGH relevance, review-cue timestamps, chart peaks, and the playhead; MEDIUM is a hollow ochre square (`#8F6400`), LOW hollow gray (`#76796E`). Type: Fraunces (display, optical-size axis) for titles and big numerals, Public Sans for UI/body, IBM Plex Mono for timestamps/numbers/cites, Courier Prime metric-locked to transcript sheets. `fonts.py` serves `@font-face` as `file://` URIs for PDFs and base64 data URIs for the HTML pages (all SIL OFL; licenses beside the files). Headings are flat labels ("At a Glance"); no explanatory ledes inside artifacts; header bands carry only the document/case label (the "Privileged & Confidential" line was removed from headers at client request and survives only in footers). Charts are hand-rolled inline SVG.

## Frontend

Next.js app under `frontend/`, proxying `/api/*` to the backend (`next.config.js`). `lib/api.ts` is the typed client and the only place URLs live; page components handle UI state only. Tailwind 4 uses `@import "tailwindcss"` and `@tailwindcss/postcss`; do not restore Tailwind 3 directives. `next.config.js` pins `turbopack.root` so Next does not infer the home directory as the workspace. Verify with `npx tsc --noEmit` and `npx next build` in `frontend/`.

## Testing

Run with the pyenv Python (system `python3` is stale).

1. **Unit / regression suite:** `python -m pytest tests/` (fast, no network, no keys; Chromium is used for the PDF tests).
   * `test_summarize_stage.py` — the summarize stage with fake JSON and text engines (filter order, note removal, token accounting)
   * `test_text_protocol.py` — Gemma block parsers
   * `test_transcript_summary_layout.py` — summary pagination, page budget, note curation
   * `test_quote_line_refs.py` — line-ref → quote hydration
   * `test_case_report_links.py` — `/Launch` link rewriting
   * `test_delivery_html.py` — script-safe JSON embedding; no remote scripts in viewer
   * `test_guide_layout.py` — guide page count + footer clearance
   * `test_pdf_render.py` — the Chromium facade
   * `test_pipeline_audio_regressions.py` — conversion works on a copy, discovery, labels
2. **Synthetic delivery package:** `python tests/make_test_package.py` (`--calls N`, `--zip`, `--no-audio`) builds a complete delivery at `test-output/REEVES_TEST_PACKAGE/` through the real delivery code with a stub synthesis engine and no job database. **Build it and review every artifact by hand after any significant change** to the delivery code or templates. A useful trick for refactors: build it before and after, and diff the HTML files and the extracted PDF text.
3. **End to end without API spend:** create a job with `skip_summary` and the Parakeet engine on one recording; it exercises conversion, transcription, both PDFs, every delivery asset, and the zip.

## Parked work

See `todo.md`: offline preamble stripping, Parakeet turn merging, revisiting local-only mode, plus older product ideas.
