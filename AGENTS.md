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
  - Repaired and converted MP3 audio files.
  - A spreadsheet index (`call-index.xlsx`) linking to transcript/viewer payloads.
  - Formatted transcript PDFs (`transcripts/`).
  - A responsive local HTML viewer dashboard (`viewer/index.html`) using WaveSurfer.js.

## Critical Technical Details
1. **Parallelization:** `_stage_convert` handles conversion via `ThreadPoolExecutor` (bottlenecked dynamically to CPU count) to avoid exhaustion, while Gemini and AssemblyAI use asyncio semaphores. When both transcription and summarization use local engines (Parakeet + Gemma), the pipeline serializes the stages to avoid competing for GPU/unified memory.
2. **Resiliency:** SQLite is used with SQLAlchemy models (`db_models.py`) to checkpoint job progression (`job_store.py`) to survive crashes without wasting API credits.
3. **Database Concurrency:** SQLite is configured with WAL mode and a 30-second busy timeout to handle concurrent writes from parallel pipeline stages (conversion threads, async transcription/summarization).
4. **Transcription Format:** Both engines produce the same `List[TranscriptTurn]` output. **Channel 1** = Inmate (Defendant), **Channel 2** = Outside Party.
5. **Export Deep Linking:** When generating PDFs and Excel spreadsheets, the app creates `../viewer/index.html?call=[file]&t=[xx:xx]` links directly pointing to specific timestamps within the self-contained static HTML viewer.
6. **PDF Architecture:** `backend/transcript_formatting.py` builds PDFs as a hybrid document:
   - Title and Gemini summary pages are rendered from `backend/pdf_cover_template.html` with WeasyPrint / HTML / CSS.
   - Transcript body pages remain ReportLab-generated legal transcript pages with Courier text, line numbers, and page numbers.
   - The summary page parses Gemini's structured sections (`RELEVANCE`, `NOTES`, `IDENTITY OF OUTSIDE PARTY`, `BRIEF SUMMARY`). `NOTES: NONE` renders as a polished "No relevant information found" panel.
   - Notes display audio timestamps plus transcript page:line cites, with a single legend explaining `[MM:SS]` and `Page:Line`.

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

When system-audio detection is enabled, `backend/pipeline.py` also removes any Gemini `NOTES` bullets that match identified automated telecom messages before generating PDFs. This keeps call-setup prompts and time warnings out of the summary product while still preserving them as `AUTOMATED MESSAGE` transcript turns in `"label"` mode.

**Important:** System audio filtering only runs when Gemini summarization is active (`skip_summary=False`). Test runs with dummy summaries get no filtering.

## Viewer Architecture

The HTML viewer (`backend/viewer/template.html`) is a self-contained static page with all call data embedded as JSON. It supports two audio backends:
- **WaveSurfer.js** (default when served via HTTP) — waveform visualization.
- **Native `<audio>` element** (automatic when opened via `file://` protocol, or forced with `?native_audio=1`) — lightweight shim that avoids WaveSurfer's CORS issues with local files.

**Word-level timestamps:** Each word in the transcript is rendered as a clickable `<span class="word-ts">` with `data-ws`/`data-we` attributes (start/end in seconds). Clicking a word seeks to that exact timestamp. During playback, the current word is highlighted with `active-word` class. When search is active, the view falls back to plain text rendering with `<mark>` highlights.
