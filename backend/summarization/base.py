"""Summarization engine interface and the shared prompt-assembly helpers.

An engine is anything that can turn a transcript into the app's summary
products. The pipeline and the case report only ever talk to this interface;
how an engine gets structured output out of its model (native JSON mode,
a text block protocol, ...) is the engine's own business. See
``json_protocol.py`` (Gemini) and ``text_protocol.py`` (Gemma) for the two
existing protocols.

Three operations, all async:

* :meth:`SummarizationEngine.detect_system_audio` - optional separate pass
  that flags automated telecom messages *before* summarization, so the
  summary sees a filtered transcript. Engines that can do this set
  ``system_audio_prepass = True``. Engines that cannot (one model call per
  call is expensive locally) detect inline during :meth:`summarize_call`
  and return the markers on the :class:`CallSummary` instead.
* :meth:`SummarizationEngine.summarize_call` - per-call summary.
* :meth:`SummarizationEngine.synthesize_case_report` - case-level findings
  and outside-party identity inference in one call.

Every operation reports :class:`TokenUsage` so cost tracking stays uniform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from ..formatting import format_timestamp
from ..models import TranscriptTurn
from ..transcript_layout import compute_line_entries
from .schemas import CaseReportResponse, SummaryResponse

# ────────────────────────── Result types ──────────────────────────

@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.thinking_tokens + other.thinking_tokens,
        )

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)


@dataclass
class CallSummary:
    """What an engine returns for one call.

    Exactly one of ``structured`` / ``text`` is set. Structured results are
    curated by ``summaries.normalize_structured_summary``; text results go
    through ``summaries.normalize_summary_text``. ``system_audio_markers`` is
    populated only by engines that detect automated messages inline.
    """
    usage: TokenUsage
    structured: Optional[SummaryResponse] = None
    text: Optional[str] = None
    system_audio_markers: List[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CaseReportInputs:
    """Pre-formatted synthesis inputs; engines substitute them into their own prompt."""
    case_context: str
    calls_block: str
    numbers_block: str
    min_findings: int
    max_findings: int

    def format_kwargs(self) -> Dict[str, object]:
        return asdict(self)


# ────────────────────────── Prompt assembly ──────────────────────────

def build_transcript_text(turns: List[TranscriptTurn], audio_duration: float = 0.0) -> str:
    """Cited transcript lines for the LLM.

    The summarization prompt asks the model to cite transcript lines in
    ``[Page:Line]`` format, so it is fed the exact wrapped line layout the
    transcript PDF, viewer, and search page use later.
    """
    if not turns:
        return ""

    lines = []
    for entry in compute_line_entries(turns, audio_duration):
        turn_index = int(entry.get("turn_index", 0) or 0)
        page = int(entry.get("page", 0) or 0)
        line = int(entry.get("line", 0) or 0)
        start = float(entry.get("start", 0) or 0)
        speaker = str(entry.get("speaker", "SPEAKER")).strip() or "SPEAKER"
        text = str(entry.get("text", "")).strip()
        lines.append(
            f"[{turn_index}] [{page}:{line}] [{format_timestamp(start, brackets=False)}] {speaker}: {text}"
        )
    return "\n".join(lines)


def build_turn_transcript_text(turns: List[TranscriptTurn]) -> str:
    """Simple turn-indexed transcript (one line per turn) for detection passes."""
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


def metadata_duration(metadata: Optional[dict]) -> float:
    return float((metadata or {}).get("duration_seconds") or 0.0)


# ────────────────────────── Engine interface ──────────────────────────

class SummarizationEngine(ABC):
    """Interface every summarization engine implements."""

    #: Short identifier used in the UI, job settings, and logs.
    name: str = "base"

    #: True when :meth:`detect_system_audio` is available as a separate pass.
    system_audio_prepass: bool = False

    async def detect_system_audio(
        self,
        turns: List[TranscriptTurn],
        metadata: Optional[dict] = None,
    ) -> Tuple[List[dict], TokenUsage]:
        """Return ``[{"turn": int, "text": str}, ...]`` markers for automated telecom messages.

        Only engines with ``system_audio_prepass = True`` implement this.
        """
        raise NotImplementedError(
            f"{self.name} detects automated messages inline during summarize_call"
        )

    @abstractmethod
    async def summarize_call(
        self,
        turns: List[TranscriptTurn],
        prompt: str,
        metadata: Optional[dict] = None,
        *,
        detect_system_audio: bool = False,
    ) -> CallSummary:
        """Summarize one call.

        ``detect_system_audio`` asks an inline-detecting engine to also flag
        automated messages and return them as markers; prepass engines ignore
        it (the pipeline already filtered the transcript).
        """

    @abstractmethod
    async def synthesize_case_report(
        self,
        inputs: CaseReportInputs,
    ) -> Tuple[Optional[CaseReportResponse], TokenUsage]:
        """Top findings plus outside-party identity inference for the case report."""

    def unload(self) -> None:
        """Release model memory. No-op for cloud engines."""
