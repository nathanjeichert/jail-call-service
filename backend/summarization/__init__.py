"""
Summarization engine factory.

Usage:
    from backend.summarization import get_engine
    engine = get_engine("gemini")   # or "gemma"
    result = await engine.summarize(turns, prompt, metadata)
"""

import logging
from typing import Optional

from .base import SummarizationEngine, build_transcript_text, build_full_prompt
from .gemini_engine import GeminiEngine, GEMINI_AVAILABLE
from .gemma_engine import GEMMA_AVAILABLE

logger = logging.getLogger(__name__)

AVAILABLE_ENGINES = []
if GEMINI_AVAILABLE:
    AVAILABLE_ENGINES.append("gemini")
if GEMMA_AVAILABLE:
    AVAILABLE_ENGINES.append("gemma")


def get_engine(
    engine_name: str,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> SummarizationEngine:
    """
    Return an initialized summarization engine by name.

    Args:
        engine_name: "gemini" or "gemma"
        api_key: Required for Gemini.
        model: Model name override.

    Raises:
        ValueError: Unknown engine name.
        RuntimeError: Engine dependencies not installed.
    """
    name = engine_name.lower().strip()

    if name == "gemini":
        if not GEMINI_AVAILABLE:
            raise RuntimeError(
                "google-genai not installed. Run: pip install google-genai"
            )
        from .. import config as cfg
        return GeminiEngine(
            api_key=api_key or cfg.GEMINI_API_KEY,
            model=model or cfg.GEMINI_MODEL,
        )

    if name == "gemma":
        if not GEMMA_AVAILABLE:
            raise RuntimeError(
                "mlx-lm not installed. Run: pip install mlx-lm"
            )
        from .gemma_engine import GemmaEngine
        from .. import config as cfg
        return GemmaEngine(
            model_name=model or cfg.GEMMA_MODEL,
            max_tokens=cfg.GEMMA_MAX_TOKENS,
        )

    raise ValueError(
        f"Unknown summarization engine: {engine_name!r}. "
        f"Available: {', '.join(AVAILABLE_ENGINES) or 'none'}"
    )
