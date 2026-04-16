# AGENTS.md - Jail Call Service Context

This document provides codebase context for future AI coding agents working on the `jail-call-service`.

## Repository
**Standalone repo:** [https://github.com/nathanjeichert/jail-call-service](https://github.com/nathanjeichert/jail-call-service)

This project was originally a branch within the TranscribeAlpha monorepo but has been split into its own independent repository.

* **Target Audience:** Used internally/locally by the operator (Nathan) rather than acting as a SaaS for tech-illiterate lawyers.
* **Volume:** Designed to process thousands of G.729 WAV files (e.g. batches of 2,000+ files) per job.
* **Architecture differences:** It uses a local SQLite database (`backend/db.py`) to avoid memory bloat during massive parallel processing instead of standard JSON/flat state.

## Tech Stack & Architecture
* **Backend:** FastAPI, Python, ffmpeg-python.
* **Transcription:** Dual-engine — AssemblyAI (cloud) or Parakeet TDT 0.6b v2 via FluidAudio CoreML (local, runs on Apple Neural Engine). Selectable per-job from the UI.
* **Summarization:** Dual-engine — Gemini Flash (cloud) or Gemma 4 E2B via MLX (local, 4-bit quantized, ~3.6 GB RAM). Selectable per-job from the UI.
* **Frontend:** Next.js (Tailwind + TypeScript).
* **Delivery Payload:** The software produces a `.zip` artifact containing:
  - Repaired and converted MP3 audio files (`audio/`).
  - A spreadsheet index (`call-index.xlsx`) linking to transcript/viewer payloads.
  - Formatted transcript PDFs (`transcripts/` and `transcripts-no-summary/`).
  - A responsive local HTML viewer dashboard (`viewer.html`) using WaveSurfer.js.
  - A dossier-style HTML search landing page (`search.html`) with a sortable, filterable table and per-row expanding detail panels.
  - A user guide PDF (`guide.pdf`) explaining how to use the delivery package.
  - A case-level report PDF (`case-report.pdf`) with at-a-glance metrics, timeline, AI-synthesized top findings, outside-party identity inference, and high/medium relevance call cards.

## Critical Technical Details
1. **Parallelization:** Conversion runs on a `ThreadPoolExecutor` sized to CPU count; AssemblyAI and Gemini calls are driven by asyncio worker pools capped by `MAX_TRANSCRIPTION_CONCURRENT` (default **50**) and `MAX_SUMMARIZATION_CONCURRENT` (default **50**). These defaults are tuned to keep burst traffic safely under AssemblyAI's pay-as-you-go "100 new streams/minute" rate and Gemini Flash Tier 1's 300 RPM with headroom for retries; bump them via `.env` only if you've verified your account tier. When both transcription and summarization use local engines (Parakeet + Gemma), the pipeline serializes the stages to avoid competing for GPU/unified memory.
2. **Resiliency & cost control:**
   - SQLite (`backend/db.py`, `db_models.py`, `job_store.py`) checkpoints every stage transition per call so that a crash or pause resumes without re-spending API credits. Resumption is keyed by `original_path` and routes each call back to the correct worker queue based on its stored `CallStatus` — **resume the same job id; do not re-upload, since that creates a new job and re-pays for everything**.
   - `_run_pipeline` validates required API keys for the selected cloud engines at job start and raises immediately if they're missing, so a bad `.env` fails *before* the conversion stage spends compute.
   - AssemblyAI's polling loop is bounded by `ASSEMBLYAI_TRANSCRIPTION_TIMEOUT_SEC` (default **900s / 15min**). A stuck transcript raises `TimeoutError`, lands in the Errors sheet, and does not stall the batch.
   - Gemini summarization failures are not silent: the call still finishes with a `"Summary unavailable for this call."` fallback, but the `error` field is set so the call appears in the Excel Errors sheet as a partial failure.
   - The case-report synthesis call (routed through whichever summarization engine the job is using) is wrapped in tenacity retry (3 attempts, exponential backoff) plus a 120-second per-attempt timeout via `concurrent.futures`.
   - The final completion log breaks out clean / partial / errored call counts.
3. **Database Concurrency:** SQLite is configured with WAL mode and a 30-second busy timeout to handle concurrent writes from parallel pipeline stages (conversion threads, async transcription/summarization).
4. **Transcription Format:** Both engines produce the same `List[TranscriptTurn]` output. **Channel 1** = Inmate (Defendant), **Channel 2** = Outside Party.
5. **Export Deep Linking:** When generating PDFs and Excel spreadsheets, the app creates `viewer.html?call=[file]&t=[xx:xx]` links directly pointing to specific timestamps within the self-contained static HTML viewer. `viewer.html` lives at the root of the delivery zip next to `transcripts/`, `audio/`, `call-index.xlsx`, etc., so every one of those links is a relative path (no absolute `file://` URL ever gets baked into an exported PDF).
6. **Canonical call stem:** `backend/models.py` exposes `call_stem(index, filename)` — the single source of truth for how per-call output files (transcript PDFs, per-call audio, viewer deep-links) are named. `pipeline.py`, `case_report.py`, and `search_html.py` all call it so filenames stay in lockstep. Any new consumer that builds a per-call filename must use this helper, not a local copy.
7. **PDF Architecture:** The delivery contains four PDF artifacts, all ultimately rendered through Jinja + WeasyPrint templates that share the same teal/ink/Georgia + Avenir design language:
   - **Per-call transcript PDF** (`backend/transcript_formatting.py`, `pdf_cover_template.html`) — hybrid document: title and Gemini summary pages are WeasyPrint/HTML/CSS; transcript body pages are ReportLab-generated legal-deposition style with Courier text, line numbers, and page numbers. The summary page parses Gemini's structured sections (`RELEVANCE`, `NOTES`, `IDENTITY OF OUTSIDE PARTY`, `BRIEF SUMMARY`). `NOTES: NONE` renders as a polished "No relevant information found" panel. Notes display audio timestamps plus transcript page:line cites, with a single legend explaining `[MM:SS]` and `Page:Line`. **HIGH-relevance summary spillover:** HIGH calls with more than `SUMMARY_PAGE1_CUE_CAP` (=7) review cues automatically spill the excess onto a dedicated "Notes, continued" second summary page (capped at `SUMMARY_PAGE2_CUE_CAP` = 14 more), driven by a Python split in `_render_cover_pages`. The Identity of Outside Party and Brief Summary blocks always render on page 1. Page 1 shows a subtle "+N more on next page →" pill in the Notes section heading; the continuation page mirrors it with "← continued from page 02" and renumbers the notes ("Notes 8–14"). The page-1 footer becomes `02 / 03` when a continuation exists. MEDIUM/LOW calls keep the existing single-page layout — spillover is HIGH-only because MEDIUM/LOW almost never exceed the cap in practice.
   - **User guide PDF** (`backend/guide_pdf.py`, `guide_template.html`) — 7-page how-to-use document (cover, package tree, viewer, search, Excel, AI analysis, important notes). Includes three screenshot pages that pull PNGs from `backend/guide_assets/`; if the PNGs are missing the template falls back to visible dashed placeholder boxes, so those assets must always be present.
   - **Case report PDF** — see "Case Report Architecture" below.
8. **WeasyPrint gradient syntax gotcha:** WeasyPrint's CSS parser does NOT accept the modern multi-position color-stop syntax for `linear-gradient()` (`linear-gradient(145deg, transparent 0 45%, rgba(...) 45% 51%, transparent 51%)`). It silently drops the rule, producing a PDF without the intended accent stripe and emitting a warning. Always write gradients in the long-form "one position per stop" syntax instead: `linear-gradient(145deg, transparent 0%, transparent 45%, rgba(...) 45%, rgba(...) 51%, transparent 51%, transparent 100%)`. Applies to every teal diagonal stripe on `pdf_cover_template.html`, `guide_template.html`, and `case_report_template.html`.
9. **Delivery zip layout (flat):** All top-level client-facing HTML and PDF artifacts sit at the delivery root — `viewer.html`, `search.html`, `call-index.xlsx`, `guide.pdf`, `case-report.pdf` — next to the `audio/`, `transcripts/`, and `transcripts-no-summary/` directories. There is NO `viewer/` subdirectory — the viewer ships as a single `viewer.html` file. Consequences: (a) the viewer template's audio `<audio>`/WaveSurfer URL uses `audio/<mp3>` (not `../audio/...`), (b) every deep-link from Excel, search.html, and case-report.pdf is the relative path `viewer.html?call=[file]&t=[xx:xx]`, (c) `case_report.generate_case_report_pdf` omits `base_url=` from its WeasyPrint call so those relative hrefs are written into the PDF link annotations verbatim rather than being resolved against the developer's local `backend/` path. Do not reintroduce a `viewer/` subdirectory or set `base_url=` in the case-report renderer — it breaks portability of the delivery across machines.

If editing the `template.html` for the viewer, note that template parameters (like `{{CALLS_JSON}}`) are pre-populated by Python string manipulation, hence why TypeScript/Javascript linting will report errors inside the HTML syntax. Ignore these syntactical lint warnings during development.

If editing `backend/pdf_cover_template.html`, visually verify rendered PDF pages with Poppler (`pdftoppm`) before considering the work done. Do not commit one-off PDF mockup artifacts, local preview PDFs, or scratch scripts used only to inspect layout.

## Transcription Engine Architecture

The transcription system is modular, located in `backend/transcription/`:

```
backend/transcription/
  __init__.py              # get_engine() factory, AVAILABLE_ENGINES list
  base.py                  # TranscriptionEngine protocol + shared utils
  assemblyai_engine.py     # Cloud: AssemblyAI multichannel API
  parakeet_engine.py       # Local: FluidAudio CoreML via fluidaudiocli subprocess
```

**AssemblyAI (cloud):** Sends stereo MP3 directly; AssemblyAI handles multichannel separation. Async submit-then-poll pattern. Requires API key.

**Parakeet (local):** Splits stereo audio into two mono 16 kHz WAV channels via ffmpeg, transcribes each with `fluidaudiocli` (CoreML on Apple Neural Engine), then merges the two word streams into interleaved speaker-attributed turns based on timestamps. The `fluidaudiocli` binary lives at `bin/fluidaudiocli` (gitignored, ~7MB). Concurrency is capped at 1 to stay within 8GB RAM.

Both engines return identical `List[TranscriptTurn]` — the rest of the pipeline (summarization, PDF generation, viewer) is engine-agnostic.

**AssemblyAI config notes:** No `prompt` parameter is sent — it caused the model to insert spurious `[SPEAKER]` tags into transcript text. Only `speech_models`, `format_text`, `multichannel`, and `temperature` are set.

## Summarization Engine Architecture

The summarization system follows the same modular pattern, located in `backend/summarization/`:

```
backend/summarization/
  __init__.py              # get_engine() factory, AVAILABLE_ENGINES list
  base.py                  # Shared utils (build_transcript_text, build_full_prompt)
  gemini_engine.py         # Cloud: Gemini Flash API
  gemma_engine.py          # Local: Gemma 4 E2B via mlx-lm on Apple Silicon
```

**Gemini (cloud):** Calls the Gemini Flash API. Requires API key. Concurrency controlled by `MAX_SUMMARIZATION_CONCURRENT`.

**Gemma (local):** Runs Gemma 4 E2B (4-bit quantized) via mlx-lm for Metal-accelerated inference. Lazy-loads the model on first call with a warm-up pass to trigger Metal JIT compilation. Concurrency capped at 1 to stay within 8GB RAM. The engine instance is created once per pipeline run and reused across all calls to avoid repeated model loading.

**Default Gemini summary shape:** The production prompt in `backend/config.py` asks for:
- `RELEVANCE: HIGH / MEDIUM / LOW`
- `NOTES:` timestamped attorney-relevant moments only, or exactly `NOTES: NONE` when there is nothing an attorney would plausibly need to know.
- `IDENTITY OF OUTSIDE PARTY:` only when the call itself supports an identity or relationship inference.
- `BRIEF SUMMARY:` one to two sentences.

Do not make Gemini add notes merely to orient the reader to routine personal conversation. Notes should be reserved for content that may matter to case review, charges/evidence, confinement, allegedly criminal conduct, or another substantive attorney-review reason.

## System Audio Filtering

Automated telecom messages (IVR prompts, time warnings, provider sign-offs) are detected and filtered via `backend/system_audio.py`. The approach piggybacks on the Gemini summarization call — the prompt is extended to ask Gemini to also identify automated turns by index, returned as a `SYSTEM_AUDIO: [...]` JSON line appended to the summary response.

**Job-level `auto_message_mode` setting (UI toggle):**
- `None` / "Keep": No filtering, automated messages left as-is.
- `"exclude"`: System audio turns/words are removed entirely from the transcript.
- `"label"`: System audio turns are relabeled with speaker `"AUTOMATED MESSAGE"`. Consecutive automated turns are deduplicated (both channels carry the same audio) and merged into single turns.

For partial turns (real speech + system text in the same turn, e.g. "...they're playing us— You have 1 minute remaining."), the system text substring is split out — either stripped (exclude) or broken into a separate AUTOMATED MESSAGE turn (label).

When system-audio detection is enabled, `backend/pipeline.py` also removes any `NOTES` bullets that match identified automated telecom messages before generating PDFs. This keeps call-setup prompts and time warnings out of the summary product while still preserving them as `AUTOMATED MESSAGE` transcript turns in `"label"` mode.

**Important:** System audio filtering only runs when summarization is active (`skip_summary=False`). Both the Gemini (cloud) and Gemma (local) summarization engines receive the `SYSTEM_AUDIO` detection tail, so filtering works regardless of which engine is selected. Test runs with dummy summaries get no filtering.

## Case Report Architecture

`backend/case_report.py` + `case_report_template.html` generate `case-report.pdf`, a case-level dossier aggregating per-call analysis. It runs at the end of the pipeline inside `_stage_generate_indexes` and is packaged into the delivery ZIP automatically.

Pipeline:
1. Parse every call's summary once through `_parse_summary_sections` and reuse the result everywhere downstream.
2. Bucket calls by `RELEVANCE` (HIGH / MEDIUM / LOW / UNKNOWN).
3. Select a synthesis input set — HIGH calls first, topping up from MEDIUM if there are fewer than `TARGET_FINDINGS_INPUT_COUNT` (10) HIGH calls.
4. Issue **one** extra synthesis call, routed through the same summarization engine the per-call summaries used (Gemini cloud or Gemma local — fully interchangeable). This call performs BOTH "top findings" synthesis AND per-outside-number "identity inference" in a single prompt, using a block-delimited output format (`FINDING_START…FINDING_END`, `IDENTITY_START…IDENTITY_END`), and is wrapped in a 3-attempt tenacity retry and a 120-second per-attempt `concurrent.futures` timeout.
5. Render a multi-section PDF: at-a-glance metrics, activity timeline, relevance distribution, top findings, high-relevance call cards, medium-relevance compact rows, and frequent-caller stats (including AI-inferred identities).

**Graceful degradation:** The template reads a `synthesis_state` variable (`ok` / `no_input` / `gemini_unavailable` / `parse_failed`) and renders a differentiated empty-state panel in the Top Findings section for each case, so a failed synthesis still produces a usable report.

**Voice contract for the synthesis prompt:** `CASE_REPORT_SYNTHESIS_PROMPT` in `case_report.py` explicitly instructs Gemini that (a) the audience is a legal professional who may be DEFENSE counsel OR PROSECUTION, so the voice must be neutral, objective, and reader-agnostic, (b) all findings must be written in third-person neutral past-tense factual prose, and (c) the second person ("you"/"your") is forbidden — even when narrating the defendant's side of the call. The defendant is NEVER "you". Before loosening those rules, check the prior failure mode: without them Gemini slipped into "the outside party informs you that…" because the transcript has the defendant speaking first-person and the model naturally adopted the defendant's POV.

**Link portability:** All hrefs rendered into the case report (viewer deep-links, transcript PDF links) are relative paths like `viewer.html?call=...` and `transcripts/<stem>.pdf`. The `HTML(string=html_str)` call in `generate_case_report_pdf` intentionally omits `base_url=` so WeasyPrint writes those relative URIs into the PDF's link annotations verbatim — setting `base_url` to the template's parent directory (the default footgun) causes the developer's absolute `file:///Users/.../backend/` path to be baked into the annotations, which then fail on every other machine. Verified via inspecting the PDF's `/URI` entries after render.

**When editing `case_report_template.html`:** do a dry-run job end-to-end (even a small one) and spot-check the rendered PDF — its multi-section WeasyPrint layout uses absolute positioning and @page margins that can regress silently. Note that `.call-card` deliberately does NOT carry `page-break-inside: avoid`; that rule is scoped to `.call-card-head` only, so long cue tables flow cleanly across page breaks instead of orphaning the entire chapter header onto its own near-empty page.

## Guide Assets

`backend/guide_assets/` must contain `viewer_screenshot.png`, `search_screenshot.png`, and `excel_screenshot.png` — the three screenshots embedded in `guide.pdf`. These are **synthetic** samples built from a fake "State v. Marcus Reeves" dataset, not from any real client job, so they can ship with every delivery without leaking case data. If you need to regenerate them (e.g., after a UI refresh):
- Build samples by calling `render_viewer`, `generate_search_html`, and `generate_excel` against synthetic `CallResult` objects.
- Screenshot the viewer and search HTML via Chrome headless at `1600×1040`.
- For the Excel screenshot, prefer rendering an HTML facsimile of the same column layout and styling (navy header, cream alternating rows) because macOS `qlmanage -t` crops the real xlsx at ~3 rows due to tall auto-sized summary-cell heights.
- Keep the scratch scripts out of the repo.

## Viewer Architecture

The HTML viewer (`backend/viewer/template.html`) is a self-contained static page with all call data embedded as JSON. It supports two audio backends:
- **WaveSurfer.js** (default when served via HTTP) — waveform visualization.
- **Native `<audio>` element** (automatic when opened via `file://` protocol, or forced with `?native_audio=1`) — lightweight shim that avoids WaveSurfer's CORS issues with local files.

**Word-level timestamps:** Each word in the transcript is rendered as a clickable `<span class="word-ts">` with `data-ws`/`data-we` attributes (start/end in seconds). Clicking a word seeks to that exact timestamp. During playback, the current word is highlighted with `active-word` class. When search is active, the view falls back to plain text rendering with `<mark>` highlights.

**Collapsible AI Summary pane:** The right-hand summary pane can be collapsed to a 36px vertical rail via a collapse button; state is persisted in `localStorage` under `jcs_summary_collapsed`. When collapsed, the header text is rotated with `writing-mode: vertical-rl` + `transform: rotate(180deg)`.

**Search mode:** Typing in the search box adds a `searching` class to `.trans-scroll`, which shows every transcript page at once and hides pages with no match (`.trans-page.no-match`). The pager switches from `Page N / M` to `N pages with matches` while search is active.

## Search Page Architecture

`backend/search_html.py` emits `search.html`, a single self-contained page that serves as the client-facing "home" of the delivery. It's a dossier-style sortable, filterable table (not a simple search box) — one row per call with relevance chip, inline summary, per-row expanding detail panel containing the full set of review cues and the full transcript, and deep-link buttons to the viewer and the transcript PDF. All data is embedded in an inline `<script>` JSON blob; there are no external dependencies.

Per-call `line_cite` (`Tr. 4:12`-style) values are computed Python-side at generation time via `compute_line_entries`, matching what the transcript PDF would show.
