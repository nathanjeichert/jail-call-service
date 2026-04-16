"""
Local Gemma 4 summarization engine via MLX on Apple Silicon.

Uses mlx-lm for native Metal-accelerated inference with the
Gemma 4 E2B model (4-bit quantized, ~3.6 GB RAM).
"""

import asyncio
import gc
import logging
import re
from typing import Dict, List, Optional

from ..models import TranscriptTurn
from .base import build_transcript_text, build_full_prompt

logger = logging.getLogger(__name__)

# Gemma 4 (unsloth MLX build) emits a reasoning channel before the final
# answer:  <|channel>thought ...reasoning... <channel|>FINAL_ANSWER
# We only want the final answer in the summary output; the thought trace
# is noisy and breaks downstream structured-section parsing. `_CLOSE_MARKER`
# matches the transition to the final channel. `_OPEN_MARKER` catches a
# stray opener in case the close marker was stripped upstream.
_CLOSE_MARKER = re.compile(r"<\s*channel\s*\|\s*>", re.IGNORECASE)
_OPEN_MARKER = re.compile(r"<\s*\|\s*channel\s*>\s*thought\b", re.IGNORECASE)

GEMMA_AVAILABLE = False
try:
    import mlx_lm  # noqa: F401
    GEMMA_AVAILABLE = True
except ImportError:
    pass


def _strip_thinking(text: str) -> str:
    """Remove Gemma's chain-of-thought prelude, keeping only the final answer.

    Raises RuntimeError when max_tokens ran out mid-thinking (no final channel
    produced) so the pipeline can record this as a partial failure instead of
    saving the raw reasoning as the summary.
    """
    close = list(_CLOSE_MARKER.finditer(text))
    if close:
        return text[close[-1].end():].lstrip()
    if _OPEN_MARKER.search(text):
        raise RuntimeError(
            "Gemma produced only chain-of-thought; max_tokens exhausted before "
            "the final answer was emitted."
        )
    return text.strip()


class GemmaEngine:
    """Local summarization via Gemma 4 E2B on Apple Silicon (MLX)."""

    def __init__(
        self,
        model_name: str = "unsloth/gemma-4-E2B-it-UD-MLX-4bit",
        max_tokens: int = 2048,
    ):
        if not GEMMA_AVAILABLE:
            raise RuntimeError("mlx-lm not installed. Run: pip install mlx-lm")
        self._model_name = model_name
        self._max_tokens = max_tokens
        self._model = None
        self._tokenizer = None

        from mlx_lm.sample_utils import make_sampler
        self._sampler = make_sampler(temp=0.3)

    def _ensure_loaded(self):
        """Lazy-load the model and run a warm-up pass to trigger Metal JIT compilation."""
        if self._model is not None:
            return
        from mlx_lm import load, stream_generate
        import mlx.core as mx

        logger.info("Loading Gemma model: %s", self._model_name)
        self._model, self._tokenizer = load(self._model_name)

        # Warm-up: trigger Metal kernel JIT compilation so the first real
        # call doesn't pay a 5-15s penalty.
        for _ in stream_generate(self._model, self._tokenizer, prompt="warmup", max_tokens=1):
            pass
        mx.clear_cache()
        logger.info("Gemma model loaded and warmed up")

    def unload(self):
        """Free model memory. Call after batch processing completes."""
        if self._model is None:
            return
        logger.info("Unloading Gemma model")
        del self._model
        del self._tokenizer
        self._model = None
        self._tokenizer = None
        gc.collect()
        try:
            import mlx.core as mx
            mx.clear_cache()
        except Exception:
            pass

    async def summarize(
        self,
        turns: List[TranscriptTurn],
        prompt: str,
        metadata: Optional[dict] = None,
    ) -> Dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._summarize_sync, turns, prompt, metadata)

    def _summarize_sync(
        self,
        turns: List[TranscriptTurn],
        prompt: str,
        metadata: Optional[dict] = None,
    ) -> Dict:
        from mlx_lm import stream_generate

        self._ensure_loaded()

        transcript_text = build_transcript_text(turns)
        full_prompt = build_full_prompt(prompt, transcript_text, metadata)

        messages = [
            {
                "role": "system",
                "content": "You are a legal analyst reviewing jail call transcripts. Follow the user's instructions precisely and produce structured analysis.",
            },
            {"role": "user", "content": full_prompt},
        ]

        prompt_text = self._tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False,
        )

        response = None
        text = ""
        for response in stream_generate(
            self._model, self._tokenizer, prompt=prompt_text,
            max_tokens=self._max_tokens,
            sampler=self._sampler,
        ):
            text += response.text

        input_tokens = response.prompt_tokens if response else 0
        output_tokens = response.generation_tokens if response else 0
        if not text.strip():
            raise RuntimeError("Gemma returned an empty summary response")

        final_text = _strip_thinking(text)
        if not final_text:
            raise RuntimeError("Gemma final-channel output was empty after stripping thinking trace")

        thinking_portion = text[: len(text) - len(final_text)]
        thinking_tokens = len(self._tokenizer.encode(thinking_portion)) if thinking_portion else 0
        logger.info(
            "Gemma tokens — input: %d, output: %d (thinking: %d)",
            input_tokens, output_tokens, thinking_tokens,
        )

        return {
            "text": final_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
        }
