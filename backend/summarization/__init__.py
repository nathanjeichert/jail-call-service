"""
Summarization engine factory.

Usage:
    from backend.summarization import get_engine
    engine = get_engine("gemini")   # or "qwen"
    result = await engine.summarize(turns, prompt, metadata)
"""

import logging
from typing import Optional

from .base import SummarizationEngine, build_transcript_text, build_full_prompt
from .gemini_engine import GeminiEngine, GEMINI_AVAILABLE
from .qwen_engine import QWEN_AVAILABLE

logger = logging.getLogger(__name__)

AVAILABLE_ENGINES = []
if GEMINI_AVAILABLE:
    AVAILABLE_ENGINES.append("gemini")
if QWEN_AVAILABLE:
    AVAILABLE_ENGINES.append("qwen")


def get_engine(
    engine_name: str,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> SummarizationEngine:
    """
    Return an initialized summarization engine by name.

    Args:
        engine_name: "gemini" or "qwen"
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

    if name == "qwen":
        if not QWEN_AVAILABLE:
            raise RuntimeError(
                "mlx-lm not installed. Run: pip install mlx-lm"
            )
        from .qwen_engine import QwenEngine
        from .. import config as cfg
        return QwenEngine(
            model_name=model or cfg.QWEN_MODEL,
            max_tokens=cfg.QWEN_MAX_TOKENS,
            max_kv_size=cfg.QWEN_MAX_KV_SIZE,
        )

    raise ValueError(
        f"Unknown summarization engine: {engine_name!r}. "
        f"Available: {', '.join(AVAILABLE_ENGINES) or 'none'}"
    )
