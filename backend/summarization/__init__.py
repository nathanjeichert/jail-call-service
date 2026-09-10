"""Summarization engines.

    from backend.summarization import get_engine
    engine = get_engine("gemini")   # or "gemma"
    result = await engine.summarize_call(turns, prompt, metadata)

``AVAILABLE_ENGINES`` lists the engines whose dependencies are installed.
See ``base.py`` for the interface every engine implements.
"""

from typing import Optional

from .. import config as cfg
from .base import CallSummary, CaseReportInputs, SummarizationEngine, TokenUsage
from .gemini_engine import GEMINI_AVAILABLE, GeminiEngine
from .gemma_engine import GEMMA_AVAILABLE

__all__ = [
    "AVAILABLE_ENGINES",
    "CallSummary",
    "CaseReportInputs",
    "SummarizationEngine",
    "TokenUsage",
    "get_engine",
]

AVAILABLE_ENGINES = []
if GEMINI_AVAILABLE:
    AVAILABLE_ENGINES.append("gemini")
if GEMMA_AVAILABLE:
    AVAILABLE_ENGINES.append("gemma")


def get_engine(engine_name: str, *, model: Optional[str] = None) -> SummarizationEngine:
    """Return an initialized summarization engine by name.

    Raises ValueError for an unknown name and RuntimeError when the engine's
    dependencies are not installed.
    """
    name = engine_name.lower().strip()

    if name == "gemini":
        if not GEMINI_AVAILABLE:
            raise RuntimeError("google-genai not installed. Run: pip install google-genai")
        return GeminiEngine(api_key=cfg.GEMINI_API_KEY, model=model or cfg.GEMINI_MODEL)

    if name == "gemma":
        if not GEMMA_AVAILABLE:
            raise RuntimeError("mlx-lm not installed. Run: pip install mlx-lm")
        from .gemma_engine import GemmaEngine
        return GemmaEngine(model_name=model or cfg.GEMMA_MODEL, max_tokens=cfg.GEMMA_MAX_TOKENS)

    raise ValueError(
        f"Unknown summarization engine: {engine_name!r}. "
        f"Available: {', '.join(AVAILABLE_ENGINES) or 'none'}"
    )
