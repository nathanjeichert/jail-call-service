"""
Multi-call viewer HTML generator.

Renders template.html with all call data embedded as JSON.
Audio references use paths relative to the delivery root (audio/<filename>.mp3),
since the rendered file ships as viewer.html alongside the audio/ directory.
"""

import html as html_mod
import os

from ..html_json import dump_script_safe_json

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "template.html")


def _build_call_entry(call, line_entries: list) -> dict:
    """Convert a CallResult + line_entries to viewer JSON format."""
    mp3_filename = os.path.basename(call.mp3_path) if call.mp3_path else f"call-{call.index + 1:03d}.mp3"
    return {
        "index": call.index,
        "filename": call.filename,
        "audio_filename": mp3_filename,
        "duration": call.duration_seconds or 0,
        "summary": call.summary or "",
        "lines": line_entries,
        "inmate": call.inmate_name or "",
        "outside": call.outside_number_fmt or "",
        "datetime": call.call_datetime_str or "",
        "facility": call.facility or "",
        "outcome": call.call_outcome or "",
    }


def _line_entries_for_call(call) -> list:
    """Build flat line entries from a CallResult's transcript turns."""
    from ..transcript_formatting import compute_line_entries

    if not call.turns:
        return []

    entries = compute_line_entries(call.turns, call.duration_seconds or 0)
    # Convert start/end from seconds (already in seconds from compute_line_entries)
    return entries


def render_viewer(calls, case_name: str = "") -> str:
    """
    Render the multi-call viewer HTML.

    Args:
        calls: List of CallResult objects (must have .turns, .summary, .mp3_path etc.)
        case_name: Used in the page title and header.

    Returns:
        Full HTML string.
    """
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        template = f.read()

    call_data = []
    for call in calls:
        status = call.status if isinstance(call.status, str) else call.status.value
        if status not in ("done", "generating_pdf"):
            continue
        line_entries = _line_entries_for_call(call)
        call_data.append(_build_call_entry(call, line_entries))

    calls_json = dump_script_safe_json(call_data)
    escaped_case_name = html_mod.escape(case_name or "Jail Calls")
    result = template.replace("{{CALLS_JSON}}", calls_json)
    result = result.replace("{{ CALLS_JSON }}", calls_json)
    result = result.replace("{{CASE_NAME}}", escaped_case_name)

    return result
