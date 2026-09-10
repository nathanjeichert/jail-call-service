"""
Base protocol and shared utilities for transcription engines.
"""

from typing import Dict, List, Optional, Protocol

from ..models import TranscriptTurn

# ── Shared utilities ──

def default_channel_speaker(channel: int) -> str:
    return {
        1: "LEFT SPEAKER",
        2: "RIGHT SPEAKER",
    }.get(int(channel), f"SPEAKER {channel}")


def mark_continuation_turns(turns: List[TranscriptTurn]) -> List[TranscriptTurn]:
    """Flag turns whose speaker matches the previous turn (rendered without a speaker prefix)."""
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
