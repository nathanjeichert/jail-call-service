"""
Base protocol and shared utilities for transcription engines.
"""

import re
from typing import Dict, List, Optional, Protocol

from ..models import TranscriptTurn


# ── Shared utilities ──

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


# ── Engine protocol ──

class TranscriptionEngine(Protocol):
    """Interface that all transcription engines must implement."""

    async def transcribe(
        self,
        audio_path: str,
        channel_labels: Optional[Dict[int, str]] = None,
    ) -> List[TranscriptTurn]:
        """
        Transcribe a 2-channel audio file and return speaker-attributed turns.

        Args:
            audio_path: Path to the audio file (MP3 or WAV).
            channel_labels: Mapping of channel index to speaker name,
                            e.g. {1: "INMATE", 2: "OUTSIDE PARTY"}.

        Returns:
            Ordered list of TranscriptTurn objects.
        """
        ...
