"""
Summary normalization and curation helpers.

Ensures every persisted call summary uses the same capped, structured shape so
the PDF, search page, viewer, and case report all read from the same
attorney-facing note set.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from . import pdf_utils as U
from .gemini_structured import SummaryNote, SummaryResponse

SUMMARY_NOTE_LIMITS: Dict[str, Dict[str, int]] = {
    "LOW": {"target": 2, "hard": 3},
    "MEDIUM": {"target": 4, "hard": 6},
    "HIGH": {"target": 12, "hard": 21},
}
SUMMARY_MAX_PAGES = 3

_MAX_IDENTITY_CHARS = 280
_MAX_BRIEF_CHARS = 320
_MAX_CONTEXT_SENTENCES = 2
_RELEVANCE_LINE_RE = re.compile(r"(?im)^RELEVANCE:\s*(HIGH|MEDIUM|LOW)\b")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


def _normalize_timestamp_label(value: str) -> str:
    seconds = U.timestamp_to_seconds(value)
    total = max(int(seconds), 0)
    mins, secs = divmod(total, 60)
    return f"[{mins:02d}:{secs:02d}]"


def _collapse_whitespace(value: Optional[str]) -> str:
    return " ".join(str(value or "").split()).strip()


def _trim_context_text(value: Optional[str], *, max_chars: int) -> str:
    text = _collapse_whitespace(value)
    if not text:
        return ""

    sentences = [part.strip() for part in _SENTENCE_BOUNDARY_RE.split(text) if part.strip()]
    if len(sentences) > _MAX_CONTEXT_SENTENCES:
        text = " ".join(sentences[:_MAX_CONTEXT_SENTENCES]).strip()

    if len(text) <= max_chars:
        return text

    soft_clip = text[: max_chars + 1]
    boundary = max(
        soft_clip.rfind(". "),
        soft_clip.rfind("? "),
        soft_clip.rfind("! "),
        soft_clip.rfind("; "),
    )
    if boundary >= int(max_chars * 0.6):
        clipped = soft_clip[: boundary + 1].strip()
        return clipped if clipped else U.shorten(text, max_chars)
    return U.shorten(text, max_chars)


def sanitize_summary_text(summary_text: Optional[str]) -> str:
    """Strip obvious leaked reasoning or scaffolding ahead of RELEVANCE."""
    text = str(summary_text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""

    match = _RELEVANCE_LINE_RE.search(text)
    if match and match.start() > 0:
        text = text[match.start() :].lstrip()
    return text.strip()


def _note_sort_key(item: dict, line_entries: Optional[List[dict]]) -> tuple:
    from .transcript_formatting import resolve_line_ref_context

    line_ref = str(item.get("line_ref") or item.get("line_cite") or "").strip()
    if line_ref and line_entries:
        ctx = resolve_line_ref_context(line_ref, line_entries)
        if ctx:
            return (float(ctx["start"]), 0)
    return (U.timestamp_to_seconds(item.get("timestamp", "")), 1)


def _normalize_note_item(
    item: dict,
    line_entries: Optional[List[dict]],
) -> Optional[dict]:
    from .transcript_formatting import resolve_line_ref_context

    line_ref = str(item.get("line_ref") or item.get("line_cite") or "").strip()
    line_ref = line_ref.replace("\u2013", "-").replace("\u2014", "-")
    line_ref = re.sub(r"\s+", "", line_ref)

    note = _collapse_whitespace(item.get("note") or item.get("reason"))
    if not note:
        return None

    timestamp = _collapse_whitespace(item.get("timestamp"))
    speaker = _collapse_whitespace(item.get("speaker")).upper()
    if line_ref and line_entries:
        ctx = resolve_line_ref_context(line_ref, line_entries)
        if ctx:
            timestamp = ctx["timestamp"]
            speaker = _collapse_whitespace(ctx.get("speaker")).upper()
            line_ref = _collapse_whitespace(ctx.get("line_cite"))

    if timestamp:
        timestamp = _normalize_timestamp_label(timestamp)

    return {
        "timestamp": timestamp,
        "speaker": speaker,
        "line_ref": line_ref,
        "note": note,
    }


def _curate_note_items(
    note_items: List[dict],
    relevance: str,
    line_entries: Optional[List[dict]],
) -> List[dict]:
    normalized: List[dict] = []
    seen = set()
    for raw_item in sorted(note_items, key=lambda item: _note_sort_key(item, line_entries)):
        item = _normalize_note_item(raw_item, line_entries)
        if not item:
            continue
        dedupe_key = item["line_ref"] or (
            item["timestamp"],
            item["speaker"],
            item["note"].lower(),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(item)

    limit = SUMMARY_NOTE_LIMITS.get(relevance, SUMMARY_NOTE_LIMITS["LOW"])["hard"]
    return normalized[:limit]


def _fit_note_items_to_page_budget(
    note_items: List[dict],
    *,
    speakers: str,
    call_summary: str,
    line_entries: Optional[List[dict]],
) -> List[dict]:
    from .transcript_formatting import hydrate_review_cues, paginate_structured_summary

    fitted = list(note_items)
    while fitted:
        hydrated = hydrate_review_cues(fitted, line_entries)
        pagination = paginate_structured_summary(
            hydrated,
            speakers=speakers,
            call_summary=call_summary,
        )
        if 1 + len(pagination["overflow_review_cue_pages"]) <= SUMMARY_MAX_PAGES:
            return fitted
        fitted = fitted[:-1]
    return fitted


def render_summary_sections(
    relevance: str,
    note_items: List[dict],
    *,
    identity_of_outside_party: str = "",
    brief_summary: str = "",
    line_entries: Optional[List[dict]] = None,
) -> str:
    from .transcript_formatting import resolve_line_ref_context

    blocks = [f"RELEVANCE: {relevance}"]

    rendered_notes = []
    for raw_item in note_items:
        item = _normalize_note_item(raw_item, line_entries)
        if not item:
            continue

        timestamp = item.get("timestamp") or "[00:00]"
        speaker = item.get("speaker", "")
        line_ref = item.get("line_ref", "")
        if line_ref and line_entries:
            ctx = resolve_line_ref_context(line_ref, line_entries)
            if ctx:
                timestamp = ctx["timestamp"]
                speaker = _collapse_whitespace(ctx.get("speaker")).upper()
                line_ref = _collapse_whitespace(ctx.get("line_cite"))

        prefix_parts = [timestamp]
        if speaker:
            prefix_parts.append(speaker)
        if line_ref:
            prefix_parts.append(f"[{line_ref}]")

        rendered_notes.append((
            U.timestamp_to_seconds(timestamp),
            f"- {' '.join(prefix_parts)} — {item['note']}",
        ))

    rendered_notes.sort(key=lambda item: item[0])
    if rendered_notes:
        blocks.append("NOTES:\n" + "\n".join(line for _, line in rendered_notes))
    else:
        blocks.append("NOTES: NONE")

    identity_text = _trim_context_text(
        identity_of_outside_party,
        max_chars=_MAX_IDENTITY_CHARS,
    )
    if identity_text:
        blocks.append(f"IDENTITY OF OUTSIDE PARTY:\n{identity_text}")

    brief_text = _trim_context_text(brief_summary, max_chars=_MAX_BRIEF_CHARS)
    blocks.append(f"BRIEF SUMMARY:\n{brief_text}" if brief_text else "BRIEF SUMMARY:\n")

    return "\n\n".join(blocks).strip()


def normalize_structured_summary(
    summary: SummaryResponse,
    line_entries: Optional[List[dict]],
) -> SummaryResponse:
    relevance = str(summary.relevance or "LOW").upper()
    identity_text = _trim_context_text(
        summary.identity_of_outside_party,
        max_chars=_MAX_IDENTITY_CHARS,
    )
    brief_text = _trim_context_text(summary.brief_summary, max_chars=_MAX_BRIEF_CHARS)
    curated_items = _curate_note_items(
        [
            {"line_ref": note.line_ref, "reason": note.reason}
            for note in (summary.notes or [])
        ],
        relevance,
        line_entries,
    )
    curated_items = _fit_note_items_to_page_budget(
        curated_items,
        speakers=identity_text,
        call_summary=brief_text,
        line_entries=line_entries,
    )
    curated_notes = [
        SummaryNote(line_ref=item["line_ref"], reason=item["note"])
        for item in curated_items
        if item.get("line_ref")
    ]

    return SummaryResponse(
        relevance=relevance,
        notes=curated_notes,
        identity_of_outside_party=identity_text or None,
        brief_summary=brief_text,
    )


def normalize_summary_text(
    summary_text: Optional[str],
    line_entries: Optional[List[dict]],
) -> str:
    """Normalize legacy/raw summary text into the canonical structured format."""
    cleaned = sanitize_summary_text(summary_text)
    if not cleaned:
        return ""

    sections = U.parse_summary_sections(cleaned)
    relevance = str(sections.get("relevance") or "").upper()
    if relevance not in SUMMARY_NOTE_LIMITS:
        return cleaned

    identity_text = _trim_context_text(
        sections.get("speakers"),
        max_chars=_MAX_IDENTITY_CHARS,
    )
    brief_text = _trim_context_text(
        sections.get("call_summary"),
        max_chars=_MAX_BRIEF_CHARS,
    )
    curated_items = _curate_note_items(
        list(sections.get("review_cue_items") or []),
        relevance,
        line_entries,
    )
    curated_items = _fit_note_items_to_page_budget(
        curated_items,
        speakers=identity_text,
        call_summary=brief_text,
        line_entries=line_entries,
    )
    return render_summary_sections(
        relevance,
        curated_items,
        identity_of_outside_party=identity_text,
        brief_summary=brief_text,
        line_entries=line_entries,
    )
