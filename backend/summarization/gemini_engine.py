"""Gemini Flash summarization engine (cloud, native JSON structured output).

Text-only: sends transcript text + prompt, no audio upload. Automated
telecom messages are detected in a separate structured pass before the
summary call, so the summary always sees a filtered transcript.
"""

import asyncio
import json
import logging
from typing import List, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_random_exponential

from .. import config as cfg
from ..models import TranscriptTurn
from .base import (
    CallSummary,
    CaseReportInputs,
    SummarizationEngine,
    TokenUsage,
    build_full_prompt,
    build_transcript_text,
    build_turn_transcript_text,
    metadata_duration,
)
from .json_protocol import (
    CASE_REPORT_SYNTHESIS_JSON_PROMPT,
    SUMMARY_JSON_INSTRUCTIONS,
    SYSTEM_AUDIO_DETECTION_JSON_PROMPT,
)
from .schemas import CaseReportResponse, SummaryResponse, SystemAudioResponse

logger = logging.getLogger(__name__)
SchemaT = TypeVar("SchemaT", bound=BaseModel)

try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False


class GeminiEngine(SummarizationEngine):
    """Cloud summarization via Google Gemini Flash."""

    name = "gemini"
    system_audio_prepass = True

    def __init__(self, api_key: str, model: str = "gemini-3-flash-preview"):
        if not GEMINI_AVAILABLE:
            raise RuntimeError("google-genai not installed. Run: pip install google-genai")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not configured")
        self._client = genai.Client(api_key=api_key)
        self._model = model

    # ── transport ──

    async def _request(self, prompt_text: str, *, response_json_schema: dict, thinking_level: str):
        @retry(wait=wait_random_exponential(min=2, max=60), stop=stop_after_attempt(6))
        async def _call_gemini():
            loop = asyncio.get_running_loop()

            def _do_request():
                config = genai_types.GenerateContentConfig(
                    temperature=1.0,
                    max_output_tokens=8192,
                    thinking_config=genai_types.ThinkingConfig(thinking_level=thinking_level),
                    safety_settings=[
                        genai_types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
                    ],
                    response_mime_type="application/json",
                    response_json_schema=response_json_schema,
                )
                return self._client.models.generate_content(
                    model=self._model, contents=prompt_text, config=config,
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
    def _usage(response) -> TokenUsage:
        meta = getattr(response, "usage_metadata", None)
        usage = TokenUsage(
            input_tokens=getattr(meta, "prompt_token_count", None) or 0,
            output_tokens=getattr(meta, "candidates_token_count", None) or 0,
            thinking_tokens=getattr(meta, "thoughts_token_count", None) or 0,
        )
        logger.info(
            "Gemini tokens — input: %d, output: %d, thinking: %d",
            usage.input_tokens, usage.output_tokens, usage.thinking_tokens,
        )
        return usage

    async def _generate_json(
        self,
        prompt_text: str,
        schema_model: Type[SchemaT],
        thinking_level: str,
    ) -> Tuple[SchemaT, TokenUsage]:
        response = await self._request(
            prompt_text,
            response_json_schema=schema_model.model_json_schema(),
            thinking_level=thinking_level,
        )
        text = self._extract_text(response)
        usage = self._usage(response)
        try:
            parsed = schema_model.model_validate_json(text)
        except Exception as exc:
            logger.warning("Gemini structured response failed validation: %s", exc)
            try:
                parsed = schema_model.model_validate(json.loads(text))
            except Exception:
                raise RuntimeError(f"Gemini returned invalid structured JSON: {exc}") from exc
        return parsed, usage

    # ── engine interface ──

    async def detect_system_audio(
        self,
        turns: List[TranscriptTurn],
        metadata: Optional[dict] = None,
    ) -> Tuple[List[dict], TokenUsage]:
        prompt = build_full_prompt(
            SYSTEM_AUDIO_DETECTION_JSON_PROMPT, build_turn_transcript_text(turns), metadata,
        )
        parsed, usage = await self._generate_json(
            prompt, SystemAudioResponse, cfg.GEMINI_SYSTEM_AUDIO_THINKING_LEVEL,
        )
        return [{"turn": m.turn, "text": m.text} for m in parsed.system_audio], usage

    async def summarize_call(
        self,
        turns: List[TranscriptTurn],
        prompt: str,
        metadata: Optional[dict] = None,
        *,
        detect_system_audio: bool = False,
    ) -> CallSummary:
        full_prompt = build_full_prompt(
            f"{prompt}\n\n{SUMMARY_JSON_INSTRUCTIONS}",
            build_transcript_text(turns, metadata_duration(metadata)),
            metadata,
        )
        parsed, usage = await self._generate_json(
            full_prompt, SummaryResponse, cfg.GEMINI_SUMMARY_THINKING_LEVEL,
        )
        return CallSummary(usage=usage, structured=parsed)

    async def synthesize_case_report(
        self,
        inputs: CaseReportInputs,
    ) -> Tuple[Optional[CaseReportResponse], TokenUsage]:
        prompt = CASE_REPORT_SYNTHESIS_JSON_PROMPT.format(**inputs.format_kwargs())
        return await self._generate_json(prompt, CaseReportResponse, cfg.GEMINI_CASE_REPORT_THINKING_LEVEL)
