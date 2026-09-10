"""One presentation model per call, built once and shared by every surface.

Before this module existed, the transcript PDF, the search page, the viewer,
and the case report each parsed the stored summary text, computed the
page:line layout, hydrated the review cues, and derived output filenames on
their own, four slightly different ways. :func:`build_call_view` does that
work exactly once per call; the delivery stage builds the views up front and
hands the same list to every generator.

What a :class:`CallView` carries:

* ``call`` - the source :class:`~backend.models.CallResult` (metadata,
  turns, status) for anything surface-specific.
* ``stem`` / ``audio_filename`` / ``pdf_filename`` - the delivery-relative
  output names, all derived from :func:`~backend.models.call_stem`.
* ``line_entries`` - :func:`~backend.transcript_layout.compute_line_entries`
  for the call, the single page:line computation every cite depends on.
* ``summary`` - the stored summary text ("" when none). ``is_dummy`` marks
  the skip-summary placeholder, which the HTML surfaces treat as "no
  summary" while the transcript PDF still prints it as a raw sheet.
* ``sections`` - the stored ``summary_json`` rebuilt into the
  :func:`~backend.summaries.parse_summary_sections` shape when present
  (:func:`~backend.summaries.sections_from_summary_json`), else that parser's
  output for the text (older jobs), or ``{}`` when there is nothing usable.
  ``structured`` / ``relevance`` / ``brief`` / ``identity`` read from it.
* ``cues`` - the hydrated review cues (dicts, because the summary paginator
  and the PDF template read them as such), each with ``timestamp``,
  ``speaker``, ``quote``, ``note``, ``line_cite`` plus two derived keys:
  ``seconds`` (start time) and ``page`` (transcript page of the cite).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, List, Optional

from ..formatting import format_duration, timestamp_to_seconds
from ..models import CallResult, TranscriptTurn, call_stem
from ..summaries import DUMMY_SUMMARY_PREFIX, parse_summary_sections, sections_from_summary_json
from ..transcript_layout import compute_line_entries, hydrate_review_cues


def _one_line(value: object) -> str:
    return str(value or "").replace("\n", " ").strip()


def _page_from_cite(cite: str) -> Optional[int]:
    if not cite or ":" not in cite:
        return None
    try:
        return int(cite.split(":", 1)[0])
    except ValueError:
        return None


@dataclass
class CallView:
    call: CallResult
    stem: str
    audio_filename: str
    pdf_filename: str
    duration: float
    duration_label: str
    line_entries: List[dict]
    summary: str
    is_dummy: bool
    sections: dict
    cues: List[dict]

    # ── convenience readers ──

    @property
    def index(self) -> int:
        return self.call.index

    @property
    def filename(self) -> str:
        return self.call.filename

    @property
    def turns(self) -> List[TranscriptTurn]:
        return self.call.turns or []

    @property
    def structured(self) -> bool:
        return bool(self.sections.get("structured"))

    @property
    def relevance(self) -> str:
        return str(self.sections.get("relevance") or "")

    @property
    def brief(self) -> str:
        return _one_line(self.sections.get("call_summary"))

    @property
    def identity(self) -> str:
        return _one_line(self.sections.get("speakers"))


def build_call_view(call: CallResult) -> CallView:
    """Parse, lay out, and hydrate one call. Deterministic and side-effect free."""
    stem = call_stem(call.index, call.filename)
    duration = float(call.duration_seconds or 0.0)
    turns = call.turns or []
    line_entries = compute_line_entries(turns, duration) if turns else []

    summary = call.summary or ""
    is_dummy = summary.startswith(DUMMY_SUMMARY_PREFIX)
    sections: dict = {}
    if not is_dummy:
        # Prefer the structured twin written at summarize time; older jobs
        # (and the synthetic package) only have the text.
        sections = sections_from_summary_json(call.summary_json)
        if not sections and summary:
            sections = parse_summary_sections(summary)

    cues: List[dict] = []
    for cue in hydrate_review_cues(sections.get("review_cue_items") or [], line_entries):
        timestamp = str(cue.get("timestamp") or "")
        line_cite = str(cue.get("line_cite") or "")
        cue["timestamp"] = timestamp
        cue["line_cite"] = line_cite
        cue["seconds"] = timestamp_to_seconds(timestamp)
        cue["page"] = _page_from_cite(line_cite)
        cues.append(cue)

    return CallView(
        call=call,
        stem=stem,
        audio_filename=os.path.basename(call.mp3_path) if call.mp3_path else f"{stem}.mp3",
        pdf_filename=f"{stem}.pdf",
        duration=duration,
        duration_label=format_duration(duration),
        line_entries=line_entries,
        summary=summary,
        is_dummy=is_dummy,
        sections=sections,
        cues=cues,
    )


def build_call_views(calls: Iterable[CallResult]) -> List[CallView]:
    return [build_call_view(call) for call in calls]
