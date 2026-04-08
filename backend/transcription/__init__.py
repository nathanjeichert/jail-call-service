"""
Transcription engine factory.

Usage:
    from backend.transcription import get_engine
    engine = get_engine("parakeet")
    turns = await engine.transcribe(audio_path, channel_labels)
"""

import logging

from .base import TranscriptionEngine, normalize_speaker_label, mark_continuation_turns
from .parakeet_engine import ParakeetEngine, _find_fluidaudiocli

logger = logging.getLogger(__name__)

AVAILABLE_ENGINES = []
if _find_fluidaudiocli():
    AVAILABLE_ENGINES.append("parakeet")


def get_engine(engine_name: str = "parakeet") -> TranscriptionEngine:
    """
    Return an initialized transcription engine by name.

    Args:
        engine_name: "parakeet"

    Raises:
        ValueError: Unknown engine name.
        RuntimeError: Engine dependencies not installed.
    """
    name = engine_name.lower().strip()

    if name == "parakeet":
        if not _find_fluidaudiocli():
            raise RuntimeError(
                "fluidaudiocli not found. Place the binary in bin/fluidaudiocli "
                "or set the FLUIDAUDIO_PATH environment variable."
            )
        return ParakeetEngine()

    raise ValueError(
        f"Unknown transcription engine: {engine_name!r}. "
        f"Available: {', '.join(AVAILABLE_ENGINES) or 'none'}"
    )
