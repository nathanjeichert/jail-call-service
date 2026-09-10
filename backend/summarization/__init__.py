"""Summarization engines.

    from backend.summarization import REGISTRY, get_engine
    engine = get_engine("gemini")   # or "gemma"
    result = await engine.summarize_call(turns, prompt, metadata)

``REGISTRY`` describes every engine (label, local/cloud, readiness); see
``backend/engine_registry.py``. ``AVAILABLE_ENGINES`` lists the ids whose
dependencies are installed. See ``base.py`` for the interface every engine
implements.
"""

from typing import Optional

from .. import config as cfg
from ..engine_registry import EngineRegistry, EngineSpec
from .base import CallSummary, CaseReportInputs, SummarizationEngine, TokenUsage
from .gemini_engine import GEMINI_AVAILABLE, GeminiEngine
from .gemma_engine import GEMMA_AVAILABLE

__all__ = [
    "AVAILABLE_ENGINES",
    "REGISTRY",
    "CallSummary",
    "CaseReportInputs",
    "SummarizationEngine",
    "TokenUsage",
    "get_engine",
]


def _create_gemma(model: Optional[str] = None):
    from .gemma_engine import GemmaEngine
    return GemmaEngine(model_name=model or cfg.GEMMA_MODEL, max_tokens=cfg.GEMMA_MAX_TOKENS)


REGISTRY = EngineRegistry("summarization", [
    EngineSpec(
        id="gemini",
        label="Gemini (Cloud)",
        local=False,
        installed=lambda: GEMINI_AVAILABLE,
        install_hint="Run: pip install google-genai",
        requirement=lambda: None if cfg.GEMINI_API_KEY else "GEMINI_API_KEY is not set in .env",
        create=lambda model=None: GeminiEngine(api_key=cfg.GEMINI_API_KEY, model=model or cfg.GEMINI_MODEL),
    ),
    EngineSpec(
        id="gemma",
        label="Gemma 4 E2B (Local)",
        local=True,
        installed=lambda: GEMMA_AVAILABLE,
        install_hint="Run: pip install -e '.[local]'",
        requirement=lambda: None,
        create=_create_gemma,
        max_concurrency=cfg.MAX_GEMMA_CONCURRENT,
    ),
])

AVAILABLE_ENGINES = REGISTRY.available()


def get_engine(engine_name: str, *, model: Optional[str] = None) -> SummarizationEngine:
    """Return an initialized summarization engine by id.

    Raises ValueError for an unknown id and RuntimeError when the engine's
    dependencies or credentials are missing.
    """
    return REGISTRY.create(engine_name, model=model) if model else REGISTRY.create(engine_name)
