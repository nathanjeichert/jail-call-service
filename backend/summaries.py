"""The canonical per-call summary text format: parse, normalize, render.

Every persisted call summary is plain text in one shape::

    RELEVANCE: HIGH

    NOTES:
    - [MM:SS] SPEAKER [Page:Line] — why this moment matters
    ...            (or exactly ``NOTES: NONE``)

    IDENTITY OF OUTSIDE PARTY:
    <one or two sentences>

    BRIEF SUMMARY:
    <one or two sentences>

The transcript PDF, viewer, search page, and case report all read summaries
back through :func:`parse_summary_sections`, so the pipeline normalizes every
engine's output into this format before persisting it: leaked preambles are
stripped, note counts are capped by relevance tier, duplicates are dropped,
identity/brief text is shortened to card length, and the kept note set is
reduced until it fits the summary-sheet page budget.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .delivery.summary_layout import paginate_structured_summary
from .formatting import (
    TIMESTAMP_RE,
    collapse_whitespace,
    format_timestamp,
    shorten,
    timestamp_to_seconds,
)
from .prompts import SUMMARY_NOTE_GUIDANCE, SUMMARY_NOTE_HARD_MAX  # noqa: F401
from .summarization.schemas import SummaryNote, SummaryResponse
from .transcript_layout import hydrate_review_cues, resolve_line_ref_context

# Summary sheets allowed per relevance tier (page 1 plus overflow pages).
SUMMARY_PAGE_LIMITS: Dict[str, int] = {"LOW": 1, "MEDIUM": 1, "HIGH": 3}

#: Summaries written by skip_summary test jobs start with this marker; the
#: delivery surfaces treat them as "no summary" rather than parsing them.
DUMMY_SUMMARY_PREFIX = "**DUMMY SUMMARY"

_MAX_IDENTITY_CHARS = 280
_MAX_BRIEF_CHARS = 320
_MAX_CONTEXT_SENTENCES = 2
_RELEVANCE_LINE_RE = re.compile(r"(?im)^RELEVANCE:\s*(HIGH|MEDIUM|LOW)\b")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


# ────────────────────────── Parsing ──────────────────────────

def _parse_review_cue_line(line: str) -> Optional[dict]:
    """Parse a single cue bullet into timestamp/speaker/quote/note fields."""
    clean = line.strip()
    if not clean:
        return None
    clean = re.sub(r"^[-•*]\s*", "", clean)
    clean = re.sub(r"^\d+[.)]\s*", "", clean)
    if re.fullmatch(
        r"(?i)(none|n/?a|no\s+notes?|no\s+relevant\s+(?:information|moments?)(?:\s+found)?\.?)",
        clean,
    ):
        return None

    ts_match = TIMESTAMP_RE.search(clean)
    if not ts_match:
        return None

    timestamp = ts_match.group(1)
    rest = clean[ts_match.end():].strip(" :-–—")

    speaker = ""
    speaker_match = re.match(
        r"\[?([A-Z][A-Z0-9 /&.'-]{1,34})\]?(?=\s*:|\s+\[|\s+[-–—]|$)",
        rest,
    )
    if speaker_match:
        speaker = speaker_match.group(1).strip()
        rest = rest[speaker_match.end():].strip()
        rest = re.sub(r"^:\s*", "", rest)

    line_ref = ""
    line_ref_match = re.match(r"\[?(\d+:\d+(?:\s*[-–—]\s*\d+:\d+)?)\]?\s*", rest)
    if line_ref_match:
        line_ref = re.sub(r"\s+", "", line_ref_match.group(1))
        line_ref = line_ref.replace("–", "-").replace("—", "-")
        rest = rest[line_ref_match.end():].strip()

    quote = ""
    if not line_ref:
        quote_match = re.search(r'["“]([^"”]{1,220})["”]', rest)
        if quote_match:
            quote = quote_match.group(1).strip()
            before = rest[: quote_match.start()].strip()
            after = rest[quote_match.end():].strip()
            rest = " ".join(part for part in (before, after) if part)

    rest = re.sub(r"^\s*[-–—:]+\s*", "", rest).strip()
    if quote and rest:
        parts = re.split(r"\s+[-–—]\s+", rest, maxsplit=1)
        note = parts[-1].strip()
    else:
        note = rest

    return {
        "timestamp": timestamp,
        "speaker": speaker,
        "line_ref": line_ref,
        "quote": quote,
        "note": note,
    }


def _parse_review_cues(body: str) -> List[dict]:
    cues: List[dict] = []
    for line in body.splitlines():
        cue = _parse_review_cue_line(line)
        if cue:
            cues.append(cue)
    cues.sort(key=lambda c: timestamp_to_seconds(c.get("timestamp", "")))
    return cues


_SECTION_HEADERS = [
    ("review_cues", r"(?m)^\s*NOTES:?"),
    ("review_cues", r"(?m)^\s*REVIEW\s+CUES:?"),
    ("review_cues", r"(?m)^\s*NOTABLE\s+MOMENTS:?"),
    ("review_cues", r"(?m)^\s*KEY\s+MOMENTS:?"),
    ("review_cues", r"(?m)^\s*PULL\s+QUOTES:?"),
    ("key_findings", r"(?m)^\s*KEY\s+FINDINGS:?"),
    ("speakers", r"(?m)^\s*IDENTITY\s+OF\s+OUTSIDE\s+PARTY:?"),
    ("speakers", r"(?m)^\s*SPEAKERS?\s*(?:&|AND)\s*RELATIONSHIP:?"),
    ("speakers", r"(?m)^\s*SPEAKER\s+NOTES:?"),
    ("call_summary", r"(?m)^\s*BRIEF\s+SUMMARY:?"),
    ("call_summary", r"(?m)^\s*CALL\s+SUMMARY:?"),
    ("call_summary", r"(?m)^\s*SUMMARY:?"),
]


def parse_summary_sections(summary: str) -> dict:
    """Parse summary text into renderable sections.

    Accepts the current NOTES-based format and older REVIEW CUES / KEY
    FINDINGS output so previously generated summaries still render. Keys:
    ``relevance``, ``review_cues`` (raw body), ``review_cue_items`` (parsed
    bullets), ``speakers``, ``call_summary``, ``structured`` (bool), ``raw``.
    """
    sections: Dict[str, object] = {"raw": summary}
    text = summary.strip()

    rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", text, re.IGNORECASE)
    if rel_match:
        sections["relevance"] = rel_match.group(1).upper()

    positions: List[tuple] = []
    for key, pattern in _SECTION_HEADERS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if any(
                not (match.end() <= start or match.start() >= end)
                for start, end, _ in positions
            ):
                continue
            positions.append((match.start(), match.end(), key))
            break
    positions.sort()

    seen_keys: set = set()
    for i, (start, end, key) in enumerate(positions):
        if key in seen_keys and key not in {"review_cues"}:
            continue
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[end:next_start].strip()
        if key == "review_cues" and sections.get("review_cues"):
            sections["review_cues"] += "\n" + body
        else:
            sections[key] = body
        seen_keys.add(key)

    cue_body = sections.get("review_cues") or sections.get("key_findings") or ""
    if cue_body:
        sections["review_cue_items"] = _parse_review_cues(cue_body)

    sections["structured"] = bool(
        sections.get("relevance")
        and (
            sections.get("review_cue_items")
            or sections.get("call_summary")
            or sections.get("speakers")
        )
    )

    return sections


# ────────────────────────── Normalization ──────────────────────────

def _trim_context_text(value: Optional[str], *, max_chars: int) -> str:
    text = collapse_whitespace(value)
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
        return clipped if clipped else shorten(text, max_chars)
    return shorten(text, max_chars)


def sanitize_summary_text(summary_text: Optional[str]) -> str:
    """Strip obvious leaked reasoning or scaffolding ahead of RELEVANCE."""
    text = str(summary_text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""

    match = _RELEVANCE_LINE_RE.search(text)
    if match and match.start() > 0:
        text = text[match.start():].lstrip()
    return text.strip()


def _coerce_importance_rank(value: object) -> Optional[int]:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(rank, SUMMARY_NOTE_HARD_MAX))


def _note_display_sort_key(item: dict, line_entries: Optional[List[dict]]) -> tuple:
    line_ref = str(item.get("line_ref") or item.get("line_cite") or "").strip()
    if line_ref and line_entries:
        ctx = resolve_line_ref_context(line_ref, line_entries)
        if ctx:
            return (float(ctx["start"]), 0)
    return (timestamp_to_seconds(item.get("timestamp", "")), 1)


def _note_priority_key(item: dict, line_entries: Optional[List[dict]]) -> Tuple[Tuple[int, int], tuple, int]:
    rank = _coerce_importance_rank(item.get("importance_rank"))
    if rank is None:
        rank_key = (1, int(item.get("source_index", SUMMARY_NOTE_HARD_MAX)))
    else:
        rank_key = (0, rank)
    return (
        rank_key,
        _note_display_sort_key(item, line_entries),
        int(item.get("source_index", 0)),
    )


def _normalize_note_item(item: dict, line_entries: Optional[List[dict]]) -> Optional[dict]:
    line_ref = str(item.get("line_ref") or item.get("line_cite") or "").strip()
    line_ref = line_ref.replace("–", "-").replace("—", "-")
    line_ref = re.sub(r"\s+", "", line_ref)

    note = collapse_whitespace(item.get("note") or item.get("reason"))
    if not note:
        return None

    timestamp = collapse_whitespace(item.get("timestamp"))
    speaker = collapse_whitespace(item.get("speaker")).upper()
    if line_ref and line_entries:
        ctx = resolve_line_ref_context(line_ref, line_entries)
        if ctx:
            timestamp = ctx["timestamp"]
            speaker = collapse_whitespace(ctx.get("speaker")).upper()
            line_ref = collapse_whitespace(ctx.get("line_cite"))

    if timestamp:
        timestamp = format_timestamp(timestamp_to_seconds(timestamp))

    return {
        "timestamp": timestamp,
        "speaker": speaker,
        "line_ref": line_ref,
        "note": note,
        "importance_rank": _coerce_importance_rank(item.get("importance_rank")),
        "source_index": int(item.get("source_index", 0)),
    }


def _curate_note_items(note_items: List[dict], line_entries: Optional[List[dict]]) -> List[dict]:
    """Normalize, dedupe (best rank wins), rank-sort, and hard-cap notes."""
    normalized_by_key: Dict[object, dict] = {}
    for source_index, raw_item in enumerate(note_items):
        item = _normalize_note_item(
            {**raw_item, "source_index": raw_item.get("source_index", source_index)},
            line_entries,
        )
        if not item:
            continue
        dedupe_key = item["line_ref"] or (item["timestamp"], item["speaker"], item["note"].lower())
        existing = normalized_by_key.get(dedupe_key)
        if existing is None or _note_priority_key(item, line_entries) < _note_priority_key(existing, line_entries):
            normalized_by_key[dedupe_key] = item

    normalized = sorted(normalized_by_key.values(), key=lambda item: _note_priority_key(item, line_entries))
    return normalized[:SUMMARY_NOTE_HARD_MAX]


def _least_important_note_index(note_items: List[dict], line_entries: Optional[List[dict]]) -> int:
    worst_index = 0
    worst_key = _note_priority_key(note_items[0], line_entries)
    for idx, item in enumerate(note_items[1:], start=1):
        candidate_key = _note_priority_key(item, line_entries)
        if candidate_key > worst_key:
            worst_index = idx
            worst_key = candidate_key
    return worst_index


def _fit_note_items_to_page_budget(
    note_items: List[dict],
    *,
    relevance: str,
    speakers: str,
    call_summary: str,
    line_entries: Optional[List[dict]],
) -> List[dict]:
    """Drop the weakest notes until the summary fits its tier's page limit."""
    fitted = list(note_items)
    max_pages = SUMMARY_PAGE_LIMITS.get(relevance, SUMMARY_PAGE_LIMITS["LOW"])
    while fitted:
        hydrated = hydrate_review_cues(fitted, line_entries)
        pagination = paginate_structured_summary(hydrated, speakers=speakers, call_summary=call_summary)
        if 1 + len(pagination["overflow_review_cue_pages"]) <= max_pages:
            return fitted
        del fitted[_least_important_note_index(fitted, line_entries)]
    return fitted


# ────────────────────────── Rendering ──────────────────────────

def render_summary_sections(
    relevance: str,
    note_items: List[dict],
    *,
    identity_of_outside_party: str = "",
    brief_summary: str = "",
    line_entries: Optional[List[dict]] = None,
) -> str:
    """Render normalized note dicts plus context into canonical summary text."""
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
                speaker = collapse_whitespace(ctx.get("speaker")).upper()
                line_ref = collapse_whitespace(ctx.get("line_cite"))

        prefix_parts = [timestamp]
        if speaker:
            prefix_parts.append(speaker)
        if line_ref:
            prefix_parts.append(f"[{line_ref}]")

        rendered_notes.append((timestamp_to_seconds(timestamp), f"- {' '.join(prefix_parts)} — {item['note']}"))

    rendered_notes.sort(key=lambda item: item[0])
    if rendered_notes:
        blocks.append("NOTES:\n" + "\n".join(line for _, line in rendered_notes))
    else:
        blocks.append("NOTES: NONE")

    identity_text = _trim_context_text(identity_of_outside_party, max_chars=_MAX_IDENTITY_CHARS)
    if identity_text:
        blocks.append(f"IDENTITY OF OUTSIDE PARTY:\n{identity_text}")

    brief_text = _trim_context_text(brief_summary, max_chars=_MAX_BRIEF_CHARS)
    blocks.append(f"BRIEF SUMMARY:\n{brief_text}" if brief_text else "BRIEF SUMMARY:\n")

    return "\n\n".join(blocks).strip()


def render_summary_text(summary: SummaryResponse, line_entries: List[dict]) -> str:
    """Render a structured summary response into canonical summary text.

    Notes whose cite does not resolve to transcript lines are dropped: the
    timestamp, speaker, and pull quote are always derived from the cited
    lines, never from model-authored text.
    """
    blocks: List[str] = [f"RELEVANCE: {summary.relevance}"]

    rendered_notes = []
    for note in summary.notes:
        ctx = resolve_line_ref_context(note.line_ref, line_entries)
        if not ctx:
            continue
        reason = collapse_whitespace(note.reason)
        if not reason:
            continue
        rendered_notes.append((
            float(ctx["start"]),
            f'- {ctx["timestamp"]} {ctx["speaker"]} [{ctx["line_cite"]}] — {reason}',
        ))

    rendered_notes.sort(key=lambda item: item[0])
    note_lines = [line for _, line in rendered_notes]
    blocks.append("NOTES:\n" + "\n".join(note_lines) if note_lines else "NOTES: NONE")

    identity = (summary.identity_of_outside_party or "").strip()
    if identity:
        blocks.append(f"IDENTITY OF OUTSIDE PARTY:\n{identity}")

    brief = collapse_whitespace(summary.brief_summary)
    blocks.append(f"BRIEF SUMMARY:\n{brief}" if brief else "BRIEF SUMMARY:\n")

    return "\n\n".join(blocks).strip()


# ────────────────────────── Entry points ──────────────────────────

def normalize_structured_summary(summary: SummaryResponse, line_entries: Optional[List[dict]]) -> SummaryResponse:
    """Curate a structured (JSON) summary: cap, dedupe, fit to page budget, sort by time."""
    relevance = str(summary.relevance or "LOW").upper()
    identity_text = _trim_context_text(summary.identity_of_outside_party, max_chars=_MAX_IDENTITY_CHARS)
    brief_text = _trim_context_text(summary.brief_summary, max_chars=_MAX_BRIEF_CHARS)
    curated_items = _curate_note_items(
        [
            {"line_ref": note.line_ref, "reason": note.reason, "importance_rank": note.importance_rank}
            for note in (summary.notes or [])
        ],
        line_entries,
    )
    curated_items = _fit_note_items_to_page_budget(
        curated_items,
        relevance=relevance,
        speakers=identity_text,
        call_summary=brief_text,
        line_entries=line_entries,
    )
    curated_notes = [
        SummaryNote(
            line_ref=item["line_ref"],
            reason=item["note"],
            importance_rank=item.get("importance_rank") or (idx + 1),
        )
        for idx, item in enumerate(curated_items)
        if item.get("line_ref")
    ]
    curated_notes.sort(key=lambda note: _note_display_sort_key({"line_ref": note.line_ref}, line_entries))

    return SummaryResponse(
        relevance=relevance,
        notes=curated_notes,
        identity_of_outside_party=identity_text or None,
        brief_summary=brief_text,
    )


def normalize_summary_text(summary_text: Optional[str], line_entries: Optional[List[dict]]) -> str:
    """Normalize free-text engine output into the canonical structured format.

    Text without a recognizable ``RELEVANCE:`` line is returned sanitized but
    otherwise untouched so the unstructured-fallback rendering path applies.
    """
    cleaned = sanitize_summary_text(summary_text)
    if not cleaned:
        return ""

    sections = parse_summary_sections(cleaned)
    relevance = str(sections.get("relevance") or "").upper()
    if relevance not in SUMMARY_PAGE_LIMITS:
        return cleaned

    identity_text = _trim_context_text(sections.get("speakers"), max_chars=_MAX_IDENTITY_CHARS)
    brief_text = _trim_context_text(sections.get("call_summary"), max_chars=_MAX_BRIEF_CHARS)
    curated_items = _curate_note_items(list(sections.get("review_cue_items") or []), line_entries)
    curated_items = _fit_note_items_to_page_budget(
        curated_items,
        relevance=relevance,
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
