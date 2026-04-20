"""
Gemini-specific structured-output schemas and helpers.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

class SystemAudioMarker(BaseModel):
    turn: int = Field(ge=0, description="0-based transcript turn index.")
    text: str = Field(
        min_length=1,
        description="Exact automated-message substring from that turn.",
    )


class SystemAudioResponse(BaseModel):
    system_audio: List[SystemAudioMarker] = Field(default_factory=list)


class SummaryNote(BaseModel):
    line_ref: str = Field(
        min_length=3,
        description=(
            "Exact supporting transcript line cite in Page:Line or "
            "Page:Line-Page:Line format. Prefer a single line when it fully "
            "supports the note; otherwise keep ranges short."
        ),
    )
    reason: str = Field(
        min_length=1,
        description="Why this cited moment matters for attorney review.",
    )
    importance_rank: int = Field(
        ge=1,
        le=21,
        description=(
            "Unique importance rank for this returned note. 1 is the most "
            "important note in the response, and larger numbers are weaker."
        ),
    )


class SummaryResponse(BaseModel):
    relevance: Literal["HIGH", "MEDIUM", "LOW"]
    notes: List[SummaryNote] = Field(
        default_factory=list,
        max_length=21,
        description=(
            "Ordered from most important note to least important note. "
            "Return no more than 21 notes total."
        ),
    )
    identity_of_outside_party: Optional[str] = Field(default=None)
    brief_summary: str = Field(default="")


class CaseReportFinding(BaseModel):
    call_id: int = Field(ge=0)
    headline: str = Field(min_length=1)
    timestamp: Optional[str] = Field(
        default=None,
        description="Use a [MM:SS] note timestamp from the cited call, or null.",
    )
    detail: str = Field(min_length=1)


class CaseReportIdentity(BaseModel):
    number: str = Field(min_length=1)
    inference: str = Field(min_length=1)
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


class CaseReportResponse(BaseModel):
    findings: List[CaseReportFinding] = Field(default_factory=list)
    identities: List[CaseReportIdentity] = Field(default_factory=list)


GEMINI_SUMMARY_JSON_INSTRUCTIONS = (
    "OUTPUT OVERRIDE:\n"
    "Ignore any earlier output-format instructions that conflict with this section.\n"
    "Return a JSON object matching the provided schema.\n"
    "For each note, provide ONLY:\n"
    '- "line_ref": the exact supporting transcript line or short adjacent line range in '
    'Page:Line or Page:Line-Page:Line format\n'
    '- "reason": why that cited moment matters\n'
    '- "importance_rank": unique integer rank where 1 is the strongest note in the response\n'
    "Do NOT include timestamps, speakers, or quoted text in the JSON notes. "
    "The application derives those from the cited lines.\n"
    "Order the notes array from most important to least important. The application will "
    "re-sort kept notes chronologically for display.\n"
    "Choose the shortest cited range that both directly supports the note and, when possible, "
    "will read coherently when rendered as a standalone pull quote. Prefer a single cited line "
    "when it fully supports the point; use 2-3 adjacent lines only when necessary.\n"
    "Use an empty notes array when there is nothing attorney-relevant to note.\n"
    "For LOW calls, usually return 0-3 notes. "
    "For MEDIUM calls, do not exceed 6 notes. "
    "For HIGH calls, usually keep the note count to about 12 so the strongest material fits cleanly "
    "in two summary pages, but for unusually dense and highly relevant calls you may return more "
    "when warranted, up to 21 notes total. "
    "If more moments seem arguable, omit weaker or redundant ones so the strongest notes fit first.\n"
    "Set identity_of_outside_party to null when the caller cannot be reasonably identified.\n"
    "Ignore any transcript lines spoken by AUTOMATED MESSAGE when choosing notes or writing the brief summary."
)


def render_summary_text(summary: SummaryResponse, line_entries: List[dict]) -> str:
    """Render a structured Gemini summary back into the app's text summary format."""
    from .transcript_formatting import resolve_line_ref_context

    blocks: List[str] = [f"RELEVANCE: {summary.relevance}"]

    note_lines = []
    rendered_notes = []
    for note in summary.notes:
        ctx = resolve_line_ref_context(note.line_ref, line_entries)
        if not ctx:
            continue
        reason = " ".join((note.reason or "").split()).strip()
        if not reason:
            continue
        rendered_notes.append((
            float(ctx["start"]),
            f'- {ctx["timestamp"]} {ctx["speaker"]} [{ctx["line_cite"]}] — {reason}',
        ))

    rendered_notes.sort(key=lambda item: item[0])
    note_lines = [line for _, line in rendered_notes]

    if note_lines:
        blocks.append("NOTES:\n" + "\n".join(note_lines))
    else:
        blocks.append("NOTES: NONE")

    identity = (summary.identity_of_outside_party or "").strip()
    if identity:
        blocks.append(f"IDENTITY OF OUTSIDE PARTY:\n{identity}")

    brief = " ".join((summary.brief_summary or "").split()).strip()
    if brief:
        blocks.append(f"BRIEF SUMMARY:\n{brief}")
    else:
        blocks.append("BRIEF SUMMARY:\n")

    return "\n\n".join(blocks).strip()
