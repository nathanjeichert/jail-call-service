"""
Gemini Flash summarization engine for jail call transcripts.

Text-only: sends transcript text + prompt, no audio upload.
"""

import asyncio
import json
import logging
from typing import Dict, List, Optional, Type, TypeVar
from tenacity import retry, wait_random_exponential, stop_after_attempt

from pydantic import BaseModel

from .. import config as cfg
from ..models import TranscriptTurn
from .base import build_transcript_text, build_full_prompt

logger = logging.getLogger(__name__)
SchemaT = TypeVar("SchemaT", bound=BaseModel)

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

    async def _request(
        self,
        prompt_text: str,
        *,
        response_json_schema: Optional[dict] = None,
        thinking_level: Optional[str] = None,
    ):
        @retry(
            wait=wait_random_exponential(min=2, max=60),
            stop=stop_after_attempt(6),
        )
        async def _call_gemini():
            loop = asyncio.get_running_loop()

            def _do_request():
                config_kwargs = {
                    "temperature": 1.0,
                    "max_output_tokens": 8192,
                    "thinking_config": genai_types.ThinkingConfig(
                        thinking_level=(thinking_level or cfg.GEMINI_SYSTEM_AUDIO_THINKING_LEVEL),
                    ),
                    "safety_settings": [
                        genai_types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
                    ],
                }
                if response_json_schema is not None:
                    config_kwargs["response_mime_type"] = "application/json"
                    config_kwargs["response_json_schema"] = response_json_schema

                return self._client.models.generate_content(
                    model=self._model,
                    contents=prompt_text,
                    config=genai_types.GenerateContentConfig(**config_kwargs),
                )

            return await loop.run_in_executor(None, _do_request)

        return await _call_gemini()

    @staticmethod
    def _extract_text(response) -> str:
        text = getattr(response, "text", None)
        if not text and getattr(response, "candidates", None):
            try:
                text = response.candidates[0].content.parts[0].text
            except Exception:
                text = None
        if not text or not str(text).strip():
            raise RuntimeError("Gemini returned an empty response")
        return str(text).strip()

    @staticmethod
    def _usage_dict(response) -> Dict[str, int]:
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
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
        }

    async def summarize(
        self,
        turns: List[TranscriptTurn],
        prompt: str,
        metadata: Optional[dict] = None,
    ) -> Dict:
        duration = float((metadata or {}).get("duration_seconds") or 0.0)
        transcript_text = build_transcript_text(turns, duration)
        full_prompt = build_full_prompt(prompt, transcript_text, metadata)
        return await self.generate(
            full_prompt,
            thinking_level=cfg.GEMINI_SUMMARY_THINKING_LEVEL,
        )

    async def generate(self, prompt_text: str, thinking_level: Optional[str] = None) -> Dict:
        response = await self._request(
            prompt_text,
            thinking_level=thinking_level,
        )
        text = self._extract_text(response)
        usage = self._usage_dict(response)

        return {
            "text": text,
            **usage,
        }

    async def generate_json(
        self,
        prompt_text: str,
        schema_model: Type[SchemaT],
        thinking_level: Optional[str] = None,
    ) -> Dict:
        response = await self._request(
            prompt_text,
            response_json_schema=schema_model.model_json_schema(),
            thinking_level=thinking_level,
        )
        text = self._extract_text(response)
        usage = self._usage_dict(response)

        try:
            parsed = schema_model.model_validate_json(text)
        except Exception as exc:
            logger.warning("Gemini structured response failed validation: %s", exc)
            try:
                parsed = schema_model.model_validate(json.loads(text))
            except Exception:
                raise RuntimeError(f"Gemini returned invalid structured JSON: {exc}") from exc

        return {
            "text": text,
            "parsed": parsed,
            **usage,
        }
