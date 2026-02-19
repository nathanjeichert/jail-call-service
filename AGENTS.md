# AGENTS.md - Jail Call Service Context

This document provides codebase context for future AI coding agents working on the `jail-call-service` component of TranscribeAlpha.

## Relationship to Main App
The `jail-call-service` is a **spin-off/local-first** application residing within the broader TranscribeAlpha monorepo. 
* **Target Audience:** Used internally/locally by the operator (Nathan) rather than acting as a SaaS for tech-illiterate lawyers.
* **Volume:** Designed to process thousands of G.729 WAV files (e.g. batches of 2,000+ files) per job.
* **Architecture differences:** It uses a local SQLite database (`backend/db.py`) to avoid memory bloat during massive parallel processing instead of standard JSON/flat state.

## Tech Stack & Architecture
* **Backend:** FastAPI, Python, AssemblyAI, Gemini 2.0 Flash, ffmpeg-python.
* **Frontend:** Next.js (Tailwind + TypeScript).
* **Delivery Payload:** The software produces a `.zip` artifact containing:
  - Repaired and converted MP3 audio files.
  - A spreadsheet index (`call-index.xlsx`) linking to transcript/viewer payloads.
  - Formatted transcript PDFs (`transcripts/`).
  - A responsive local HTML viewer dashboard (`viewer/index.html`) using WaveSurfer.js.

## Critical Technical Details
1. **Parallelization:** `_stage_convert` handles conversion via `ThreadPoolExecutor` (bottlenecked dynamically to CPU count) to avoid exhaustion, while Gemini and AssemblyAI use asyncio semaphores.
2. **Resiliency:** SQLite is used with SQLAlchemy models (`db_models.py`) to checkpoint job progression (`job_store.py`) to survive crashes without wasting API credits.
3. **Transcription Format:** Employs AssemblyAI multichannel transcription where **Channel 1** represents the Inmate (Defendant) and **Channel 2** represents the Outside Party.
4. **Export Deep Linking:** When generating PDFs and Excel spreadsheets, the app creates `../viewer/index.html?call=?t=[xx:xx]` links directly pointing to specific timestamps within the self-contained static HTML viewer.

If editing the `template.html` for the viewer, note that template parameters (like `{{CALLS_JSON}}`) are pre-populated by Python string manipulation, hence why TypeScript/Javascript linting will report errors inside the HTML syntax. Ignore these syntactical lint warnings during development.
