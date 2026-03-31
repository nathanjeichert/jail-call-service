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
* **Summarization:** Gemini Flash.
* **Frontend:** Next.js (Tailwind + TypeScript).
* **Delivery Payload:** The software produces a `.zip` artifact containing:
  - Repaired and converted MP3 audio files.
  - A spreadsheet index (`call-index.xlsx`) linking to transcript/viewer payloads.
  - Formatted transcript PDFs (`transcripts/`).
  - A responsive local HTML viewer dashboard (`viewer/index.html`) using WaveSurfer.js.

## Critical Technical Details
1. **Parallelization:** `_stage_convert` handles conversion via `ThreadPoolExecutor` (bottlenecked dynamically to CPU count) to avoid exhaustion, while Gemini and AssemblyAI use asyncio semaphores.
2. **Resiliency:** SQLite is used with SQLAlchemy models (`db_models.py`) to checkpoint job progression (`job_store.py`) to survive crashes without wasting API credits.
3. **Database Concurrency:** SQLite is configured with WAL mode and a 30-second busy timeout to handle concurrent writes from parallel pipeline stages (conversion threads, async transcription/summarization).
4. **Transcription Format:** Both engines produce the same `List[TranscriptTurn]` output. **Channel 1** = Inmate (Defendant), **Channel 2** = Outside Party.
5. **Export Deep Linking:** When generating PDFs and Excel spreadsheets, the app creates `../viewer/index.html?call=?t=[xx:xx]` links directly pointing to specific timestamps within the self-contained static HTML viewer.

If editing the `template.html` for the viewer, note that template parameters (like `{{CALLS_JSON}}`) are pre-populated by Python string manipulation, hence why TypeScript/Javascript linting will report errors inside the HTML syntax. Ignore these syntactical lint warnings during development.

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
