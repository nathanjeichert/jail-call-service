"""
Base protocol and shared utilities for summarization engines.
"""

from typing import Dict, List, Optional, Protocol

from ..models import TranscriptTurn


# ── Shared utilities ──

def build_transcript_text(turns: List[TranscriptTurn]) -> str:
    """Convert TranscriptTurn list to plain text for the LLM.
    Each line is prefixed with a turn index so Gemini can reference specific turns."""
    lines = []
    for i, turn in enumerate(turns):
        ts = turn.timestamp or "[00:00]"
        lines.append(f"[{i}] {ts} {turn.speaker}: {turn.text}")
    return "\n".join(lines)


def build_full_prompt(prompt: str, transcript_text: str, metadata: Optional[dict] = None) -> str:
    """Assemble the full prompt from base prompt, metadata context, and transcript."""
    context_lines = []
    if metadata:
        if metadata.get("filename"):
            context_lines.append(f"File: {metadata['filename']}")
        if metadata.get("duration_seconds"):
            secs = int(metadata["duration_seconds"])
            context_lines.append(f"Duration: {secs // 60}:{secs % 60:02d}")

    context = "\n".join(context_lines)
    if context:
        return f"{prompt}\n\n{context}\n\nTRANSCRIPT:\n{transcript_text}"
    return f"{prompt}\n\nTRANSCRIPT:\n{transcript_text}"


# ── Engine protocol ──

class SummarizationEngine(Protocol):
    """Interface that all summarization engines must implement."""

    async def summarize(
        self,
        turns: List[TranscriptTurn],
        prompt: str,
        metadata: Optional[dict] = None,
    ) -> Dict:
        """
        Summarize a transcript and return structured results.

        Args:
            turns: Ordered list of TranscriptTurn objects.
            prompt: Summary instruction prompt.
            metadata: Optional dict with filename, duration, etc. for context.

        Returns:
            Dict with keys: text, input_tokens, output_tokens, thinking_tokens.
        """
        ...
