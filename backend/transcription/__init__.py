"""Transcription engines.

    from backend.transcription import REGISTRY, get_engine
    engine = get_engine("assemblyai")   # or "parakeet"
    turns = await engine.transcribe(audio_path, channel_labels)

``REGISTRY`` describes every engine (label, local/cloud, readiness); see
``backend/engine_registry.py``. ``AVAILABLE_ENGINES`` lists the ids whose
dependencies are installed.
"""

from .. import config as cfg
from ..engine_registry import EngineRegistry, EngineSpec
from .assemblyai_engine import ASSEMBLYAI_AVAILABLE, AssemblyAIEngine
from .base import TranscriptionEngine
from .parakeet_engine import ParakeetEngine, _find_fluidaudiocli

__all__ = ["AVAILABLE_ENGINES", "REGISTRY", "TranscriptionEngine", "get_engine"]

REGISTRY = EngineRegistry("transcription", [
    EngineSpec(
        id="assemblyai",
        label="AssemblyAI (Cloud)",
        local=False,
        installed=lambda: ASSEMBLYAI_AVAILABLE,
        install_hint="Run: pip install assemblyai",
        requirement=lambda: None if cfg.ASSEMBLYAI_API_KEY else "ASSEMBLYAI_API_KEY is not set in .env",
        create=lambda: AssemblyAIEngine(
            api_key=cfg.ASSEMBLYAI_API_KEY,
            speech_model=cfg.ASSEMBLYAI_MODEL,
            polling_interval=cfg.ASSEMBLYAI_POLLING_INTERVAL,
        ),
    ),
    EngineSpec(
        id="parakeet",
        label="Parakeet (Local)",
        local=True,
        installed=lambda: _find_fluidaudiocli() is not None,
        install_hint="Place the binary at bin/fluidaudiocli or set FLUIDAUDIO_PATH",
        requirement=lambda: None if _find_fluidaudiocli() else "fluidaudiocli not found (bin/fluidaudiocli or FLUIDAUDIO_PATH)",
        create=lambda: ParakeetEngine(),
        max_concurrency=cfg.MAX_PARAKEET_CONCURRENT,
    ),
])

AVAILABLE_ENGINES = REGISTRY.available()


def get_engine(engine_name: str) -> TranscriptionEngine:
    """Return an initialized transcription engine by id.

    Raises ValueError for an unknown id and RuntimeError when the engine's
    dependencies or credentials are missing.
    """
    return REGISTRY.create(engine_name)
