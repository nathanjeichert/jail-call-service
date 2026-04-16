"""
Shared helpers for job prompt composition and engine/runtime selection.
"""

from dataclasses import dataclass
from typing import Iterable, Optional

from . import config as cfg

CASE_CONTEXT_MARKER = "\n\nCASE CONTEXT:\n"
AUTO_MESSAGE_MODES = frozenset({"exclude", "label"})
LOCAL_TRANSCRIPTION_ENGINES = frozenset({"parakeet"})
LOCAL_SUMMARIZATION_ENGINES = frozenset({"gemma"})


def normalize_optional_name(value: Optional[str]) -> Optional[str]:
    candidate = (value or "").strip().lower()
    return candidate or None


def normalize_engine_name(value: Optional[str], default: str) -> str:
    return normalize_optional_name(value) or default.strip().lower()


def normalize_auto_message_mode(value: Optional[str]) -> Optional[str]:
    candidate = normalize_optional_name(value)
    return candidate if candidate in AUTO_MESSAGE_MODES else None


def compose_summary_prompt(case_context: Optional[str]) -> str:
    context = (case_context or "").strip()
    if not context:
        return cfg.DEFAULT_SUMMARY_PROMPT
    return f"{cfg.DEFAULT_SUMMARY_PROMPT}{CASE_CONTEXT_MARKER}{context}"


def extract_case_context(summary_prompt: Optional[str]) -> str:
    prompt = (summary_prompt or "").strip()
    if CASE_CONTEXT_MARKER not in prompt:
        return ""
    return prompt.split(CASE_CONTEXT_MARKER, 1)[1].strip()


def resolve_default_engine(default_engine: str, available_engines: Iterable[str]) -> str:
    available = tuple(available_engines)
    normalized_default = default_engine.strip().lower()
    if normalized_default in available:
        return normalized_default
    if available:
        return available[0]
    return normalized_default


@dataclass(frozen=True)
class RuntimeSelection:
    transcription_engine: str
    summarization_engine: str
    skip_summary: bool
    auto_message_mode: Optional[str]

    @property
    def transcription_is_local(self) -> bool:
        return self.transcription_engine in LOCAL_TRANSCRIPTION_ENGINES

    @property
    def summarization_is_local(self) -> bool:
        return self.summarization_engine in LOCAL_SUMMARIZATION_ENGINES

    @property
    def all_local(self) -> bool:
        return self.transcription_is_local and self.summarization_is_local

    @property
    def effective_auto_message_mode(self) -> Optional[str]:
        # System-audio filtering piggybacks on the summary prompt, so it's
        # active whenever summarization runs — both cloud (Gemini) and local
        # (Gemma) engines carry the SYSTEM_AUDIO: tail.
        if self.skip_summary:
            return None
        return self.auto_message_mode

    def transcription_workers(self, total_calls: int) -> int:
        limit = cfg.MAX_PARAKEET_CONCURRENT if self.transcription_is_local else cfg.MAX_TRANSCRIPTION_CONCURRENT
        return min(limit, max(1, total_calls))

    def summarization_workers(self, total_calls: int) -> int:
        limit = cfg.MAX_GEMMA_CONCURRENT if self.summarization_is_local else cfg.MAX_SUMMARIZATION_CONCURRENT
        return min(limit, max(1, total_calls))


def resolve_runtime_selection(
    transcription_engine: Optional[str],
    summarization_engine: Optional[str],
    *,
    skip_summary: bool,
    auto_message_mode: Optional[str],
) -> RuntimeSelection:
    return RuntimeSelection(
        transcription_engine=normalize_engine_name(
            transcription_engine, cfg.DEFAULT_TRANSCRIPTION_ENGINE
        ),
        summarization_engine=normalize_engine_name(
            summarization_engine, cfg.DEFAULT_SUMMARIZATION_ENGINE
        ),
        skip_summary=skip_summary,
        auto_message_mode=normalize_auto_message_mode(auto_message_mode),
    )


def validate_runtime_selection(selection: RuntimeSelection) -> None:
    from .transcription import AVAILABLE_ENGINES as AVAILABLE_TRANSCRIPTION_ENGINES
    from .summarization import AVAILABLE_ENGINES as AVAILABLE_SUMMARIZATION_ENGINES

    if selection.transcription_engine not in AVAILABLE_TRANSCRIPTION_ENGINES:
        available = ", ".join(AVAILABLE_TRANSCRIPTION_ENGINES) or "none"
        raise RuntimeError(
            f"Transcription engine '{selection.transcription_engine}' is unavailable. "
            f"Available: {available}"
        )

    if (
        not selection.skip_summary
        and selection.summarization_engine not in AVAILABLE_SUMMARIZATION_ENGINES
    ):
        available = ", ".join(AVAILABLE_SUMMARIZATION_ENGINES) or "none"
        raise RuntimeError(
            f"Summarization engine '{selection.summarization_engine}' is unavailable. "
            f"Available: {available}"
        )

    if not selection.transcription_is_local and not cfg.ASSEMBLYAI_API_KEY:
        raise RuntimeError(
            f"ASSEMBLYAI_API_KEY is required for transcription engine "
            f"'{selection.transcription_engine}' but is not set in .env"
        )

    if (
        not selection.skip_summary
        and not selection.summarization_is_local
        and not cfg.GEMINI_API_KEY
    ):
        raise RuntimeError(
            f"GEMINI_API_KEY is required for summarization engine "
            f"'{selection.summarization_engine}' but is not set in .env"
        )
