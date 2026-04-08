"""
Summarization engine factory.

Usage:
    from backend.summarization import get_engine
    engine = get_engine("gemma")
    result = await engine.summarize(turns, prompt, metadata)
"""

import logging

from .base import SummarizationEngine, build_transcript_text, build_full_prompt
from .gemma_engine import GEMMA_AVAILABLE

logger = logging.getLogger(__name__)

AVAILABLE_ENGINES = []
if GEMMA_AVAILABLE:
    AVAILABLE_ENGINES.append("gemma")


def get_engine(engine_name: str = "gemma", *, model: str | None = None) -> SummarizationEngine:
    """
    Return an initialized summarization engine by name.

    Args:
        engine_name: "gemma"
        model: Model name override.

    Raises:
        ValueError: Unknown engine name.
        RuntimeError: Engine dependencies not installed.
    """
    name = engine_name.lower().strip()

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
