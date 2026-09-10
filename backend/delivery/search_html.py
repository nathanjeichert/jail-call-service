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

Input is the per-call :class:`~backend.delivery.call_view.CallView` list the
delivery stage builds once for every surface.
"""

import html
import re
from typing import List, Optional, Sequence

from ..formatting import format_generated_date, timestamp_to_seconds
from ..html_json import dump_script_safe_json
from .call_view import CallView
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


def _build_call_datum(view: CallView) -> dict:
    """The per-call payload embedded in the page."""
    call = view.call
    notes_cues = [
        {
            "timestamp": cue["timestamp"],
            "timestamp_sec": cue["seconds"],
            "speaker": cue.get("speaker", "") or "",
            "quote": cue.get("quote", "") or "",
            "note": cue.get("note", "") or "",
            "line_cite": cue["line_cite"],
            "page": cue["page"],
        }
        for cue in view.cues
    ]

    # Compact turn array: [speaker, start_seconds, text]
    compact_turns = [[turn.speaker, round(_turn_start_seconds(turn), 2), turn.text] for turn in view.turns]

    return {
        "index": call.index,
        "filename": call.filename,
        "audio_filename": view.audio_filename,
        "pdf_filename": view.pdf_filename,
        "duration": view.duration,
        "duration_str": view.duration_label,
        "inmate": call.inmate_name or "",
        "outside": call.outside_number_fmt or "",
        "datetime": call.call_datetime_str or "",
        "call_date": call.call_date or "",
        "call_sort": call.call_datetime_str or call.call_date or "",
        "facility": call.facility or "",
        "outcome": call.call_outcome or "",
        "call_type": call.call_type or "",
        "relevance": view.relevance,
        "brief_summary": view.brief,
        "identity": view.identity,
        "notes_cues": notes_cues,
        "turns": compact_turns,
        # Kept for legacy full-text search over the summary blob
        "summary_raw": "" if view.is_dummy else view.summary,
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


def generate_search_html(views: Sequence[CallView], case_name: str = "", gen_date: Optional[str] = None) -> str:
    call_data: List[dict] = [_build_call_datum(v) for v in views]
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
