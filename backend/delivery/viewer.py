"""Multi-call viewer HTML generator.

Renders ``templates/viewer.html`` with all call data embedded as JSON.
Audio references use paths relative to the delivery root (audio/<file>.mp3),
since the rendered file ships as viewer.html alongside the audio/ directory.
Fonts are embedded as base64 data URIs so the page is fully self-contained.
"""

import html
import os

from ..formatting import timestamp_to_seconds
from ..html_json import dump_script_safe_json
from ..summaries import parse_summary_sections
from ..transcript_layout import compute_line_entries, hydrate_review_cues
from .fonts import embedded_font_css
from .templates import load_static_template

DUMMY_SUMMARY_PREFIX = "**DUMMY SUMMARY"


def _build_call_entry(call, line_entries: list) -> dict:
    """Convert a CallResult + line_entries to viewer JSON format."""
    mp3_filename = os.path.basename(call.mp3_path) if call.mp3_path else f"call-{call.index + 1:03d}.mp3"

    # Structured analysis for the right-hand rail. Dummy stubs from
    # skip_summary jobs fall back to the raw-text rendering path.
    summary_text = call.summary or ""
    is_dummy = summary_text.startswith(DUMMY_SUMMARY_PREFIX)
    sections = parse_summary_sections(summary_text) if (summary_text and not is_dummy) else {}
    structured = bool(sections.get("structured"))

    cues = []
    if structured:
        for cue in hydrate_review_cues(sections.get("review_cue_items") or [], line_entries):
            ts = cue.get("timestamp", "") or ""
            cues.append({
                "t": timestamp_to_seconds(ts),
                "ts": ts.strip("[]"),
                "note": cue.get("note", "") or "",
                "quote": cue.get("quote", "") or "",
                "line_cite": cue.get("line_cite", "") or "",
            })

    return {
        "index": call.index,
        "filename": call.filename,
        "audio_filename": mp3_filename,
        "duration": call.duration_seconds or 0,
        "summary": "" if is_dummy else summary_text,
        "structured": structured,
        "relevance": sections.get("relevance", "") if structured else "",
        "brief": (sections.get("call_summary") or "").replace("\n", " ").strip() if structured else "",
        "identity": (sections.get("speakers") or "").replace("\n", " ").strip() if structured else "",
        "cues": cues,
        "lines": line_entries,
        "inmate": call.inmate_name or "",
        "outside": call.outside_number_fmt or "",
        "datetime": call.call_datetime_str or "",
        "facility": call.facility or "",
        "outcome": call.call_outcome or "",
    }


def render_viewer(calls, case_name: str = "") -> str:
    """Render the multi-call viewer HTML for the given CallResult objects."""
    call_data = []
    for call in calls:
        status = call.status if isinstance(call.status, str) else call.status.value
        if status not in ("done", "generating_pdf"):
            continue
        line_entries = compute_line_entries(call.turns, call.duration_seconds or 0) if call.turns else []
        call_data.append(_build_call_entry(call, line_entries))

    calls_json = dump_script_safe_json(call_data)
    return (
        load_static_template("viewer.html")
        .replace("{{CALLS_JSON}}", calls_json)
        .replace("{{ CALLS_JSON }}", calls_json)
        .replace("{{CASE_NAME}}", html.escape(case_name or "Jail Calls"))
        .replace("{{FONTS_CSS}}", embedded_font_css(("Fraunces", "Public Sans", "IBM Plex Mono", "Courier Prime")))
    )
