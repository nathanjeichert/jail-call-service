"""
End-to-end test: pick 5 WAV files, convert, transcribe with Parakeet
(including preamble stripping + audio correlation), generate PDFs + viewer.
"""

import asyncio
import os
import shutil
import subprocess
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))

from backend.audio_converter import FFMPEG_PATH
from backend.transcription.parakeet_engine import ParakeetEngine
from backend.transcription.base import mark_continuation_turns
from backend.transcript_formatting import create_pdf
from backend.viewer import render_viewer
from backend.models import CallResult, CallStatus

WAV_FILES = [
    "uploads/d4333a15-d6be-41b9-bacf-6d8f0ba38489/1649909618_5000_13_166_945.wav",
    "uploads/d4333a15-d6be-41b9-bacf-6d8f0ba38489/1646962560_5000_13_159_593.wav",
    "uploads/d4333a15-d6be-41b9-bacf-6d8f0ba38489/1646692552_5000_12_199_239.wav",
    "uploads/d4333a15-d6be-41b9-bacf-6d8f0ba38489/1647312389_5000_12_183_590.wav",
    "uploads/d4333a15-d6be-41b9-bacf-6d8f0ba38489/1647235088_5000_12_158_636.wav",
]

OUTPUT_DIR = "test_preamble_output"


def convert_to_mp3(wav_path: str, mp3_path: str) -> float:
    """Convert WAV to MP3 and return duration in seconds."""
    cmd = [
        FFMPEG_PATH, "-y", "-i", wav_path,
        "-ac", "2", "-ar", "44100", "-b:a", "128k", mp3_path,
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    # Get duration
    probe = subprocess.run(
        [FFMPEG_PATH, "-i", mp3_path, "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    import re
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", probe.stderr)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 100
    return 0.0


async def main():
    os.makedirs(os.path.join(OUTPUT_DIR, "audio"), exist_ok=True)
    os.makedirs(os.path.join(OUTPUT_DIR, "transcripts"), exist_ok=True)
    os.makedirs(os.path.join(OUTPUT_DIR, "viewer"), exist_ok=True)

    engine = ParakeetEngine()
    calls = []

    for idx, wav_rel in enumerate(WAV_FILES):
        wav_path = os.path.abspath(wav_rel)
        stem = os.path.splitext(os.path.basename(wav_rel))[0]
        mp3_name = f"{idx + 1:03d}-{stem}.mp3"
        mp3_path = os.path.abspath(os.path.join(OUTPUT_DIR, "audio", mp3_name))

        print(f"\n{'='*60}")
        print(f"[{idx+1}/5] {os.path.basename(wav_rel)}")
        print(f"{'='*60}")

        # Convert
        print("  Converting to MP3...")
        duration = convert_to_mp3(wav_path, mp3_path)
        print(f"  Duration: {duration:.1f}s")

        # Transcribe (includes preamble stripping)
        print("  Transcribing with Parakeet...")
        turns = await engine.transcribe(
            mp3_path,
            channel_labels={1: "INMATE", 2: "OUTSIDE PARTY"},
        )
        print(f"  Got {len(turns)} turns (after preamble strip)")
        if turns:
            print(f"  First turn: [{turns[0].speaker}] {turns[0].text[:60]}...")

        # Generate PDF
        title_data = {
            "case_name": "Preamble Strip E2E Test",
            "inmate_name": "INMATE",
            "filename": os.path.basename(wav_rel),
            "audio_filename": mp3_name,
        }
        pdf_bytes = create_pdf(
            title_data=title_data,
            turns=turns,
            summary=None,
            audio_duration=duration,
        )
        pdf_path = os.path.join(OUTPUT_DIR, "transcripts", f"{idx + 1:03d}-{stem}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        print(f"  PDF: {pdf_path} ({len(pdf_bytes)} bytes)")

        calls.append(CallResult(
            index=idx,
            filename=os.path.basename(wav_rel),
            original_path=wav_path,
            mp3_path=mp3_path,
            duration_seconds=duration,
            turns=turns,
            summary="(no summary — preamble strip test)",
            status=CallStatus.DONE,
        ))

    # Generate viewer
    print(f"\n{'='*60}")
    print("Generating viewer...")
    viewer_html = render_viewer(calls, case_name="Preamble Strip E2E Test")
    viewer_path = os.path.join(OUTPUT_DIR, "viewer", "index.html")
    with open(viewer_path, "w", encoding="utf-8") as f:
        f.write(viewer_html)
    print(f"Viewer: {viewer_path}")

    print(f"\nDone! All output in {OUTPUT_DIR}/")
    return calls


if __name__ == "__main__":
    asyncio.run(main())
