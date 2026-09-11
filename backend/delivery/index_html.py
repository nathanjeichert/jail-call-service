"""The delivery's one HTML page: call index and synced audio viewer.

``index.html`` is a self-contained page with two views switched by the URL
hash:

* ``#index`` (or no hash): the call index, the client's home page. Masthead
  stats, a sticky filter bar (full text, date range, number, relevance), and
  one row per call that expands into the summary, review cues, and the full
  transcript with match stepping. ``#calendar`` and ``#timeline`` show the
  same filtered set as month grids or as a bucketed chart
  (``templates/index_charts.js``); the modes are offered only when calls
  carry dates, and a click on a day or bucket narrows the list to it.
* ``#call=<audio filename>&t=MM:SS``: the call view. Call rail, audio
  transport, printed transcript pages that follow playback, analysis rail
  with clickable cues, present mode. A ``t`` seeks and reveals the line while
  staying paused. The legacy ``?call=...&t=...`` query form that shipped case
  reports use is rewritten to the hash form on load.

Every call's data is embedded once as script-safe JSON: the view's
``line_entries`` serve both the printed pages and the index's full-text
search (turns are re-derived in the page), so the transcript text ships one
time. Fonts and the shared design layer are inlined (:mod:`theme`); the page
has no external dependencies and works from ``file://`` on a machine with no
network access.

Input is the per-call :class:`~backend.delivery.call_view.CallView` list the
delivery stage builds once for every surface.
"""

import hashlib
import html
import json
import re
from typing import List, Optional, Sequence

from ..formatting import format_generated_date
from ..html_json import dump_script_safe_json
from .call_view import CallView
from .templates import render_template
from .theme import theme_css

FONT_FAMILIES = ("Source Serif 4", "Source Sans 3", "IBM Plex Mono", "Courier Prime")


def _escape(s: str) -> str:
    return html.escape(s, quote=False)


def build_call_payload(view: CallView) -> dict:
    """The per-call JSON the page embeds; both views read this one shape."""
    call = view.call
    cues = [
        {
            "id": hashlib.sha256(json.dumps(
                [cue.get("line_cite"), cue.get("note"), cue.get("quote"), cue["seconds"]],
                ensure_ascii=False,
            ).encode("utf-8")).hexdigest()[:24],
            "t": cue["seconds"],
            "ts": cue["timestamp"].strip("[]"),
            "speaker": cue.get("speaker", "") or "",
            "quote": cue.get("quote", "") or "",
            "note": cue.get("note", "") or "",
            "line_cite": cue["line_cite"],
            "page": cue["page"],
        }
        for cue in view.cues
    ]
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
        "structured": view.structured,
        "relevance": view.relevance,
        "brief": view.brief,
        "identity": view.identity,
        # Raw text only matters for the unstructured fallback in the analysis rail.
        "summary": "" if view.is_dummy else view.summary,
        "cues": cues,
        # The one copy of the transcript: printed pages, cites, and search.
        "lines": view.line_entries,
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


def generate_index_html(views: Sequence[CallView], case_name: str = "", gen_date: Optional[str] = None) -> str:
    """Render ``index.html`` for the given call views."""
    call_data: List[dict] = [build_call_payload(v) for v in views]
    # Bind review files to this transcript set, independent of generated dates,
    # summaries, and the folder where the recipient extracts the delivery.
    review_manifest = [
        [c["audio_filename"], c["duration"], c["outside"], c["lines"]] for c in call_data
    ]
    review_id = hashlib.sha256(
        json.dumps([case_name, review_manifest], ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    title = f"{case_name} — Call Index" if case_name else "Call Index"
    gen_date = gen_date or format_generated_date()

    return render_template(
        "index.html",
        title=_escape(title),
        case_name=_escape(case_name or "Jail Calls"),
        case_title_html=_case_title_html(case_name),
        gen_date=gen_date,
        theme_css=theme_css("screen", FONT_FAMILIES),
        case_name_json=dump_script_safe_json(case_name or "Jail Calls"),
        review_id_json=dump_script_safe_json(review_id),
        data_json=dump_script_safe_json(call_data),
    )
