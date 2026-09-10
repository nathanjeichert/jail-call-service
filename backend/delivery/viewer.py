"""Multi-call viewer HTML generator.

Renders ``templates/viewer.html`` with all call data embedded as JSON.
Audio references use paths relative to the delivery root (audio/<file>.mp3),
since the rendered file ships as viewer.html alongside the audio/ directory.
Fonts and the shared design layer are inlined so the page is fully self-contained.

Input is the per-call :class:`~backend.delivery.call_view.CallView` list the
delivery stage builds once for every surface.
"""

import html
from typing import Sequence

from ..html_json import dump_script_safe_json
from .call_view import CallView
from .templates import render_template
from .theme import theme_css


def _build_call_entry(view: CallView) -> dict:
    """Convert a CallView to the viewer's embedded JSON shape."""
    call = view.call
    structured = view.structured
    cues = (
        [
            {
                "t": cue["seconds"],
                "ts": cue["timestamp"].strip("[]"),
                "note": cue.get("note", "") or "",
                "quote": cue.get("quote", "") or "",
                "line_cite": cue["line_cite"],
            }
            for cue in view.cues
        ]
        if structured
        else []
    )

    return {
        "index": call.index,
        "filename": call.filename,
        "audio_filename": view.audio_filename,
        "duration": call.duration_seconds or 0,
        "summary": "" if view.is_dummy else view.summary,
        "structured": structured,
        "relevance": view.relevance if structured else "",
        "brief": view.brief if structured else "",
        "identity": view.identity if structured else "",
        "cues": cues,
        "lines": view.line_entries,
        "inmate": call.inmate_name or "",
        "outside": call.outside_number_fmt or "",
        "datetime": call.call_datetime_str or "",
        "facility": call.facility or "",
        "outcome": call.call_outcome or "",
    }


def render_viewer(views: Sequence[CallView], case_name: str = "") -> str:
    """Render the multi-call viewer HTML for the given call views."""
    return render_template(
        "viewer.html",
        calls_json=dump_script_safe_json([_build_call_entry(v) for v in views]),
        case_name=html.escape(case_name or "Jail Calls"),
        theme_css=theme_css("screen", ("Fraunces", "Public Sans", "IBM Plex Mono", "Courier Prime")),
    )
