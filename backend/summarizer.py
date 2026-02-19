"""
Gemini Flash summarization for jail call transcripts.

Text-only: sends transcript text + prompt, no audio upload.
Uses asyncio.Semaphore for rate limiting parallel calls.
"""

import asyncio
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "Summarize this jail call transcript. Note key topics, mentions of the case, "
    "legal matters, names, dates, locations. Keep under 300 words."
)

_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _get_semaphore(limit: int = 10) -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(limit)
    return _SEMAPHORE


def _build_transcript_text(turns) -> str:
    """Convert TranscriptTurn list to plain text for the LLM."""
    lines = []
    for turn in turns:
        lines.append(f"{turn.speaker}: {turn.text}")
    return "\n".join(lines)


async def summarize_transcript(
    turns,
    prompt: str = DEFAULT_PROMPT,
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
    from . import config as cfg

    api_key = api_key or cfg.GEMINI_API_KEY
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

    sem = _get_semaphore(10)
    async with sem:
        client = genai.Client(api_key=api_key)
        try:
            # Run sync SDK call in thread to avoid blocking event loop
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=model,
                    contents=full_prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=600,
                    ),
                ),
            )
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
    prompt: str = DEFAULT_PROMPT,
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
