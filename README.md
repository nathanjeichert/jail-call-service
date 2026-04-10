# Jail Call Service

Local-first batch processing tool for G.729 jail call recordings.
Takes a folder of WAV files, delivers a zip with transcripts, audio, and indexes.

## Setup

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Set up API keys
cp .env.example .env
# Edit .env with your AssemblyAI and Gemini API keys

# 3. Install frontend dependencies
cd frontend && npm install && cd ..

# 4. (Optional) Install local transcription engine
# Place the fluidaudiocli binary at bin/fluidaudiocli
# Build from source: https://github.com/FluidInference/FluidAudio (requires Xcode)
# Or run the GitHub Actions workflow: .github/workflows/build-fluidaudio.yml

# 5. Start everything
./run.sh
# Open http://localhost:3000
```

## Transcription Engines

| Engine | Type | Speed (17-min call) | Requirements |
|--------|------|---------------------|--------------|
| **AssemblyAI** | Cloud API | ~30s (depends on queue) | API key |
| **Parakeet** | Local (CoreML) | ~21s on M2 | `bin/fluidaudiocli` binary |

Select the engine per-job from the UI. The local engine uses NVIDIA Parakeet TDT 0.6b v2 via FluidAudio CoreML, running on Apple's Neural Engine. No API key or internet needed after initial model download.

## Usage

1. Open http://localhost:3000
2. Create a job: provide case name, choose transcription engine, and path to WAV files
3. Click "Start Processing" — the pipeline runs automatically
4. When done, click "Review Transcripts" to check/edit summaries
5. Click "Approve All & Package" → "Download Zip"

## Deliverable Zip Contents

```
{CaseName}/
├── transcripts/          # PDF per call (summary on page 2)
├── audio/                # Converted MP3 files
├── viewer.html           # Multi-call browser player
├── search.html           # Full-text search
├── call-index.xlsx       # Spreadsheet index
└── README.txt            # Instructions for the attorney
```

## Pipeline Stages

1. **Convert** — repairs corrupted G.729 WAV headers, converts to MP3 (parallel)
2. **Transcribe** — AssemblyAI multichannel or Parakeet local (inmate/outside party)
3. **Summarize** — Gemini Flash generates summaries (parallel, rate-limited)
4. **Generate** — PDFs, Excel, search HTML, viewer HTML
5. **Package** — Zips the deliverable folder

## Requirements

- Python 3.11+
- Node.js 18+
- ffmpeg (must be on PATH: `brew install ffmpeg`)
- Gemini API key (or GOOGLE_API_KEY)
- AssemblyAI API key (only if using cloud transcription)
- `bin/fluidaudiocli` (only if using local transcription)
