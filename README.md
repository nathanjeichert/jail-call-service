# Jail Call Service

Local-first batch processing for G.729 jail call recordings. Takes a folder
of WAV files (plus the provider's ICM metadata XML) and delivers a zip with
per-call transcripts, audio, an offline call index and viewer, a case
report, and a reviewer's guide.

## Setup

```bash
# 1. Python dependencies (use the pyenv interpreter, 3.12)
pip install -e ".[dev]"                       # add ",local" for Gemma via mlx-lm
python -m playwright install chromium        # headless Chromium for PDF rendering

# 2. Keys and settings
cp .env.example .env                          # fill in what you use

# 3. Frontend dependencies
cd frontend && npm install && cd ..

# 4. (Optional) local transcription engine
#    Place the fluidaudiocli binary at bin/fluidaudiocli
#    (build from https://github.com/FluidInference/FluidAudio, or run
#    .github/workflows/build-fluidaudio.yml). Models download on first use.

# 5. Run
./run.sh                                      # backend :8000 + frontend :3000
```

Requirements: Python 3.11+, Node 20.9+, `ffmpeg` on PATH (`brew install ffmpeg`).

## Engines

Selectable per job in the UI. Any transcription engine pairs with any
summarization engine; the rest of the pipeline is engine-agnostic.

| Stage | Engine | Runs | Needs |
|---|---|---|---|
| Transcription | AssemblyAI (multichannel) | cloud | `ASSEMBLYAI_API_KEY` |
| Transcription | Parakeet TDT 0.6b v2 via FluidAudio CoreML | local (Apple Neural Engine) | `bin/fluidaudiocli` |
| Summarization | Gemini Flash (structured JSON) | cloud | `GEMINI_API_KEY` |
| Summarization | Gemma 4 E2B via mlx-lm | local (Metal) | `mlx-lm`, ~4 GB RAM |

## Usage

1. Open http://localhost:3000
2. New job: case name, defendant, engines, speaker side, then upload or paste
   the audio paths (and the `ICM_report.xml` if you have it)
3. The pipeline starts automatically; pause/resume/retry from the job page
4. When done, **Review Transcripts** to check or edit summaries
5. **Approve All & Package**, then **Download Zip**

## Delivery zip

```
{CaseName}/
├── transcripts/              # PDF per call: cover, AI summary sheet(s), ruled transcript
├── transcripts-no-summary/   # same PDFs without the summary sheet
├── audio/                    # converted MP3s
├── index.html                # searchable call index + offline player with synced transcript
├── search/                   # its search indexes: keyword (index.js) and, when built, meaning search
│                             #   (passage vectors, the embedding model, ONNX Runtime) as script sidecars
├── case-report.pdf           # case-level findings, caller stats, timeline
└── guide.pdf                 # how to use the package
```

## Pipeline

1. **Convert** — repairs zeroed G.729 WAV headers on a working copy, converts to MP3 (parallel)
2. **Transcribe** — two channels → speaker-attributed turns with word timestamps
3. **Summarize** — automated-message detection, then a structured per-call summary
4. **Generate** — transcript PDFs, then index.html, guide, and case report
5. **Package** — zips the output folder

Every stage transition is checkpointed in SQLite (`jobs/jail_calls.db`), so
a paused or interrupted job resumes without re-spending API credits.

## Development

```bash
python -m pytest tests/                       # unit + regression suite (includes the golden package diff)
python -m ruff check backend tests            # lint
python tests/make_test_package.py             # synthetic delivery package for manual review
```

CI (`.github/workflows/ci.yml`) runs the lint, the test suite, `tsc`, and
`next build` on every push and pull request.

`AGENTS.md` is the architecture reference for the codebase.
