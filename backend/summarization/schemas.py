"""Typed results shared by every summarization engine.

Engines that support native structured output send these as the response
JSON schema; text-protocol engines parse their block format into the same
models. Downstream code (summary normalization, case report) only ever sees
these types.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

Relevance = Literal["HIGH", "MEDIUM", "LOW"]


class SystemAudioMarker(BaseModel):
    turn: int = Field(ge=0, description="0-based transcript turn index.")
    text: str = Field(min_length=1, description="Exact automated-message substring from that turn.")


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
    reason: str = Field(min_length=1, description="Why this cited moment matters for attorney review.")
    importance_rank: int = Field(
        ge=1,
        le=21,
        description=(
            "Unique importance rank for this returned note. 1 is the most "
            "important note in the response, and larger numbers are weaker."
        ),
    )


class SummaryResponse(BaseModel):
    relevance: Relevance
    notes: List[SummaryNote] = Field(
        default_factory=list,
        max_length=21,
        description="Ordered from most important note to least important note. Return no more than 21 notes total.",
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
    confidence: Optional[Relevance] = None


class CaseReportResponse(BaseModel):
    findings: List[CaseReportFinding] = Field(default_factory=list)
    identities: List[CaseReportIdentity] = Field(default_factory=list)
