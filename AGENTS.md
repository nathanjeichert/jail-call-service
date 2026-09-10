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
* **Frontend:** Next.js 16 / React 19 (Turbopack), Tailwind CSS 4, TypeScript 6. Node.js must be >=20.9.0 because Next 16 enforces that engine floor.
* **Delivery Payload:** The software produces a `.zip` artifact containing:
  - Repaired and converted MP3 audio files (`audio/`).
  - Formatted transcript PDFs (`transcripts/` and `transcripts-no-summary/`).
  - A responsive local HTML viewer dashboard (`viewer.html`) using WaveSurfer.js.
  - A dossier-style HTML search landing page (`search.html`) that also serves as the primary searchable call index, with a sortable, filterable table and per-row expanding detail panels.
  - A user guide PDF (`guide.pdf`) explaining how to use the delivery package.
  - A case-level report PDF (`case-report.pdf`) with at-a-glance metrics, timeline, AI-synthesized top findings, outside-party identity inference, and high/medium relevance call cards.

## Critical Technical Details
1. **Parallelization:** Conversion runs on a `ThreadPoolExecutor` sized to CPU count; AssemblyAI and Gemini calls are driven by asyncio worker pools capped by `MAX_TRANSCRIPTION_CONCURRENT` (default **50**) and `MAX_SUMMARIZATION_CONCURRENT` (default **50**). These defaults are tuned to keep burst traffic safely under AssemblyAI's pay-as-you-go "100 new streams/minute" rate and Gemini Flash Tier 1's 300 RPM with headroom for retries; bump them via `.env` only if you've verified your account tier. When both transcription and summarization use local engines (Parakeet + Gemma), the pipeline serializes the stages to avoid competing for GPU/unified memory.
2. **Resiliency & cost control:**
   - SQLite (`backend/db.py`, `job_store.py`) checkpoints every stage transition per call so that a crash or pause resumes without re-spending API credits. Resumption is keyed by `original_path` and routes each call back to the correct worker queue based on its stored `CallStatus` — **resume the same job id; do not re-upload, since that creates a new job and re-pays for everything**.
   - `_run_pipeline` validates required API keys for the selected cloud engines at job start and raises immediately if they're missing, so a bad `.env` fails *before* the conversion stage spends compute.
   - AssemblyAI's polling loop is bounded by `ASSEMBLYAI_TRANSCRIPTION_TIMEOUT_SEC` (default **900s / 15min**). A stuck transcript raises `TimeoutError`, marks the call as errored, and does not stall the batch.
   - Gemini summarization failures are not silent: the call still finishes with a `"Summary unavailable for this call."` fallback, but the `error` field is set so the call is counted as a partial failure in downstream reporting and completion logs.
   - The case-report synthesis call (routed through whichever summarization engine the job is using) is wrapped in tenacity retry (3 attempts, exponential backoff) plus a 120-second per-attempt timeout via `concurrent.futures`.
   - The final completion log breaks out clean / partial / errored call counts.
3. **Database Concurrency:** SQLite is configured with WAL mode and a 30-second busy timeout to handle concurrent writes from parallel pipeline stages (conversion threads, async transcription/summarization).
4. **Transcription Format:** Both engines produce the same `List[TranscriptTurn]` output. **Channel 1** = Inmate (Defendant), **Channel 2** = Outside Party.
5. **Export Deep Linking:** When generating PDFs and HTML review surfaces, the app creates `viewer.html?call=[file]&t=[xx:xx]` links directly pointing to specific timestamps within the self-contained static HTML viewer. `viewer.html` lives at the root of the delivery zip next to `transcripts/`, `audio/`, `search.html`, etc., so every one of those links is a relative path (no absolute `file://` URL ever gets baked into an exported PDF).
6. **Canonical call stem:** `backend/models.py` exposes `call_stem(index, filename)` — the single source of truth for how per-call output files (transcript PDFs, per-call audio, viewer deep-links) are named. `pipeline.py`, `case_report.py`, and `search_html.py` all call it so filenames stay in lockstep. Any new consumer that builds a per-call filename must use this helper, not a local copy.
7. **PDF Architecture:** The delivery contains four PDF artifacts, all rendered as Jinja HTML templates printed by **headless Chromium** via `backend/delivery/pdf_render.py` (`render_pdf(html, *, paged=False) -> bytes`). That module is a thread-safe sync facade over async Playwright running on a dedicated daemon thread: one shared browser, a fresh context per render, semaphore-gated concurrency, single relaunch-and-retry on browser crash. Setup requires `python -m playwright install chromium` once per machine (a missing browser raises a RuntimeError with that exact remedy). `paged=True` injects the vendored Paged.js polyfill (`backend/delivery/assets/vendor/paged.polyfill.js`) to support `@page` margin boxes, `string-set` running footers, and page counters for flowing documents (the case report); `paged=False` prints explicit fixed-size sheets as-is (transcript PDFs, guide). All client-facing artifacts (PDF and HTML) share the **"Record" design system** — see "Design System" below:
   - **Per-call transcript PDF** (`backend/delivery/transcript_pdf.py`, `delivery/templates/transcript_pdf.html`) — ONE template, ONE render: title sheet, summary sheet(s), then legal-deposition transcript sheets (Courier Prime from `backend/delivery/assets/fonts/`, line numbers, ruled corridor, 25 lines/page). Every page is an explicit `8.5in × 11in` sheet div with `overflow: hidden`, so Chromium never makes a pagination decision — **Python is the layout source of truth**. `transcript_layout.compute_line_entries()` wraps text to `MAX_LINE_CHARS` (asserted == 62 at import; every page:line citation in search.html/viewer.html/summary cues depends on it) and `create_pdf()` asserts the rendered page count equals the emitted sheet count, raising RuntimeError on mismatch. The summary page parses Gemini's structured sections (`RELEVANCE`, `NOTES`, `IDENTITY OF OUTSIDE PARTY`, `BRIEF SUMMARY`). `NOTES: NONE` renders as a polished "No relevant information found" panel. Summary pagination is **height-based**: `delivery/summary_layout.paginate_structured_summary()` estimates cue/context heights using `backend/delivery/font_metrics.py` (embedded standard-14 AFM width tables × a 1.06 `SAFETY_FACTOR`, since Chromium renders with slightly wider system fonts) and packs page 1 plus overflow pages (cap currently 3 summary pages, enforced by `summaries`). Identity of Outside Party and Brief Summary remain on page 1; overflow pages render notes only. If editing the summary layout, validate both the Python estimator and the HTML/CSS — an under-estimate clips content inside the fixed-height sheet.
   - **User guide PDF** (`backend/delivery/guide_pdf.py`, `delivery/templates/guide.html`) — 6-page how-to-use document (cover, delivery contents, viewer, search, case report, AI analysis), rendered as explicit fixed-size sheets (`paged=False`). Tutorial copy is technical usage information only — no "suggested order of review" or other reading-order advice (client preference). Includes the viewer/search screenshot pages that pull PNGs from `backend/delivery/assets/guide/`; if the PNGs are missing the template falls back to visible dashed placeholder boxes, so those assets must always be present. `tests/test_guide_layout.py` pins the 6-page count and the footer token shape ("USER GUIDE" · section · page number, in their CSS-uppercased rendered form).
   - **Case report PDF** — see "Case Report Architecture" below.
8. **Paged.js layout gotchas (case report & guide):** Chromium accepts all modern CSS, but documents rendered with `paged=True` are paginated by Paged.js, which does NOT replicate `position: fixed` elements onto every generated page the way WeasyPrint did. Per-page decorations (the black left spine, footer rules) are therefore styled on the generated page boxes (`.pagedjs_page` / `.pagedjs_pagebox`) instead of fixed-position divs — keep it that way, and visually verify pages 2+ whenever touching them. Section-varying footers use `string-set`/`@bottom-left` and `counter(page, decimal-leading-zero)`/`@bottom-right`, with `@page :first` suppressing the footer on covers — all polyfilled by Paged.js, so confirm footers still render after template edits.
9. **Delivery zip layout (flat):** All top-level client-facing HTML and PDF artifacts sit at the delivery root — `viewer.html`, `search.html`, `guide.pdf`, `case-report.pdf` — next to the `audio/`, `transcripts/`, and `transcripts-no-summary/` directories. There is NO `viewer/` subdirectory — the viewer ships as a single `viewer.html` file. Consequences: (a) the viewer template's audio `<audio>`/WaveSurfer URL uses `audio/<mp3>` (not `../audio/...`), (b) every deep-link from `search.html` and `case-report.pdf` is the relative path `viewer.html?call=[file]&t=[xx:xx]`, (c) Chromium resolves relative hrefs against its temp-file render URL, so case-report link annotations come back as absolute `file:///tmp/...` URIs — `case_report._rewrite_local_links_to_launch_actions` uses `_extract_local_target` to strip them back to the relative `viewer.html?...` / `transcripts/*.pdf` form (percent-encoding preserved verbatim) and converts them to `/Launch` actions so macOS opens local files from the extracted delivery root. Do not reintroduce a `viewer/` subdirectory, and after touching the rewriter verify no `file://` URI survives anywhere in the rendered PDF — an absolute path baked into an annotation breaks portability of the delivery across machines.

## Design System ("Record")

All client-facing artifacts share one design language, introduced June 2026:

* **Tokens:** paper `#F5F2EA` / cream `#FBF9F3` / sheet white; ink `#16140F`, `#45413A` (body), `#807A6E` (muted); rules at `rgba(22,20,15,.18)` and `.09`. **One signal color:** crimson `#A8271E` (text form `#871F18`) is reserved exclusively for HIGH relevance, review-cue timestamps, chart peaks, and the live playhead. MEDIUM renders as a hollow ochre square (`#8F6400`), LOW as a hollow gray square (`#76796E`) — the `.rv` marker atom in every template. Buttons and actions are ink-on-paper; nothing else carries color.
* **Type:** Fraunces (variable, with the optical-size axis — display titles set `font-variation-settings: "opsz"` 70–110, serif body text 18) for document titles and large numerals; Public Sans (variable) for UI, data, and body copy; IBM Plex Mono for timestamps, phone numbers, and page:line cites only. Courier Prime remains metric-locked to the transcript sheets.
* **Fonts module:** `backend/delivery/fonts.py` serves `@font-face` CSS two ways — `pdf_font_css()` (file:// URIs into `backend/delivery/assets/fonts/`) for PDF templates, `embedded_font_css()` (base64 data URIs) for the self-contained HTML artifacts. All families are SIL OFL; license texts live next to the font files. `backend/delivery/font_metrics.py` carries measured advance-width tables extracted from these exact font files (regenerate with fontTools if the files change) — the transcript summary-page height estimator depends on them.
* **Copy voice:** headings are flat work-product labels ("At a Glance", "Top Findings", "Caller Statistics"); no explanatory ledes, methodology blurbs, or reader-aimed how-to text inside the artifacts (client preference — the guide is the only tutorial surface). Prefer colons over em dashes in template copy and label–value separators; the summary/synthesis prompts also instruct the models to avoid em dashes in generated prose. As of June 2026 the page-top header bands carry only the document/case label — the "Privileged & Confidential · Attorney Work Product" text was removed from every header band in every artifact (client request, "for now"); the confidentiality line survives only in the case-report `@bottom-center` page footer and the transcript sheet footers. AI disclosure is a single footer line per document.
* **Charts:** hand-rolled inline SVG only (geometry precomputed in Python — see `_build_timeline_svg` in `case_report.py`); no chart libraries, nothing canvas-based (prints poorly under Paged.js).

If editing `delivery/templates/viewer.html`, note that template parameters (like `{{CALLS_JSON}}`) are pre-populated by Python string manipulation, hence why TypeScript/Javascript linting will report errors inside the HTML syntax. Ignore these syntactical lint warnings during development.

If editing `backend/delivery/templates/transcript_pdf.html`, visually verify rendered PDF pages with Poppler (`pdftoppm`) before considering the work done. Do not commit one-off PDF mockup artifacts, local preview PDFs, or scratch scripts used only to inspect layout.

**Frontend dependency notes:** Tailwind 4 uses `@import "tailwindcss";` in `frontend/src/app/globals.css` and the split PostCSS plugin `@tailwindcss/postcss` in `frontend/postcss.config.js`; do not restore the Tailwind 3 `@tailwind base/components/utilities` directives. `frontend/next.config.js` sets `turbopack.root` to the frontend directory so Next does not infer the parent home directory when other lockfiles exist outside this repo.

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
For Gemini 3 Flash, the app currently uses `thinkingLevel="low"` for the automated-message detection pass and `thinkingLevel="medium"` for both the per-call summary pass and the case-report synthesis pass.

**Gemma (local):** Runs Gemma 4 E2B (4-bit quantized) via mlx-lm for Metal-accelerated inference. Lazy-loads the model on first call with a warm-up pass to trigger Metal JIT compilation. Concurrency capped at 1 to stay within 8GB RAM. The engine instance is created once per pipeline run and reused across all calls to avoid repeated model loading.

**Default Gemini summary shape:** The production prompt in `backend/config.py` asks for:
- `RELEVANCE: HIGH / MEDIUM / LOW`
- `NOTES:` timestamped attorney-relevant moments only, or exactly `NOTES: NONE` when there is nothing an attorney would plausibly need to know.
- `IDENTITY OF OUTSIDE PARTY:` only when the call itself supports an identity or relationship inference.
- `BRIEF SUMMARY:` one to two sentences.

**Gemini structured-output architecture:** For Gemini jobs, the app no longer trusts model-authored quote text, timestamps, or speaker labels inside `NOTES`. Instead:
- The model returns JSON for all three Gemini call types: automated-message detection, per-call summary, and case-report synthesis.
- The per-call summary JSON note items contain only `line_ref` plus `reason`.
- The app derives the rendered `[MM:SS]`, `SPEAKER`, and pull quote from the cited transcript lines at render time via `compute_line_entries` / `hydrate_review_cues`.
- Search, transcript PDFs, and case-report call cards all hydrate note quotes from those same transcript line refs so the visible excerpt is always transcript-derived.
- Before persistence, Gemini summaries are normalized through `backend/summaries.py`: leaked preambles are stripped, note counts are capped by relevance tier, duplicate/weak note entries are trimmed, identity/brief-summary text is shortened to card-friendly lengths, and the kept note set is reduced further if needed so the summary can paginate cleanly.

Do not make Gemini add notes merely to orient the reader to routine personal conversation. Notes should be reserved for content that may matter to case review, charges/evidence, confinement, allegedly criminal conduct, or another substantive attorney-review reason.

## System Audio Filtering

Automated telecom messages (IVR prompts, time warnings, provider sign-offs) are detected and filtered via `backend/system_audio.py`.

**Current behavior by summarization engine:**
- **Gemini:** runs a dedicated first-pass structured-output detection call on the raw turn transcript, applies filtering, then runs the citation-bearing summary call on the already-filtered transcript.
- **Gemma:** keeps the legacy combined summary prompt, with a trailing `SYSTEM_AUDIO: [...]` line parsed out of the summary response.

**Job-level `auto_message_mode` setting (UI toggle):**
- `None` / "Keep": No filtering, automated messages left as-is.
- `"exclude"`: System audio turns/words are removed entirely from the transcript.
- `"label"`: System audio turns are relabeled with speaker `"AUTOMATED MESSAGE"`. Consecutive automated turns are deduplicated (both channels carry the same audio) and merged into single turns.

The UI/API default is now `"label"` rather than "Keep".

For partial turns (real speech + system text in the same turn, e.g. "...they're playing us— You have 1 minute remaining."), the system text substring is split out — either stripped (exclude) or broken into a separate AUTOMATED MESSAGE turn (label).

When system-audio detection is enabled, `backend/pipeline.py` also removes any `NOTES` bullets that match identified automated telecom messages before generating PDFs. This keeps call-setup prompts and time warnings out of the summary product while still preserving them as `AUTOMATED MESSAGE` transcript turns in `"label"` mode.

**Important:** System audio filtering only runs when summarization is active (`skip_summary=False`). Test runs with dummy summaries get no filtering.

## Job Metadata & ICM XML

Per-call metadata (phone numbers, dates, inmate name, facility, outcome) comes from the GTL/ViaPath `ICM_report.xml` sidecar via `backend/icm_parser.py`, matched to audio by `<recordfilename>`. Case-level metadata (case name, defendant name, case context) is operator-entered in the job-setup UI and editable before the job starts. `POST /api/upload/xml` (and `POST /api/xml/preview` for pasted paths) returns a parsed preview — call count, inmate names, date range, facilities, unique numbers — which the UI shows under the XML field and uses to prefill the Defendant Name input when it's blank, so the operator can confirm/tweak the metadata that will flow into transcript covers, search.html, and the case report.

## Case Report Architecture

`backend/delivery/case_report.py` + `delivery/templates/case_report.html` generate `case-report.pdf`, a case-level dossier aggregating per-call analysis. It runs at the end of the pipeline inside `_stage_generate_delivery_assets` and is packaged into the delivery ZIP automatically.

Pipeline:
1. Parse every call's summary once through `parse_summary_sections` and reuse the result everywhere downstream.
2. Bucket calls by `RELEVANCE` (HIGH / MEDIUM / LOW / UNKNOWN).
3. Select a synthesis input set — HIGH calls first, topping up from MEDIUM if there are fewer than `TARGET_FINDINGS_INPUT_COUNT` (10) HIGH calls.
4. Issue **one** extra synthesis call, routed through the same summarization engine the per-call summaries used (Gemini cloud or Gemma local — fully interchangeable). This call performs BOTH "top findings" synthesis AND per-outside-number "identity inference" in a single prompt. Gemini uses structured JSON output; Gemma retains the legacy block-delimited fallback (`FINDING_START…FINDING_END`, `IDENTITY_START…IDENTITY_END`). The call is wrapped in a 3-attempt tenacity retry and a 120-second per-attempt `concurrent.futures` timeout.
5. Render a multi-section PDF: at-a-glance metrics, activity timeline, relevance distribution, top findings, high-relevance call cards, medium-relevance compact rows, and frequent-caller stats (including AI-inferred identities).

**Graceful degradation:** The template reads a `synthesis_state` variable (`ok` / `no_input` / `gemini_unavailable` / `parse_failed`) and renders a differentiated empty-state panel in the Top Findings section for each case, so a failed synthesis still produces a usable report.

**Voice contract for the synthesis prompt:** `CASE_REPORT_SYNTHESIS_PROMPT` in `case_report.py` explicitly instructs Gemini that (a) the audience is a legal professional who may be DEFENSE counsel OR PROSECUTION, so the voice must be neutral, objective, and reader-agnostic, (b) all findings must be written in third-person neutral past-tense factual prose, and (c) the second person ("you"/"your") is forbidden — even when narrating the defendant's side of the call. The defendant is NEVER "you". Before loosening those rules, check the prior failure mode: without them Gemini slipped into "the outside party informs you that…" because the transcript has the defendant speaking first-person and the model naturally adopted the defendant's POV.

**Link portability:** All hrefs rendered into the case report (viewer deep-links, transcript PDF links) are relative paths like `viewer.html?call=...` and `transcripts/<stem>.pdf`. Headless Chromium resolves them against the temp file it renders from, so the raw PDF's annotations arrive as absolute `file:///tmp/...` URIs. `_rewrite_local_links_to_launch_actions` post-processes the PDF with pypdf: `_extract_local_target` strips any absolute-or-relative URI down to its trailing `viewer.html[?query]` or `transcripts/<file>.pdf[#fragment]` form (percent-encoding preserved verbatim, non-local URIs left untouched) and the action is rewritten to `/Launch` with that relative path. Covered by `tests/test_case_report_links.py`; after changing the rewriter, also verify no `file://` string survives anywhere in a rendered report.

**When editing `delivery/templates/case_report.html`:** do a dry-run job end-to-end (even a small one) and spot-check the rendered PDF — its multi-section Paged.js layout uses `.pagedjs_page` page-chrome backgrounds, `@page` margin-box footers, and cover TOC page references via `target-counter(attr(href url), page)` that can regress silently. Note that the call card (`.cc`) deliberately does NOT carry `page-break-inside: avoid`; that rule is scoped to `.cc-head` only (plus `.cc-foot`, which also avoids `page-break-before` so the action buttons never strand alone), so long cue tables flow cleanly across page breaks instead of orphaning the entire chapter header onto its own near-empty page. The `@page` rule carries a `0.4in` top margin specifically so card/cue content that spills onto a continuation page does not ride the top sheet edge — summary lengths are non-deterministic, so never assume a card fits one page; chapter-start spacing compensates via `section.chapter .band { padding-top }`. Medium-row filenames are middle-truncated Python-side (`U.shorten_middle(..., 24)`) to fit their mono column on one line. The call-volume chart is inline SVG with geometry precomputed by `_build_timeline_svg`; `_build_timeline` picks day/week/month/year buckets by coverage span (deliveries range from weeks to 5+ years), so verify the chart at multiple spans after touching it.

## Testing

Two layers — run both with the pyenv Python (system `python3` is stale):

**1. Unit / regression suite:** `python -m pytest tests/` (fast, no network). Each file guards one specific contract:
* `test_transcript_summary_layout.py` — summary-sheet pagination, page-count budget, rendered-string regressions
* `test_quote_line_refs.py` — line-ref → quote hydration
* `test_case_report_links.py` — `/Launch` link rewriting, no surviving `file://` URIs
* `test_delivery_html.py` — script-safe JSON embedding in search/viewer; no WaveSurfer / remote scripts
* `test_guide_layout.py` — guide stays 7 pages, footer clearance (matches CSS-uppercased footer tokens)
* `test_pdf_render.py` — the Chromium render facade
* `test_pipeline_audio_regressions.py` — audio conversion/repair edge cases

**2. Canonical manual-review package:** `python tests/make_test_package.py` builds a complete synthetic delivery (default 10 calls) at `test-output/REEVES_TEST_PACKAGE/` — scripted transcripts with word-level timestamps, structured dummy summaries covering every relevance tier plus the unstructured-fallback and skip-summary paths, real tone MP3s via ffmpeg, and case-report synthesis through a canned stub engine. It runs the REAL delivery code (`create_pdf`, the pipeline's `_stage_generate_delivery_assets`, the zip arcname convention with `--zip`) — everything except live transcription/summarization, with no API keys and no job database. **This is the final step for any significant change: build the package and review every artifact by hand.** Options: `--calls N`, `--zip`, `--no-audio`. Output is gitignored.

## Guide Assets

`backend/delivery/assets/guide/` must contain `viewer_screenshot.png` and `search_screenshot.png` — the two screenshots embedded in `guide.pdf`. These are **synthetic** samples built from a fake "State v. Marcus Reeves" dataset, not from any real client job, so they can ship with every delivery without leaking case data. If you need to regenerate them (e.g., after a UI refresh):
- Build samples by calling `render_viewer` and `generate_search_html` against synthetic `CallResult` objects.
- Screenshot the viewer and search HTML via Chrome headless at `1600×1040`.
- Keep the scratch scripts out of the repo.

## Viewer Architecture

The HTML viewer (`backend/delivery/templates/viewer.html`) is a self-contained static page with all call data (and fonts) embedded. Audio plays through a native `<audio>` element wrapped in a small `ws` shim object — there is no WaveSurfer and no remote script (pinned by `tests/test_delivery_html.py`), because Web Audio cannot decode local files over `file://`. The scrub rail carries dashed crimson **cue ticks** at each review-cue timestamp.

**Layout:** left call-list rail (date/number/relevance/cue count per call) · main column (header, transport, printed-transcript pane) · right **Analysis rail** (relevance marker, summary, outside-party identity, clickable review cues that seek the audio; the cue nearest the playhead highlights). Both rails collapse to thin vertical strips (`localStorage`: `jcs_calls_collapsed`, `jcs_summary_collapsed`).

**Present mode** (`P` key, Present button, `Esc` exits): hides the band, both rails, and search — leaving only the audio transport and the printed-format transcript page, for playing a call while displaying its transcript (e.g., to a jury). The center pane renders transcript pages as white sheets in Courier Prime with line numbers, a double-rule corridor, and centered page numbers, mirroring the transcript PDF.

**Deep links:** Opening `viewer.html?call=…&t=…` seeks the audio AND jumps the transcript view to that moment immediately (page turn + line revealed via `revealTranscriptAt`, which falls back to the next upcoming line when the rounded timestamp lands between lines) while staying **paused** — playback starts only when the user presses play. In-page cue/line clicks still autoplay via `seekTo`.

**Word-level timestamps:** Each word in the transcript is rendered as a clickable `<span class="word-ts">` with `data-ws`/`data-we` attributes (start/end in seconds). Clicking a word seeks to that exact timestamp. During playback, the current word is highlighted with `active-word` class. When search is active, the view falls back to plain text rendering with `<mark>` highlights.

**Search mode:** Typing in the search box adds a `searching` class to `.trans-scroll`, which shows every transcript page at once and hides pages with no match (`.trans-page.no-match`). The pager switches from `Page N / M` to `N pages with matches` while search is active.

## Search Page Architecture

`backend/delivery/search_html.py` emits `search.html`, a single self-contained page that serves as the client-facing "home" of the delivery. A case masthead (court-caption title, Fraunces stat strip, clickable relevance tally) sits above a sticky control bar (full-text search with calls/mentions counters, date range, number filter, relevance segments) and a grid index — one row per call with relevance marker, date, length, outside party (number + identity inference), summary or match excerpts, and Viewer/PDF buttons. HIGH rows carry a crimson left spine.

Expanding a row reveals the summary, outside-party identity, review cues (with `Tr. page:line` and `PDF p.N` cites), and the **full scrollable transcript** with search matches highlighted; `‹ ›` controls step through matches (or through cue-flagged turns when no query is active), scrolling each into view. Excerpts, cues, and transcript lines deep-link into `viewer.html?call=…&t=…`. All data is embedded in an inline `<script>` JSON blob; there are no external dependencies.

Per-call `line_cite` (`Tr. 4:12`-style) values are computed Python-side at generation time via `compute_line_entries`, matching what the transcript PDF would show.
