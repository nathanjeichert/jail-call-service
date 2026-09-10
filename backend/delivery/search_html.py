"""Self-contained search + browse HTML page generator.

This is the client-facing "home page" for a delivery package: a
sortable/filterable table of every call and a full-text search engine that
surfaces relevant transcript excerpts. From any row, clients can jump to the
call in the viewer (audio deep-link to a timestamp) or open the transcript
PDF.

All call data is embedded in a <script> JSON blob; fonts are embedded as
base64 woff2 data URIs; no external deps — the page works from file:// on a
machine with no network access. The template is ``templates/search.html``
with ``__PLACEHOLDER__`` substitution (its CSS/JS is full of braces).
"""

import html
import os
import re
from typing import List, Optional

from ..formatting import format_duration, format_generated_date, timestamp_to_seconds
from ..html_json import dump_script_safe_json
from ..models import call_stem
from ..summaries import DUMMY_SUMMARY_PREFIX, parse_summary_sections
from ..transcript_layout import compute_line_entries, hydrate_review_cues, line_cite_for_timestamp
from .fonts import embedded_font_css
from .templates import load_static_template


def _escape(s: str) -> str:
    return html.escape(s, quote=False)


def _turn_start_seconds(turn) -> float:
    """Pick the best available start second for a turn."""
    if turn.words:
        for w in turn.words:
            if w.start is not None and w.start >= 0:
                return float(w.start) / 1000.0
    return timestamp_to_seconds(turn.timestamp)


def _page_from_cite(cite: str):
    if not cite or ':' not in cite:
        return None
    try:
        return int(cite.split(':', 1)[0])
    except ValueError:
        return None


def _build_call_datum(call) -> dict:
    """Build the per-call payload embedded in the HTML page."""
    mp3_filename = os.path.basename(call.mp3_path) if call.mp3_path else ""
    pdf_filename = f"{call_stem(call.index, call.filename)}.pdf"
    duration = float(call.duration_seconds or 0.0)

    # Structured summary. Skip parsing for pipeline-generated dummy stubs
    # (skip_summary=True jobs) so we don't surface noise like "FOR foo.wav**".
    summary_text = call.summary or ""
    is_dummy = summary_text.startswith(DUMMY_SUMMARY_PREFIX)
    sections = parse_summary_sections(summary_text) if (summary_text and not is_dummy) else {}
    relevance = sections.get("relevance", "")
    brief_summary = (sections.get("call_summary") or "").replace("\n", " ").strip()
    identity = (sections.get("speakers") or "").replace("\n", " ").strip()

    # Review cues, with line_cite + page number computed from the same
    # line_entries the transcript PDF uses.
    turns = call.turns or []
    line_entries = compute_line_entries(turns, duration) if turns else []
    cues_raw = hydrate_review_cues(sections.get("review_cue_items") or [], line_entries)
    notes_cues = []
    for cue in cues_raw:
        ts = cue.get("timestamp", "") or ""
        line_cite = cue.get("line_cite", "") or (line_cite_for_timestamp(ts, line_entries) if line_entries else "")
        notes_cues.append({
            "timestamp": ts,
            "timestamp_sec": timestamp_to_seconds(ts),
            "speaker": cue.get("speaker", "") or "",
            "quote": cue.get("quote", "") or "",
            "note": cue.get("note", "") or "",
            "line_cite": line_cite,
            "page": _page_from_cite(line_cite),
        })

    # Compact turn array: [speaker, start_seconds, text]
    compact_turns = [[turn.speaker, round(_turn_start_seconds(turn), 2), turn.text] for turn in turns]

    return {
        "index": call.index,
        "filename": call.filename,
        "audio_filename": mp3_filename,
        "pdf_filename": pdf_filename,
        "duration": duration,
        "duration_str": format_duration(duration),
        "inmate": call.inmate_name or "",
        "outside": call.outside_number_fmt or "",
        "datetime": call.call_datetime_str or "",
        "call_date": call.call_date or "",
        "call_sort": call.call_datetime_str or call.call_date or "",
        "facility": call.facility or "",
        "outcome": call.call_outcome or "",
        "call_type": call.call_type or "",
        "relevance": relevance,
        "brief_summary": brief_summary,
        "identity": identity,
        "notes_cues": notes_cues,
        "turns": compact_turns,
        # Kept for legacy full-text search over the summary blob
        "summary_raw": "" if is_dummy else summary_text,
    }


def _case_title_html(case_name: str) -> str:
    """Render the masthead title; "X v. Y" captions get the italic v."""
    name = (case_name or "Call Index").strip() or "Call Index"
    match = re.match(r"^(.{2,80}?)\s+(vs?\.?)\s+(.{2,80})$", name, re.IGNORECASE)
    if match:
        return (
            f"{_escape(match.group(1))}<span class=\"v\">{_escape(match.group(2))}</span>"
            f"{_escape(match.group(3))}"
        )
    return _escape(name)


def generate_search_html(calls, case_name: str = "", gen_date: Optional[str] = None) -> str:
    call_data: List[dict] = [_build_call_datum(c) for c in calls]
    title = f"{case_name} — Call Index" if case_name else "Call Index"
    gen_date = gen_date or format_generated_date()

    return (
        load_static_template("search.html")
        .replace("__TITLE__", _escape(title))
        .replace("__CASE_TITLE_HTML__", _case_title_html(case_name))
        .replace("__GEN_DATE__", gen_date)
        .replace("__FONTS_CSS__", embedded_font_css())
        .replace("__DATA_JSON__", dump_script_safe_json(call_data))
    )
