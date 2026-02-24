"""
Gemini Flash summarization for jail call transcripts.

Text-only: sends transcript text + prompt, no audio upload.
Concurrency is controlled by the pipeline's own semaphore.
"""

import asyncio
import logging
import os
from typing import List, Optional
from tenacity import retry, wait_random_exponential, stop_after_attempt

from . import config as cfg

logger = logging.getLogger(__name__)


def _build_transcript_text(turns) -> str:
    """Convert TranscriptTurn list to plain text for the LLM."""
    lines = []
    for turn in turns:
        ts = turn.timestamp or "[00:00]"
        lines.append(f"{ts} {turn.speaker}: {turn.text}")
    return "\n".join(lines)


async def summarize_transcript(
    turns,
    prompt: str = None,
    metadata: Optional[dict] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """
    Summarize a single transcript asynchronously.

    Args:
        turns: List of TranscriptTurn objects
        prompt: Summary instruction
        metadata: Optional dict with filename, duration, etc. for context
        api_key: Gemini API key (falls back to env var)
        model: Model name (falls back to config)

    Returns:
        Summary string
    """
    api_key = api_key or cfg.GEMINI_API_KEY
    prompt = prompt or cfg.DEFAULT_SUMMARY_PROMPT
    model = model or cfg.GEMINI_MODEL

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as exc:
        raise RuntimeError("google-genai not installed. Run: pip install google-genai") from exc

    transcript_text = _build_transcript_text(turns)

    context_lines = []
    if metadata:
        if metadata.get("filename"):
            context_lines.append(f"File: {metadata['filename']}")
        if metadata.get("duration_seconds"):
            secs = int(metadata["duration_seconds"])
            context_lines.append(f"Duration: {secs // 60}:{secs % 60:02d}")

    context = "\n".join(context_lines)
    full_prompt = f"{prompt}\n\n{context}\n\nTRANSCRIPT:\n{transcript_text}" if context else f"{prompt}\n\nTRANSCRIPT:\n{transcript_text}"

    client = genai.Client(api_key=api_key)

    @retry(
        wait=wait_random_exponential(min=2, max=60),
        stop=stop_after_attempt(6)
    )
    async def _call_gemini():
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=model,
                contents=full_prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=800,
                    safety_settings=[
                        genai_types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
                        genai_types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
                    ],
                ),
            ),
        )

    try:
        response = await _call_gemini()
        text = getattr(response, "text", None)
        if not text and getattr(response, "candidates", None):
            try:
                text = response.candidates[0].content.parts[0].text
            except Exception:
                text = None
        return (text or "").strip()
    finally:
        try:
            client.close()
        except Exception:
            pass


async def batch_summarize(
    calls,
    prompt: str = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    progress_callback=None,
) -> List[str]:
    """
    Summarize a list of CallResult objects in parallel (rate-limited).

    Returns list of summary strings in same order as calls.
    """
    results = [""] * len(calls)
    total = len(calls)

    async def summarize_one(i, call):
        if not call.turns:
            return i, ""
        try:
            summary = await summarize_transcript(
                call.turns,
                prompt=prompt,
                metadata={"filename": call.filename, "duration_seconds": call.duration_seconds},
                api_key=api_key,
                model=model,
            )
            return i, summary
        except Exception as e:
            logger.error("Summarization failed for call %d (%s): %s", i, call.filename, e)
            return i, f"[Summarization failed: {e}]"

    tasks = [summarize_one(i, call) for i, call in enumerate(calls)]

    completed = 0
    for coro in asyncio.as_completed(tasks):
        i, summary = await coro
        results[i] = summary
        completed += 1
        if progress_callback:
            progress_callback(completed, total)

    return results
