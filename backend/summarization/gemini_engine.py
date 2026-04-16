"""
Gemini Flash summarization engine for jail call transcripts.

Text-only: sends transcript text + prompt, no audio upload.
"""

import asyncio
import logging
from typing import Dict, List, Optional
from tenacity import retry, wait_random_exponential, stop_after_attempt

from ..models import TranscriptTurn
from .base import build_transcript_text, build_full_prompt

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False


class GeminiEngine:
    """Cloud summarization via Google Gemini Flash."""

    def __init__(self, api_key: str, model: str = "gemini-3-flash-preview"):
        if not GEMINI_AVAILABLE:
            raise RuntimeError("google-genai not installed. Run: pip install google-genai")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not configured")
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def summarize(
        self,
        turns: List[TranscriptTurn],
        prompt: str,
        metadata: Optional[dict] = None,
    ) -> Dict:
        transcript_text = build_transcript_text(turns)
        full_prompt = build_full_prompt(prompt, transcript_text, metadata)
        return await self.generate(full_prompt)

    async def generate(self, prompt_text: str) -> Dict:
        @retry(
            wait=wait_random_exponential(min=2, max=60),
            stop=stop_after_attempt(6),
        )
        async def _call_gemini():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                lambda: self._client.models.generate_content(
                    model=self._model,
                    contents=prompt_text,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=8192,
                        thinking_config=genai_types.ThinkingConfig(
                            thinking_level="low",
                        ),
                        safety_settings=[
                            genai_types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
                            genai_types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
                            genai_types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
                            genai_types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
                        ],
                    ),
                ),
            )

        response = await _call_gemini()
        text = getattr(response, "text", None)
        if not text and getattr(response, "candidates", None):
            try:
                text = response.candidates[0].content.parts[0].text
            except Exception:
                text = None
        if not text or not str(text).strip():
            raise RuntimeError("Gemini returned an empty response")

        usage = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", None) or 0
        output_tokens = getattr(usage, "candidates_token_count", None) or 0
        thinking_tokens = getattr(usage, "thoughts_token_count", None) or 0

        logger.info(
            "Gemini tokens — input: %d, output: %d, thinking: %d, total: %d",
            input_tokens, output_tokens, thinking_tokens,
            input_tokens + output_tokens + thinking_tokens,
        )

        return {
            "text": str(text).strip(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
        }
