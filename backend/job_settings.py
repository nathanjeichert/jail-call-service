"""
Shared helpers for job prompt composition and engine/runtime selection.

Engine facts (local or cloud, concurrency cap, what a job still needs
before it can run) come from the stage registries in
``backend/engine_registry.py``; nothing here names an engine.
"""

from dataclasses import dataclass
from typing import Iterable, Optional

from . import config as cfg
from .case_documents import build_case_documents_block
from .engine_registry import EngineRegistry, EngineSpec
from .models import Job

CASE_CONTEXT_MARKER = "\n\nCASE CONTEXT:\n"
AUTO_MESSAGE_MODES = frozenset({"exclude", "label"})


def _transcription_registry() -> EngineRegistry:
    from .transcription import REGISTRY
    return REGISTRY


def _summarization_registry() -> EngineRegistry:
    from .summarization import REGISTRY
    return REGISTRY


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


def effective_summary_prompt(job: Job) -> str:
    """The per-call prompt the summarizer receives.

    ``job.summary_prompt`` (the default prompt plus the operator's typed case
    context) followed by the case-documents block. The document text is never
    written into ``summary_prompt``, so re-runs and the job list keep showing
    only what the operator typed.
    """
    prompt = job.summary_prompt or cfg.DEFAULT_SUMMARY_PROMPT
    block = build_case_documents_block(job.case_documents)
    return f"{prompt}\n\n{block}" if block else prompt


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
    def transcription_spec(self) -> Optional[EngineSpec]:
        return _transcription_registry().find(self.transcription_engine)

    @property
    def summarization_spec(self) -> Optional[EngineSpec]:
        return _summarization_registry().find(self.summarization_engine)

    @property
    def transcription_is_local(self) -> bool:
        spec = self.transcription_spec
        return bool(spec and spec.local)

    @property
    def summarization_is_local(self) -> bool:
        spec = self.summarization_spec
        return bool(spec and spec.local)

    @property
    def all_local(self) -> bool:
        return self.transcription_is_local and self.summarization_is_local

    @property
    def effective_auto_message_mode(self) -> Optional[str]:
        # System-audio filtering only runs when summarization is active. Gemini
        # performs it in a dedicated first pass; Gemma keeps the legacy
        # combined summary + SYSTEM_AUDIO tail.
        if self.skip_summary:
            return None
        return self.auto_message_mode

    def transcription_workers(self, total_calls: int) -> int:
        spec = self.transcription_spec
        limit = (spec.max_concurrency if spec else None) or cfg.MAX_TRANSCRIPTION_CONCURRENT
        return min(limit, max(1, total_calls))

    def summarization_workers(self, total_calls: int) -> int:
        spec = self.summarization_spec
        limit = (spec.max_concurrency if spec else None) or cfg.MAX_SUMMARIZATION_CONCURRENT
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


def _require_ready(registry: EngineRegistry, name: str) -> None:
    spec = registry.find(name)
    if spec is None or not spec.installed():
        available = ", ".join(registry.available()) or "none"
        raise RuntimeError(f"{registry.stage.capitalize()} engine '{name}' is unavailable. Available: {available}")
    requirement = spec.requirement()
    if requirement:
        raise RuntimeError(f"{spec.label}: {requirement}")


def validate_runtime_selection(selection: RuntimeSelection) -> None:
    """Raise before any work starts if a selected engine cannot run."""
    _require_ready(_transcription_registry(), selection.transcription_engine)
    if not selection.skip_summary:
        _require_ready(_summarization_registry(), selection.summarization_engine)
