"""
Local Qwen summarization engine via MLX on Apple Silicon.

Uses mlx-lm for native Metal-accelerated inference with the
Qwen 3.5 4B model (4-bit quantized, ~3 GB RAM).
"""

import asyncio
import gc
import logging
from typing import Dict, List, Optional

from ..models import TranscriptTurn
from .base import build_transcript_text, build_full_prompt

logger = logging.getLogger(__name__)

QWEN_AVAILABLE = False
try:
    import mlx_lm  # noqa: F401
    QWEN_AVAILABLE = True
except ImportError:
    pass


class QwenEngine:
    """Local summarization via Qwen 3.5 on Apple Silicon (MLX)."""

    def __init__(
        self,
        model_name: str = "mlx-community/Qwen3.5-4B-MLX-4bit",
        max_tokens: int = 1024,
    ):
        if not QWEN_AVAILABLE:
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

        logger.info("Loading Qwen model: %s", self._model_name)
        self._model, self._tokenizer = load(self._model_name)

        # Warm-up: trigger Metal kernel JIT compilation so the first real
        # call doesn't pay a 5-15s penalty.
        for _ in stream_generate(self._model, self._tokenizer, prompt="warmup", max_tokens=1):
            pass
        mx.clear_cache()
        logger.info("Qwen model loaded and warmed up")

    def unload(self):
        """Free model memory. Call after batch processing completes."""
        if self._model is None:
            return
        logger.info("Unloading Qwen model")
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

        # Disable Qwen 3.5's built-in thinking mode to avoid wasting tokens
        # on chain-of-thought reasoning before the structured output.
        template_kwargs = {"add_generation_prompt": True, "tokenize": False}
        try:
            prompt_text = self._tokenizer.apply_chat_template(
                messages, enable_thinking=False, **template_kwargs,
            )
        except TypeError:
            prompt_text = self._tokenizer.apply_chat_template(
                messages, **template_kwargs,
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

        logger.info(
            "Qwen tokens — input: %d, output: %d",
            input_tokens, output_tokens,
        )

        return {
            "text": text.strip(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": 0,
        }
