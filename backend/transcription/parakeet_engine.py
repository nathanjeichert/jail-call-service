"""
Local transcription engine using NVIDIA Parakeet TDT via FluidAudio CoreML.

Handles stereo jail call audio by:
1. Splitting into per-channel mono 16 kHz WAV files (via ffmpeg)
2. Transcribing each channel with fluidaudiocli (CoreML, runs on ANE)
3. Parsing word-level timestamps from JSON output
4. Segmenting word streams into utterances based on silence gaps
5. Merging and interleaving utterances by timestamp
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

from ..audio_converter import FFMPEG_PATH, _find_binary
from ..models import TranscriptTurn, WordTimestamp
from .base import mark_continuation_turns, strip_preamble, strip_shared_system_turns

logger = logging.getLogger(__name__)

UTTERANCE_GAP_SECONDS = 1.5

_FLUIDAUDIO_PATH: Optional[str] = None


def _find_fluidaudiocli() -> Optional[str]:
    """Find the fluidaudiocli binary, checking project bin/ first."""
    global _FLUIDAUDIO_PATH
    if _FLUIDAUDIO_PATH is not None:
        return _FLUIDAUDIO_PATH

    # Project-bundled binary (not covered by the generic _find_binary)
    project_bin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "bin", "fluidaudiocli",
    )
    if os.path.isfile(project_bin):
        _FLUIDAUDIO_PATH = project_bin
        return _FLUIDAUDIO_PATH

    # Reuse the generic env / PATH / common-locations search
    found = _find_binary("fluidaudiocli", "FLUIDAUDIO_PATH")
    if found:
        _FLUIDAUDIO_PATH = found
    return _FLUIDAUDIO_PATH


def _split_channels(audio_path: str, work_dir: str) -> tuple[str, str]:
    """
    Split a stereo audio file into two mono 16 kHz WAV files.
    Returns (ch1_path, ch2_path).
    """
    if not FFMPEG_PATH:
        raise RuntimeError("ffmpeg not found — required for channel splitting")

    ch1_path = os.path.join(work_dir, "ch1.wav")
    ch2_path = os.path.join(work_dir, "ch2.wav")

    cmd = [
        FFMPEG_PATH, "-y",
        "-i", audio_path,
        "-filter_complex",
        "[0:a]channelsplit=channel_layout=stereo[left][right]",
        "-map", "[left]", "-ar", "16000", "-ac", "1", ch1_path,
        "-map", "[right]", "-ar", "16000", "-ac", "1", ch2_path,
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg channel split failed (rc={proc.returncode}): {proc.stderr[-500:]}"
        )

    return ch1_path, ch2_path


def _transcribe_channel_sync(cli_path: str, wav_path: str) -> list[dict]:
    """
    Transcribe a single mono WAV file using fluidaudiocli and return
    word-level timestamps.

    Returns list of dicts: [{"text": str, "start": float_sec, "end": float_sec, "confidence": float}, ...]
    """
    # Write JSON output to a temp file
    json_path = wav_path + ".json"

    cmd = [
        cli_path, "transcribe", wav_path,
        "--model-version", "v2",
        "--output-json", json_path,
    ]

    logger.info("Parakeet: running fluidaudiocli on %s", os.path.basename(wav_path))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if proc.returncode != 0:
        stderr = proc.stderr[-500:] if proc.stderr else ""
        raise RuntimeError(
            f"fluidaudiocli failed (rc={proc.returncode}): {stderr}"
        )

    # Parse JSON output
    try:
        with open(json_path, "r") as f:
            result = json.load(f)
    finally:
        try:
            os.unlink(json_path)
        except OSError:
            pass

    # Extract word timings
    words = []
    for wt in result.get("wordTimings", []):
        text = wt.get("word", "").strip()
        if not text:
            continue
        words.append({
            "text": text,
            "start": wt["startTime"],       # seconds
            "end": wt["endTime"],            # seconds
            "confidence": wt.get("confidence", 0.0),
        })

    rtfx = result.get("rtfx")
    duration = result.get("durationSeconds")
    proc_time = result.get("processingTimeSeconds")
    if rtfx:
        logger.info("Parakeet: %s — %.1fx realtime (%.1fs audio in %.1fs)",
                     os.path.basename(wav_path), rtfx, duration or 0, proc_time or 0)

    return words


def _segment_words(
    words: list[dict],
    channel: int,
    channel_labels: Dict[int, str],
    gap_threshold: float = UTTERANCE_GAP_SECONDS,
) -> list[dict]:
    """
    Group a flat word list into utterance segments based on silence gaps.
    """
    if not words:
        return []

    speaker = channel_labels.get(channel) or f"CHANNEL {channel}"
    segments = []
    current_words = [words[0]]

    for w in words[1:]:
        prev_end = current_words[-1]["end"]
        if w["start"] - prev_end > gap_threshold:
            segments.append(_make_segment(current_words, channel, speaker))
            current_words = [w]
        else:
            current_words.append(w)

    if current_words:
        segments.append(_make_segment(current_words, channel, speaker))

    return segments


def _make_segment(words: list[dict], channel: int, speaker: str) -> dict:
    text = " ".join(w["text"] for w in words)
    return {
        "channel": channel,
        "speaker": speaker,
        "start_sec": words[0]["start"],
        "end_sec": words[-1]["end"],
        "text": text,
        "words": words,
    }


def _merge_segments(
    ch1_segments: list[dict],
    ch2_segments: list[dict],
) -> List[TranscriptTurn]:
    """
    Interleave segments from both channels by start time and convert to
    TranscriptTurn objects matching the app's data model.
    """
    all_segments = ch1_segments + ch2_segments
    all_segments.sort(key=lambda s: s["start_sec"])

    turns: List[TranscriptTurn] = []
    for seg in all_segments:
        start_sec = seg["start_sec"]
        minutes = int(start_sec // 60)
        seconds = int(start_sec % 60)
        timestamp_str = f"[{minutes:02d}:{seconds:02d}]"

        word_timestamps = [
            WordTimestamp(
                text=w["text"],
                start=w["start"] * 1000,   # convert to ms (app convention)
                end=w["end"] * 1000,
                confidence=w.get("confidence"),
                speaker=seg["speaker"],
            )
            for w in seg["words"]
        ]

        turns.append(TranscriptTurn(
            speaker=seg["speaker"],
            text=seg["text"],
            timestamp=timestamp_str,
            words=word_timestamps if word_timestamps else None,
        ))

    return turns


def _analyze_preamble_audio(ch1_path: str, ch2_path: str) -> Tuple[Optional[float], List[Tuple[float, float]]]:
    """
    Analyze shared audio regions between channels during the robocall preamble.

    The preamble has high correlation between channels (same audio on both
    sides), interspersed with silence gaps (DTMF tones, phone number entry)
    where one channel goes quiet. We return both the contiguous regions of
    high-correlation audio and a conservative boundary near the end of the
    final shared region.

    Returns:
        (boundary_sec, high_corr_regions)
    """
    try:
        import numpy as np
        import wave
    except ImportError:
        logger.warning("numpy not available — skipping audio correlation")
        return None, []

    try:
        with wave.open(ch1_path, "rb") as wf:
            ch1 = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32)
        with wave.open(ch2_path, "rb") as wf:
            ch2 = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32)
    except Exception as e:
        logger.warning("Could not read channel WAVs for correlation: %s", e)
        return None, []

    sample_rate = 16000
    window_samples = sample_rate  # 1-second windows
    hop_samples = sample_rate // 2  # 0.5-second hop
    hop_sec = hop_samples / sample_rate
    min_len = min(len(ch1), len(ch2))

    # Only scan first 3 minutes — preamble is never longer than that
    scan_limit = min(min_len, sample_rate * 180)

    # Minimum amplitude for a window to count as "active speech"
    # (avoids treating silence/noise as correlated or uncorrelated)
    min_energy = 10000.0
    high_corr_threshold = 0.9

    high_regions: List[Tuple[float, float]] = []
    region_start = None
    region_end = None

    for start in range(0, scan_limit - window_samples, hop_samples):
        w1 = ch1[start : start + window_samples]
        w2 = ch2[start : start + window_samples]
        window_start_sec = start / sample_rate
        window_end_sec = (start + window_samples) / sample_rate

        norm1 = np.linalg.norm(w1)
        norm2 = np.linalg.norm(w2)

        # Skip windows where either channel is near-silent
        if norm1 < min_energy or norm2 < min_energy:
            if region_start is not None:
                high_regions.append((region_start, region_end or window_start_sec))
                region_start = None
                region_end = None
            continue

        corr = np.dot(w1, w2) / (norm1 * norm2)

        if corr >= high_corr_threshold:
            if region_start is None:
                region_start = window_start_sec
                region_end = window_end_sec
            elif window_start_sec <= (region_end or window_start_sec) + hop_sec:
                region_end = window_end_sec
            else:
                high_regions.append((region_start, region_end or window_start_sec))
                region_start = window_start_sec
                region_end = window_end_sec
        elif region_start is not None:
            high_regions.append((region_start, region_end or window_start_sec))
            region_start = None
            region_end = None

    if region_start is not None:
        high_regions.append((region_start, region_end or (scan_limit / sample_rate)))

    if high_regions:
        boundary_sec = high_regions[-1][1] + 5.0
        logger.info("Preamble boundary via audio correlation: %.1fs", boundary_sec)
        return boundary_sec, high_regions

    return None, []


def _find_preamble_boundary(ch1_path: str, ch2_path: str) -> Optional[float]:
    boundary_sec, _ = _analyze_preamble_audio(ch1_path, ch2_path)
    return boundary_sec


class ParakeetEngine:
    """Local transcription engine using Parakeet TDT via FluidAudio CoreML."""

    async def transcribe(
        self,
        audio_path: str,
        channel_labels: Optional[Dict[int, str]] = None,
    ) -> List[TranscriptTurn]:
        cli_path = _find_fluidaudiocli()
        if not cli_path:
            raise RuntimeError(
                "fluidaudiocli not found. Place the binary in bin/fluidaudiocli "
                "or set the FLUIDAUDIO_PATH environment variable."
            )

        labels = channel_labels or {1: "CHANNEL 1", 2: "CHANNEL 2"}
        loop = asyncio.get_event_loop()

        work_dir = tempfile.mkdtemp(prefix="parakeet_")

        try:
            # 1. Split stereo into mono channels
            logger.info("Parakeet: splitting channels for %s", audio_path)
            ch1_path, ch2_path = await loop.run_in_executor(
                None, _split_channels, audio_path, work_dir,
            )

            # 2. Transcribe each channel (sequential to be safe on 8GB)
            logger.info("Parakeet: transcribing channel 1")
            ch1_words = await loop.run_in_executor(
                None, _transcribe_channel_sync, cli_path, ch1_path,
            )

            logger.info("Parakeet: transcribing channel 2")
            ch2_words = await loop.run_in_executor(
                None, _transcribe_channel_sync, cli_path, ch2_path,
            )

            # 3. Detect preamble boundary via audio cross-correlation
            correlation_boundary, correlation_regions = await loop.run_in_executor(
                None, _analyze_preamble_audio, ch1_path, ch2_path,
            )

            # 4. Segment words into utterances
            ch1_segments = _segment_words(ch1_words, 1, labels)
            ch2_segments = _segment_words(ch2_words, 2, labels)

            # 5. Merge and interleave
            turns = _merge_segments(ch1_segments, ch2_segments)

            # 6. Strip robocall preamble (duplicated turns on both channels)
            turns = strip_preamble(
                turns,
                correlation_boundary_sec=correlation_boundary,
                correlation_regions=correlation_regions,
            )
            turns = strip_shared_system_turns(turns)

            logger.info(
                "Parakeet: %d turns from %d ch1 + %d ch2 segments for %s",
                len(turns), len(ch1_segments), len(ch2_segments), audio_path,
            )

            return mark_continuation_turns(turns)

        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
