"""
Transcription engine factory.

Usage:
    from backend.transcription import get_engine
    engine = get_engine("assemblyai")   # or "parakeet"
    turns = await engine.transcribe(audio_path, channel_labels)
"""

import logging

from .. import config as cfg
from .base import TranscriptionEngine
from .assemblyai_engine import AssemblyAIEngine, ASSEMBLYAI_AVAILABLE
from .parakeet_engine import ParakeetEngine, _find_fluidaudiocli

logger = logging.getLogger(__name__)

__all__ = [
    "AVAILABLE_ENGINES",
    "get_engine",
    "TranscriptionEngine",
]

AVAILABLE_ENGINES = []
if ASSEMBLYAI_AVAILABLE:
    AVAILABLE_ENGINES.append("assemblyai")
if _find_fluidaudiocli():
    AVAILABLE_ENGINES.append("parakeet")


def get_engine(engine_name: str) -> TranscriptionEngine:
    """Return an initialized transcription engine by name ("assemblyai" or "parakeet").

    Raises ValueError for an unknown name and RuntimeError when the engine's
    dependencies or credentials are missing.
    """
    name = engine_name.lower().strip()

    if name == "assemblyai":
        if not ASSEMBLYAI_AVAILABLE:
            raise RuntimeError("AssemblyAI SDK not installed. Run: pip install assemblyai")
        if not cfg.ASSEMBLYAI_API_KEY:
            raise RuntimeError("AssemblyAI requires an API key. Set ASSEMBLYAI_API_KEY in your .env file.")
        return AssemblyAIEngine(
            api_key=cfg.ASSEMBLYAI_API_KEY,
            speech_model=cfg.ASSEMBLYAI_MODEL,
            polling_interval=cfg.ASSEMBLYAI_POLLING_INTERVAL,
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
