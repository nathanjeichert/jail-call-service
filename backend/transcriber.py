"""
AssemblyAI multichannel transcription for jail call recordings.

Jail calls are 2-channel: channel 1 = inmate, channel 2 = outside party.
We use AssemblyAI's multichannel mode which keeps them separate and
provides clean speaker attribution.
"""

import inspect
import logging
import re
import shutil
import subprocess
from typing import Dict, List, Optional

from tenacity import retry, retry_if_exception_type, wait_random_exponential, stop_after_attempt

from .models import TranscriptTurn, WordTimestamp

logger = logging.getLogger(__name__)

# Lazy import AssemblyAI so missing SDK doesn't crash at import time
try:
    import assemblyai as aai
    ASSEMBLYAI_AVAILABLE = True
except ImportError:
    ASSEMBLYAI_AVAILABLE = False
    logger.warning("AssemblyAI SDK not installed. Run: pip install assemblyai")

_SPEAKER_LETTER_RE = re.compile(r"^[A-Z]$")
_SPEAKER_NUMERIC_RE = re.compile(r"^[0-9]+$")


def normalize_speaker_label(raw_value: object, fallback: str = "SPEAKER A") -> str:
    fallback_value = str(fallback or "").strip().upper() or "SPEAKER A"
    candidate = str(raw_value or "").strip()
    candidate = re.sub(r":+$", "", candidate).strip().upper()

    if not candidate or candidate == "UNKNOWN":
        return fallback_value

    if candidate.startswith("SPEAKER"):
        suffix = candidate[len("SPEAKER"):].strip()
        return f"SPEAKER {suffix}" if suffix else "SPEAKER"

    if _SPEAKER_LETTER_RE.fullmatch(candidate) or _SPEAKER_NUMERIC_RE.fullmatch(candidate):
        return f"SPEAKER {candidate}"

    return candidate


def mark_continuation_turns(turns: List[TranscriptTurn]) -> List[TranscriptTurn]:
    prev_speaker = None
    for turn in turns:
        normalized = turn.speaker.strip().upper()
        turn.is_continuation = prev_speaker is not None and normalized == prev_speaker
        prev_speaker = normalized
    return turns


def build_multichannel_config() -> "aai.TranscriptionConfig":
    """AssemblyAI config for 2-channel jail call audio."""
    if not ASSEMBLYAI_AVAILABLE:
        raise RuntimeError("AssemblyAI SDK not installed. Run: pip install assemblyai")

    prompt = (
        "Produce a verbatim transcript. Include disfluencies and fillers "
        "(um, uh, er, ah, hmm, mhm, like, you know, I mean), "
        "repetitions (I I, the the), restarts (I was- I went), "
        "stutters (th-that, b-but), and informal speech (gonna, wanna, gotta)."
    )

    kwargs = {
        "speech_models": ["universal-3-pro"],
        "prompt": prompt,
        "format_text": True,
        "multichannel": True,
    }

    if "temperature" in inspect.signature(aai.TranscriptionConfig).parameters:
        kwargs["temperature"] = 0.1

    return aai.TranscriptionConfig(**kwargs)


def transcribe_multichannel(
    audio_path: str,
    api_key: str,
    channel_labels: Optional[Dict[int, str]] = None,
) -> List[TranscriptTurn]:
    """
    Transcribe a 2-channel audio file using AssemblyAI multichannel mode.

    Args:
        audio_path: Path to the MP3/WAV file
        api_key: AssemblyAI API key
        channel_labels: Optional {1: "Inmate", 2: "Outside Party"} label override

    Returns:
        List of TranscriptTurn objects
    """
    if not ASSEMBLYAI_AVAILABLE:
        raise RuntimeError("AssemblyAI SDK not installed")

    aai.settings.api_key = api_key
    aai.settings.http_timeout = 600.0

    logger.info("Starting multichannel transcription: %s", audio_path)

    @retry(
        wait=wait_random_exponential(min=2, max=60),
        stop=stop_after_attempt(4),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    )
    def _transcribe_with_retry():
        t = aai.Transcriber()
        c = build_multichannel_config()
        return t.transcribe(audio_path, config=c)

    response = _transcribe_with_retry()

    if response.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI error: {response.error}")

    logger.info("Transcription done, converting utterances")
    turns = _turns_from_multichannel_response(response, channel_labels)
    return mark_continuation_turns(turns)


def _turns_from_multichannel_response(
    response: object,
    channel_labels: Optional[Dict[int, str]] = None,
) -> List[TranscriptTurn]:
    transcript = getattr(response, "transcript", None) or response
    utterances = getattr(transcript, "utterances", None) or []

    # Build normalized label map: {channel_index: label}
    labels: Dict[int, str] = {}
    if channel_labels:
        for k, v in channel_labels.items():
            try:
                labels[int(k)] = str(v).strip()
            except (TypeError, ValueError):
                pass

    turns: List[TranscriptTurn] = []
    for utterance in utterances:
        channel_raw = getattr(utterance, "channel", None)
        try:
            channel_index = int(channel_raw)
        except (TypeError, ValueError):
            channel_index = 1

        speaker_name = labels.get(channel_index) or f"CHANNEL {channel_index}"

        # Timestamp
        timestamp_str = None
        if getattr(utterance, "start", None) is not None:
            start_ms = float(utterance.start)
            minutes = int(start_ms // 60000)
            seconds = int((start_ms % 60000) // 1000)
            timestamp_str = f"[{minutes:02d}:{seconds:02d}]"

        # Word-level timestamps
        word_timestamps: List[WordTimestamp] = []
        for word in getattr(utterance, "words", None) or []:
            word_text = getattr(word, "text", "")
            if not word_text:
                continue
            start_val = getattr(word, "start", None)
            end_val = getattr(word, "end", None)
            if start_val is None or end_val is None:
                continue
            confidence_val = getattr(word, "confidence", None)
            word_timestamps.append(WordTimestamp(
                text=str(word_text),
                start=float(start_val),
                end=float(end_val),
                confidence=float(confidence_val) if confidence_val is not None else None,
                speaker=speaker_name,
            ))

        turns.append(TranscriptTurn(
            speaker=speaker_name,
            text=str(getattr(utterance, "text", "") or ""),
            timestamp=timestamp_str,
            words=word_timestamps if word_timestamps else None,
        ))

    if turns:
        return turns

    # Fallback: single block
    text_value = str(getattr(transcript, "text", "") or "").strip()
    if not text_value:
        return []
    return [TranscriptTurn(speaker="CHANNEL 1", text=text_value, timestamp="[00:00]")]
