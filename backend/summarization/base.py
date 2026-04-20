"""
Base protocol and shared utilities for summarization engines.
"""

from typing import Dict, List, Optional, Protocol

from ..models import TranscriptTurn


# ── Shared utilities ──

def _seconds_to_timestamp_label(seconds: float) -> str:
    total = max(int(seconds or 0), 0)
    mins, secs = divmod(total, 60)
    return f"{mins:02d}:{secs:02d}"


def build_transcript_text(turns: List[TranscriptTurn], audio_duration: float = 0.0) -> str:
    """Convert transcript turns into cited transcript lines for the LLM.

    The summarization prompt asks the model to cite transcript lines in
    ``[Page:Line]`` format. We therefore feed it the exact same wrapped line
    layout the transcript PDF/viewer/search surfaces use later.
    """
    from ..transcript_formatting import compute_line_entries

    if not turns:
        return ""

    line_entries = compute_line_entries(turns, audio_duration)
    lines = []
    for entry in line_entries:
        turn_index = int(entry.get("turn_index", 0) or 0)
        page = int(entry.get("page", 0) or 0)
        line = int(entry.get("line", 0) or 0)
        start = float(entry.get("start", 0) or 0)
        speaker = str(entry.get("speaker", "SPEAKER")).strip() or "SPEAKER"
        text = str(entry.get("text", "")).strip()
        lines.append(
            f"[{turn_index}] [{page}:{line}] [{_seconds_to_timestamp_label(start)}] {speaker}: {text}"
        )
    return "\n".join(lines)


def build_turn_transcript_text(turns: List[TranscriptTurn]) -> str:
    """Convert transcript turns into a simple turn-indexed transcript."""
    if not turns:
        return ""

    lines = []
    for turn_index, turn in enumerate(turns):
        speaker = (turn.speaker or "SPEAKER").strip() or "SPEAKER"
        timestamp = (turn.timestamp or "[00:00]").strip() or "[00:00]"
        text = (turn.text or "").strip()
        lines.append(f"[{turn_index}] {timestamp} {speaker}: {text}")
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

    async def generate(self, prompt_text: str) -> Dict:
        """Run a single-turn prompt that is not a transcript summary.

        Used by features that need the same model for bespoke one-shot calls
        (case-report synthesis, Gemini system-audio detection, and the Gemma
        legacy combined summary flow), so cloud and local engines stay
        interchangeable.

        Args:
            prompt_text: Fully-assembled user prompt.

        Returns:
            Dict with keys: text, input_tokens, output_tokens, thinking_tokens.
        """
        ...
