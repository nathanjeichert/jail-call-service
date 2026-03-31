"""
Transcription engine factory.

Usage:
    from backend.transcription import get_engine
    engine = get_engine("assemblyai")   # or "parakeet"
    turns = await engine.transcribe(audio_path, channel_labels)
"""

import logging
from typing import Optional

from .base import TranscriptionEngine, normalize_speaker_label, mark_continuation_turns
from .assemblyai_engine import AssemblyAIEngine, ASSEMBLYAI_AVAILABLE
from .parakeet_engine import ParakeetEngine, _find_fluidaudiocli

logger = logging.getLogger(__name__)

AVAILABLE_ENGINES = []
if ASSEMBLYAI_AVAILABLE:
    AVAILABLE_ENGINES.append("assemblyai")
if _find_fluidaudiocli():
    AVAILABLE_ENGINES.append("parakeet")


def get_engine(
    engine_name: str,
    *,
    api_key: Optional[str] = None,
    speech_model: Optional[str] = None,
    polling_interval: Optional[int] = None,
) -> TranscriptionEngine:
    """
    Return an initialized transcription engine by name.

    Args:
        engine_name: "assemblyai" or "parakeet"
        api_key: Required for AssemblyAI.
        speech_model: AssemblyAI model name (default: universal-3-pro).
        polling_interval: AssemblyAI polling interval in seconds.

    Raises:
        ValueError: Unknown engine name.
        RuntimeError: Engine dependencies not installed.
    """
    name = engine_name.lower().strip()

    if name == "assemblyai":
        if not ASSEMBLYAI_AVAILABLE:
            raise RuntimeError(
                "AssemblyAI SDK not installed. Run: pip install assemblyai"
            )
        if not api_key:
            raise RuntimeError(
                "AssemblyAI requires an API key. Set ASSEMBLYAI_API_KEY in your .env file."
            )
        return AssemblyAIEngine(
            api_key=api_key,
            speech_model=speech_model or "universal-3-pro",
            polling_interval=polling_interval or 15,
        )

    if name == "parakeet":
        if not _find_fluidaudiocli():
            raise RuntimeError(
                "fluidaudiocli not found. Place the binary in bin/fluidaudiocli "
                "or set the FLUIDAUDIO_PATH environment variable."
            )
        return ParakeetEngine()

    raise ValueError(
        f"Unknown transcription engine: {engine_name!r}. "
        f"Available: assemblyai, parakeet"
    )
