"""
Self-contained search + browse HTML page generator.

This is the client-facing "home page" for a delivery package. It serves
as a sortable/filterable table of every call and as a
full-text search engine that surfaces relevant transcript excerpts. From
any row, clients can jump to the call in the viewer (with audio deep-link
to a specific timestamp) or open the formatted transcript PDF.

All call data is embedded in a <script> JSON blob; fonts are embedded as
base64 woff2 data URIs; no external deps — the page works from file:// on
a machine with no network access.
"""

import logging
import os
import re
from datetime import datetime
from typing import List, Optional

from .design_fonts import embedded_font_css
from .html_json import dump_script_safe_json
from .models import call_stem
from . import pdf_utils as U
from .pdf_utils import parse_summary_sections, timestamp_to_seconds
from .transcript_formatting import (
    _line_cite_for_timestamp,
    compute_line_entries,
    hydrate_review_cues,
)

logger = logging.getLogger(__name__)


def _call_stem(index: int, filename: str) -> str:
    return call_stem(index, filename)


def _format_duration(seconds: Optional[float]) -> str:
    return U.format_duration(seconds)


def _turn_start_seconds(turn) -> float:
    """Pick the best available start second for a turn."""
    if turn.words:
        for w in turn.words:
            if w.start is not None and w.start >= 0:
                return float(w.start) / 1000.0
    return timestamp_to_seconds(turn.timestamp)


def _page_from_cite(cite: str) -> Optional[int]:
    if not cite or ':' not in cite:
        return None
    try:
        return int(cite.split(':', 1)[0])
    except ValueError:
        return None


def _build_call_datum(call) -> dict:
    """Build the per-call payload embedded in the HTML page."""
    mp3_filename = os.path.basename(call.mp3_path) if call.mp3_path else ""
    pdf_filename = f"{_call_stem(call.index, call.filename)}.pdf"
    duration = float(call.duration_seconds or 0.0)

    # Structured summary. Skip parsing for pipeline-generated dummy stubs
    # (skip_summary=True jobs) so we don't surface noise like "FOR foo.wav**".
    summary_text = call.summary or ""
    is_dummy = summary_text.startswith("**DUMMY SUMMARY")
    sections = parse_summary_sections(summary_text) if (summary_text and not is_dummy) else {}
    relevance = sections.get("relevance", "")
    brief_summary = (sections.get("call_summary") or "").replace("\n", " ").strip()
    identity = (sections.get("speakers") or "").replace("\n", " ").strip()

    # Review cues, with line_cite + page number computed from the same
    # line_entries the transcript PDF would use.
    turns = call.turns or []
    line_entries = compute_line_entries(turns, duration) if turns else []
    cues_raw = hydrate_review_cues(sections.get("review_cue_items") or [], line_entries)
    notes_cues = []
    for cue in cues_raw:
        ts = cue.get("timestamp", "") or ""
        line_cite = cue.get("line_cite", "") or (_line_cite_for_timestamp(ts, line_entries) if line_entries else "")
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
    compact_turns = []
    for turn in turns:
        compact_turns.append([
            turn.speaker,
            round(_turn_start_seconds(turn), 2),
            turn.text,
        ])

    # Sortable timestamp: "YYYY-MM-DD HH:MM" → sortable string; fall back to call_date.
    call_sort = call.call_datetime_str or call.call_date or ""

    return {
        "index": call.index,
        "filename": call.filename,
        "audio_filename": mp3_filename,
        "pdf_filename": pdf_filename,
        "duration": duration,
        "duration_str": _format_duration(duration),
        "inmate": call.inmate_name or "",
        "outside": call.outside_number_fmt or "",
        "datetime": call.call_datetime_str or "",
        "call_date": call.call_date or "",
        "call_sort": call_sort,
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


def _build_call_data(calls) -> List[dict]:
    return [_build_call_datum(c) for c in calls]


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


def generate_search_html(calls, case_name: str = "") -> str:
    call_data = _build_call_data(calls)
    data_json = dump_script_safe_json(call_data)
    title = f"{case_name} — Call Index" if case_name else "Call Index"

    now = datetime.now()
    gen_date = f"{now.strftime('%B')} {now.day}, {now.year}"

    return (
        _TEMPLATE
        .replace("__TITLE__", _escape(title))
        .replace("__CASE_TITLE_HTML__", _case_title_html(case_name))
        .replace("__GEN_DATE__", gen_date)
        .replace("__FONTS_CSS__", embedded_font_css())
        .replace("__DATA_JSON__", data_json)
    )


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


# ─────────────────────────────────────────────────────────────────────────────
# HTML template (single-file, vanilla JS)
# ─────────────────────────────────────────────────────────────────────────────
# Placeholders (string-replaced above) — not Jinja:
#   __TITLE__            page <title>
#   __CASE_TITLE_HTML__  masthead case title (pre-escaped HTML)
#   __GEN_DATE__         delivery generation date
#   __FONTS_CSS__        embedded @font-face rules
#   __DATA_JSON__        embedded JSON blob

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
__FONTS_CSS__

  *, *::before, *::after { box-sizing: border-box; }
  :root {
    --paper:       #F5F2EA;
    --cream:       #FBF9F3;
    --sheet:       #FFFFFF;
    --ink:         #16140F;
    --ink-2:       #45413A;
    --ink-3:       #807A6E;
    --rule:        rgba(22,20,15,.18);
    --rule-faint:  rgba(22,20,15,.09);
    --signal:      #A8271E;
    --signal-ink:  #871F18;
    --signal-bg:   rgba(168,39,30,.07);
    --med:         #8F6400;
    --low:         #76796E;
    --mark:        #F4E9C8;
    --serif: "Fraunces", Georgia, serif;
    --sans:  "Public Sans", "Helvetica Neue", Helvetica, Arial, sans-serif;
    --mono:  "IBM Plex Mono", Menlo, Consolas, monospace;
    --ctl-h: 0px;
  }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: var(--sans);
    color: var(--ink);
    background: var(--paper);
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
    font-size: 15px;
    line-height: 1.5;
  }
  ::selection { background: var(--ink); color: var(--paper); }
  .mono { font-family: var(--mono); font-feature-settings: "tnum"; }

  .lbl {
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: .14em;
    text-transform: uppercase;
    color: var(--ink-3);
  }
  .lbl--ink { color: var(--ink); }

  /* relevance marker — the only color in the system */
  .rv {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .13em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .rv::before { content: ""; width: 8px; height: 8px; border-radius: 1px; flex: none; }
  .rv--HIGH   { color: var(--signal-ink); }
  .rv--HIGH::before   { background: var(--signal); }
  .rv--MEDIUM { color: var(--med); }
  .rv--MEDIUM::before { background: transparent; box-shadow: inset 0 0 0 1.5px var(--med); }
  .rv--LOW    { color: var(--low); }
  .rv--LOW::before    { background: transparent; box-shadow: inset 0 0 0 1.5px rgba(118,121,110,.7); }
  .rv--none   { color: var(--ink-3); }
  .rv--none::before   { background: transparent; box-shadow: inset 0 0 0 1.5px var(--rule); }

  /* ── Confidential band ──────────────────────────────────────────────── */
  .band {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    padding: 10px 36px;
    border-bottom: 1px solid var(--rule);
  }
  .band .sep { color: var(--ink-3); font-weight: 500; margin: 0 6px; }
  .band-right { display: flex; gap: 26px; }

  /* ── Masthead ───────────────────────────────────────────────────────── */
  .head { padding: 32px 36px 0; }
  .head-top {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 30px;
  }
  .head-title { min-width: 0; }
  .head-title h1 {
    margin: 0;
    font-family: var(--serif);
    font-weight: 600;
    font-size: 46px;
    line-height: 1.04;
    letter-spacing: -.005em;
    font-variation-settings: "opsz" 80;
    overflow-wrap: break-word;
  }
  .head-title h1 .v {
    font-style: italic;
    font-weight: 400;
    font-size: .62em;
    color: var(--ink-3);
    margin: 0 .14em;
  }
  .case-meta { margin-top: 9px; font-size: 13.5px; color: var(--ink-2); }
  .case-meta .sep { color: var(--ink-3); margin: 0 7px; }

  .stats { display: flex; flex: none; }
  .stats .cell { padding: 2px 24px 6px; border-left: 1px solid var(--rule); }
  .stats .cell:first-child { border-left: none; }
  .stats .n {
    font-family: var(--serif);
    font-weight: 600;
    font-size: 36px;
    line-height: 1;
    font-variant-numeric: tabular-nums;
    letter-spacing: -.01em;
    white-space: nowrap;
  }
  .stats .n small { font-size: .5em; font-weight: 500; color: var(--ink-3); letter-spacing: 0; }
  .stats .lbl { margin-top: 6px; font-size: 9.5px; }

  .tally {
    margin-top: 20px;
    padding: 12px 0 13px;
    border-top: 3px solid var(--ink);
    display: flex;
    align-items: center;
    gap: 28px;
    position: relative;
    flex-wrap: wrap;
  }
  .tally::before { content: ""; position: absolute; top: 2px; left: 0; right: 0; border-top: 1px solid var(--ink); }
  .tally .rv { font-size: 11.5px; cursor: pointer; }
  .tally .rv b { font-size: 14px; margin-right: 2px; }
  .tally-range { margin-left: auto; font-family: var(--mono); font-size: 11.5px; color: var(--ink-3); letter-spacing: .04em; }

  /* ── Controls ───────────────────────────────────────────────────────── */
  .ctl {
    position: sticky;
    top: 0;
    z-index: 40;
    background: var(--paper);
    padding: 13px 36px;
    border-bottom: 1px solid var(--rule);
  }
  .ctl-inner { display: flex; align-items: center; gap: 11px; flex-wrap: wrap; }
  .search-wrap {
    flex: 1 1 320px;
    min-width: 260px;
    display: flex;
    align-items: center;
    gap: 12px;
    background: var(--sheet);
    border: 1px solid var(--ink);
    padding: 0 15px;
    height: 44px;
  }
  .search-wrap svg { color: var(--ink-3); flex-shrink: 0; }
  .search-wrap:focus-within { box-shadow: inset 0 -2px 0 var(--ink); }
  .search-input {
    flex: 1;
    border: none;
    outline: none;
    background: transparent;
    font: inherit;
    font-size: 15px;
    color: var(--ink);
    height: 100%;
    min-width: 0;
  }
  .search-input::placeholder { color: var(--ink-3); }
  .search-count {
    font-family: var(--mono);
    font-size: 11.5px;
    color: var(--ink-3);
    white-space: nowrap;
  }
  .search-count b { color: var(--signal-ink); font-weight: 600; }

  .filter-label {
    color: var(--ink-3);
    font-size: 9.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .1em;
    padding-right: 2px;
  }
  .filter-input, .filter-select {
    font: inherit;
    font-size: 13px;
    color: var(--ink);
    background: var(--cream);
    border: 1px solid var(--rule);
    padding: 0 10px;
    height: 38px;
    border-radius: 0;
    font-variant-numeric: tabular-nums;
  }
  .filter-select {
    padding: 0 28px 0 12px;
    min-width: 150px;
    appearance: none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path fill='%23807A6E' d='M0 0l5 6 5-6z'/></svg>");
    background-repeat: no-repeat;
    background-position: right 10px center;
  }
  .filter-input:focus, .filter-select:focus {
    outline: none;
    border-color: var(--ink);
  }

  .chip-group { display: inline-flex; border: 1px solid var(--rule); background: var(--cream); }
  .chip {
    display: inline-flex;
    align-items: center;
    font: inherit;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: var(--ink-3);
    background: transparent;
    border: none;
    border-left: 1px solid var(--rule);
    padding: 0 14px;
    height: 36px;
    cursor: pointer;
    border-radius: 0;
  }
  .chip:first-child { border-left: none; }
  .chip:hover { color: var(--ink); }
  .chip.active { background: var(--ink); color: var(--paper); }

  .chip-clear {
    background: transparent;
    border: none;
    color: var(--ink-3);
    font: inherit;
    font-size: 12.5px;
    cursor: pointer;
    padding: 0 4px;
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .chip-clear:hover { color: var(--ink); }

  /* ── Index table ────────────────────────────────────────────────────── */
  .main { padding: 0 36px 90px; }

  .cols {
    display: grid;
    grid-template-columns: 100px 132px 60px 170px 1fr 148px;
    gap: 0 18px;
    align-items: baseline;
    padding: 14px 0 9px;
    border-bottom: 1px solid var(--ink);
    position: sticky;
    top: var(--ctl-h);
    background: var(--paper);
    z-index: 30;
  }
  .cols .lbl { font-size: 10px; cursor: pointer; user-select: none; }
  .cols .lbl.no-sort { cursor: default; }
  .cols .lbl.sorted { color: var(--ink); }
  .cols .lbl .arr { letter-spacing: 0; }
  .cols .num { text-align: right; }

  .row {
    display: grid;
    grid-template-columns: 100px 132px 60px 170px 1fr 148px;
    gap: 0 18px;
    align-items: baseline;
    padding: 14px 0 13px;
    border-bottom: 1px solid var(--rule-faint);
    cursor: pointer;
    position: relative;
  }
  .row:hover { background: rgba(255,255,255,.55); }
  .row.open { background: rgba(255,255,255,.55); border-bottom: none; }
  .row.rel-HIGH::before {
    content: "";
    position: absolute;
    left: -22px;
    top: 0;
    bottom: -1px;
    width: 4px;
    background: var(--signal);
  }
  .row .rv { font-size: 10.5px; }
  .row .date b { display: block; font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; color: var(--ink); }
  .row .date span { font-size: 12px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .row .dur { font-family: var(--mono); font-size: 13px; text-align: right; font-feature-settings: "tnum"; color: var(--ink-2); }
  .row .party { min-width: 0; }
  .row .party b { display: block; font-size: 13.5px; font-weight: 600; font-family: var(--mono); letter-spacing: .01em; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .party span { display: block; font-size: 12px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .sum { font-size: 14px; color: var(--ink-2); line-height: 1.45; min-width: 0; }
  .row .sum .clamp {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .row .sum .empty { color: var(--ink-3); font-style: italic; }
  mark { background: var(--mark); color: var(--ink); font-weight: 600; padding: 0 2px; border-bottom: 2px solid var(--med); }

  .row .acts { text-align: right; white-space: nowrap; }
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: .08em;
    text-transform: uppercase;
    padding: 8px 12px;
    cursor: pointer;
    white-space: nowrap;
    text-decoration: none;
    border: 1px solid var(--ink);
    color: var(--ink);
    background: transparent;
    margin-left: 6px;
  }
  .btn:hover { background: var(--ink); color: var(--paper); }
  .btn--primary { background: var(--ink); color: var(--paper); }
  .btn--primary:hover { background: #000; }
  .btn .mono-bit { font-family: var(--mono); font-weight: 500; letter-spacing: 0; text-transform: none; opacity: .7; }

  /* Match excerpts in the Summary column (search mode) */
  .excerpts { display: flex; flex-direction: column; gap: 5px; }
  .excerpt {
    display: grid;
    grid-template-columns: 48px 1fr;
    gap: 10px;
    padding: 6px 9px;
    background: var(--sheet);
    border-left: 3px solid var(--med);
    cursor: pointer;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-2);
  }
  .excerpt:hover { background: var(--cream); }
  .excerpt .ex-t {
    font-family: var(--mono);
    color: var(--ink-3);
    font-size: 10.5px;
    font-weight: 600;
    padding-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* ── Detail panel ───────────────────────────────────────────────────── */
  .detail {
    border-top: 1px solid var(--rule);
    border-bottom: 1px solid var(--rule);
    background: var(--sheet);
    padding: 24px 28px 22px;
    position: relative;
    box-shadow: 0 14px 26px -18px rgba(22,20,15,.25);
  }
  .detail.rel-HIGH::before {
    content: "";
    position: absolute;
    left: -22px;
    top: -1px;
    bottom: 0;
    width: 4px;
    background: var(--signal);
  }
  .detail h4 {
    margin: 0 0 9px;
    font-size: 10.5px;
    font-weight: 700;
    letter-spacing: .14em;
    text-transform: uppercase;
    color: var(--ink-3);
  }
  .detail-grid {
    display: grid;
    grid-template-columns: minmax(280px, 340px) 1fr;
    gap: 0 36px;
  }
  .brief {
    margin: 0;
    font-family: var(--serif);
    font-size: 15.5px;
    line-height: 1.55;
    color: var(--ink);
    font-variation-settings: "opsz" 18;
  }
  .id-block { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--rule-faint); }
  .id-block p { margin: 0; font-size: 13.5px; color: var(--ink-2); line-height: 1.5; }

  .cues table { width: 100%; border-collapse: collapse; }
  .cues td { padding: 9px 0 8px; border-bottom: 1px solid var(--rule-faint); vertical-align: baseline; }
  .cues tr:last-child td { border-bottom: none; }
  .cues tr { cursor: pointer; }
  .cues tr:hover .note { color: var(--ink); }
  .cues .ts { font-family: var(--mono); font-size: 13px; font-weight: 600; color: var(--signal-ink); width: 60px; white-space: nowrap; }
  .cues .note { font-size: 13.5px; color: var(--ink-2); padding-right: 18px; line-height: 1.45; }
  .cues .note b { color: var(--ink); font-weight: 600; }
  .cues .note .q { font-family: var(--serif); font-style: italic; color: var(--ink); font-variation-settings: "opsz" 18; }
  .cues .cite { font-family: var(--mono); font-size: 11px; color: var(--ink-3); text-align: right; white-space: nowrap; width: 130px; }
  .cues .cite a { color: var(--ink-3); text-decoration: underline; text-underline-offset: 3px; }
  .cues .cite a:hover { color: var(--ink); }

  /* Transcript block */
  .trans {
    margin-top: 22px;
    border: 1px solid var(--rule);
    background: var(--sheet);
  }
  .trans-head {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 12px 18px;
    border-bottom: 1px solid var(--rule);
  }
  .trans-head h4 { margin: 0; }
  .trans-head .hint { font-size: 11px; color: var(--ink-3); }
  .trans-nav { margin-left: auto; display: flex; align-items: center; gap: 8px; }
  .trans-nav .nav-label {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--signal-ink);
    font-weight: 600;
    letter-spacing: .04em;
    white-space: nowrap;
  }
  .trans-nav .nav-label.quiet { color: var(--ink-3); font-weight: 400; }
  .trans-nav button {
    font: inherit;
    font-size: 14px;
    line-height: 1;
    width: 28px;
    height: 28px;
    border: 1px solid var(--ink);
    background: transparent;
    color: var(--ink);
    cursor: pointer;
    padding: 0;
  }
  .trans-nav button:hover { background: var(--ink); color: var(--paper); }
  .trans-body {
    max-height: 460px;
    overflow-y: auto;
    padding: 6px 18px 14px;
  }
  .trans-body::-webkit-scrollbar { width: 10px; }
  .trans-body::-webkit-scrollbar-thumb { background: var(--rule); }
  .trans-body::-webkit-scrollbar-track { background: var(--cream); }

  .turn {
    display: grid;
    grid-template-columns: 118px 1fr;
    gap: 0 16px;
    padding: 9px 10px 8px;
    border-bottom: 1px solid var(--rule-faint);
    cursor: pointer;
    position: relative;
  }
  .turn:last-child { border-bottom: none; }
  .turn:hover { background: var(--cream); }
  .turn .who {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: var(--ink-3);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .turn .who .t {
    display: block;
    margin-top: 3px;
    font-family: var(--mono);
    font-weight: 400;
    font-size: 11px;
    letter-spacing: .02em;
  }
  .turn .tx { font-size: 14px; line-height: 1.55; color: var(--ink-2); word-break: break-word; }
  .turn.has-cue::before {
    content: "";
    position: absolute;
    left: 0;
    top: 9px;
    width: 4px;
    height: 16px;
    background: var(--signal);
  }
  .turn.has-cue .who { color: var(--signal-ink); }
  .turn.is-match { background: var(--cream); }
  .turn.nav-current { background: var(--signal-bg); box-shadow: inset 0 0 0 1px var(--signal); }

  /* Bottom meta strip + actions */
  .detail-meta {
    display: flex;
    gap: 28px;
    margin-top: 20px;
    padding-top: 14px;
    border-top: 1px solid var(--rule);
    align-items: flex-end;
    flex-wrap: wrap;
  }
  .detail-meta .m .lbl { font-size: 9.5px; margin-bottom: 3px; display: block; }
  .detail-meta .m .v { font-family: var(--mono); font-size: 12.5px; color: var(--ink); overflow-wrap: anywhere; }
  .detail-acts { margin-left: auto; display: flex; gap: 10px; align-items: flex-end; }

  /* Empty state */
  .no-results {
    background: var(--sheet);
    border: 1px solid var(--rule);
    padding: 80px 32px;
    text-align: center;
  }
  .no-results .big {
    font-family: var(--serif);
    font-weight: 600;
    font-size: 30px;
    color: var(--ink);
    line-height: 1;
  }
  .no-results .big::after {
    content: "";
    display: block;
    width: 64px;
    border-top: 3px solid var(--ink);
    margin: 18px auto 14px;
  }
  .no-results .small { color: var(--ink-3); font-size: 13px; }

  /* Pagination */
  .pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 18px 0 0;
    border-top: 1px solid var(--rule);
    margin-top: -1px;
  }
  .pagination button {
    background: transparent;
    border: none;
    color: var(--ink);
    font: inherit;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .1em;
    cursor: pointer;
    padding: 6px 0;
  }
  .pagination button:disabled { color: var(--ink-3); opacity: .5; cursor: default; }
  .pagination .page-info {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
    letter-spacing: .04em;
    text-transform: uppercase;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .hidden { display: none !important; }

  @media (max-width: 1100px) {
    .band, .head, .ctl, .main { padding-left: 22px; padding-right: 22px; }
    .head-top { flex-direction: column; align-items: flex-start; gap: 20px; }
    .cols, .row { grid-template-columns: 92px 124px 56px 1fr 130px; }
    .cols .c-party, .row .party { display: none; }
    .detail-grid { grid-template-columns: 1fr; gap: 22px 0; }
    .row.rel-HIGH::before, .detail.rel-HIGH::before { left: -12px; }
  }
  @media (max-width: 760px) {
    body { font-size: 13.5px; }
    .head-title h1 { font-size: 32px; }
    .cols, .row { grid-template-columns: 86px 1fr 96px; }
    .c-dur, .row .dur, .c-rel, .row .rv { display: none; }
    .cols, .row { grid-template-columns: 110px 1fr 96px; }
  }

  @media print {
    body { background: #fff; }
    .ctl, .pagination, .acts, .detail { display: none !important; }
    .cols { position: static; }
  }
</style>
</head>
<body>
  <div class="band">
    <span class="lbl lbl--ink">Call Index</span>
    <div class="band-right">
      <span class="lbl">Prepared __GEN_DATE__</span>
    </div>
  </div>

  <header class="head">
    <div class="head-top">
      <div class="head-title">
        <h1>__CASE_TITLE_HTML__</h1>
        <div class="case-meta" id="caseMeta"></div>
      </div>
      <div class="stats">
        <div class="cell"><div class="n" id="statCalls">0</div><div class="lbl">Calls</div></div>
        <div class="cell"><div class="n" id="statAudio">0</div><div class="lbl">Audio</div></div>
        <div class="cell"><div class="n" id="statNumbers">0</div><div class="lbl">Numbers</div></div>
        <div class="cell"><div class="n" id="statCues">0</div><div class="lbl">Review Cues</div></div>
      </div>
    </div>
    <div class="tally">
      <span class="rv rv--HIGH" data-tally="HIGH"><b id="tallyHigh">0</b> High</span>
      <span class="rv rv--MEDIUM" data-tally="MEDIUM"><b id="tallyMed">0</b> Medium</span>
      <span class="rv rv--LOW" data-tally="LOW"><b id="tallyLow">0</b> Low</span>
      <span class="tally-range" id="tallyRange"></span>
    </div>
  </header>

  <div class="ctl" id="ctl">
    <div class="ctl-inner">
      <div class="search-wrap">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
          <circle cx="11" cy="11" r="7"/><path d="m20 20-3.4-3.4"/>
        </svg>
        <input type="text" class="search-input" id="searchInput"
               placeholder="Search every word spoken: transcripts, summaries, cues, numbers…" autofocus>
        <span class="search-count" id="searchCount"></span>
      </div>
      <span class="filter-label">From</span>
      <input type="date" class="filter-input" id="dateFrom">
      <span class="filter-label">To</span>
      <input type="date" class="filter-input" id="dateTo">
      <select class="filter-select" id="phoneFilter">
        <option value="">All numbers</option>
      </select>
      <div class="chip-group">
        <button class="chip" data-rel="" id="relAll">All</button>
        <button class="chip" data-rel="HIGH">High</button>
        <button class="chip" data-rel="MEDIUM">Med</button>
        <button class="chip" data-rel="LOW">Low</button>
      </div>
      <button class="chip-clear" id="clearFilters">Clear</button>
    </div>
  </div>

  <main class="main">
    <div class="cols" id="cols">
      <span class="lbl c-rel" data-sort="rel_rank">Relevance<span class="arr"></span></span>
      <span class="lbl" data-sort="call_sort">Date<span class="arr"></span></span>
      <span class="lbl num c-dur" data-sort="duration">Length<span class="arr"></span></span>
      <span class="lbl c-party" data-sort="outside">Outside Party<span class="arr"></span></span>
      <span class="lbl no-sort">Summary · Matches</span>
      <span class="lbl num no-sort">Open</span>
    </div>
    <div id="rows"></div>
    <div class="no-results hidden" id="noResults">
      <div class="big">No matches</div>
      <div class="small">Try a different search term or clear your filters.</div>
    </div>
    <div class="pagination hidden" id="pagination">
      <button id="prevPage">&larr; Previous</button>
      <span class="page-info" id="pageInfo"></span>
      <button id="nextPage">Next &rarr;</button>
    </div>
  </main>

<script>
  const CALLS = __DATA_JSON__;
  const REL_RANK = { HIGH: 3, MEDIUM: 2, LOW: 1, "": 0 };
  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  // Precompute sort/search helpers
  CALLS.forEach(c => {
    c.rel_rank = REL_RANK[c.relevance] || 0;
    c.turns_blob = (c.turns || []).map(t => t[2]).join(' ').toLowerCase();
    c.search_blob = [
      c.filename, c.inmate, c.outside, c.brief_summary,
      c.identity, c.facility, c.outcome, c.call_type,
      (c.notes_cues || []).map(n => (n.quote || '') + ' ' + (n.note || '')).join(' '),
    ].join(' ').toLowerCase() + ' ' + c.turns_blob;
  });

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
  function highlight(text, q) {
    if (!q) return esc(text);
    return esc(text).replace(new RegExp('(' + escRe(q) + ')', 'gi'), '<mark>$1</mark>');
  }
  function secondsToLabel(sec) {
    if (sec == null || isNaN(sec)) return '';
    const s = Math.floor(sec); const m = Math.floor(s / 60); const ss = s % 60;
    if (m >= 60) {
      const h = Math.floor(m / 60);
      return h + ':' + String(m % 60).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
    }
    return m + ':' + String(ss).padStart(2, '0');
  }
  function fmtDate(dt) {  // "YYYY-MM-DD[ HH:MM]" → { d, t }
    if (!dt) return { d: '—', t: '' };
    const m = String(dt).match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/);
    if (!m) return { d: dt, t: '' };
    const d = MONTHS[+m[2] - 1] + ' ' + (+m[3]) + ', ' + m[1];
    let t = '';
    if (m[4] != null) {
      let h = +m[4]; const ap = h >= 12 ? 'PM' : 'AM'; h = h % 12 || 12;
      t = h + ':' + m[5] + ' ' + ap;
    }
    return { d, t };
  }
  function relMark(rel, size) {
    const cls = rel ? 'rv--' + rel : 'rv--none';
    const txt = rel ? rel.charAt(0) + rel.slice(1).toLowerCase() : '—';
    return '<span class="rv ' + cls + '">' + txt + '</span>';
  }
  function viewerUrl(call, timeSec) {
    if (!call.audio_filename) return '';
    let u = 'viewer.html?call=' + encodeURIComponent(call.audio_filename);
    if (timeSec != null && !isNaN(timeSec)) u += '&t=' + encodeURIComponent(secondsToLabel(timeSec));
    return u;
  }
  function pdfUrl(call, page) {
    if (!call.pdf_filename) return '';
    let u = 'transcripts/' + encodeURIComponent(call.pdf_filename);
    if (page) u += '#page=' + page;
    return u;
  }
  function openViewer(call, timeSec) { const u = viewerUrl(call, timeSec); if (u) window.open(u, '_blank'); }
  function openPdf(call, page)      { const u = pdfUrl(call, page);     if (u) window.open(u, '_blank'); }

  // Masthead: stats, tally, case meta
  (function buildMasthead() {
    const totalSec = CALLS.reduce((a, c) => a + (c.duration || 0), 0);
    const cues = CALLS.reduce((a, c) => a + ((c.notes_cues || []).length), 0);
    const numbers = new Set(CALLS.filter(c => c.outside).map(c => c.outside));
    const h = Math.floor(totalSec / 3600), mn = Math.floor((totalSec % 3600) / 60);
    document.getElementById('statCalls').textContent = CALLS.length;
    document.getElementById('statAudio').innerHTML = h
      ? h + '<small>h</small> ' + mn + '<small>m</small>'
      : mn + '<small>m</small>';
    document.getElementById('statNumbers').textContent = numbers.size;
    document.getElementById('statCues').textContent = cues;

    const counts = { HIGH: 0, MEDIUM: 0, LOW: 0 };
    CALLS.forEach(c => { if (counts[c.relevance] != null) counts[c.relevance]++; });
    document.getElementById('tallyHigh').textContent = counts.HIGH;
    document.getElementById('tallyMed').textContent = counts.MEDIUM;
    document.getElementById('tallyLow').textContent = counts.LOW;

    const dates = CALLS.map(c => c.call_date).filter(Boolean).sort();
    if (dates.length) {
      const a = fmtDate(dates[0]), b = fmtDate(dates[dates.length - 1]);
      const range = dates[0] === dates[dates.length - 1] ? a.d : a.d.replace(/, \d{4}$/,'') + ' – ' + b.d;
      document.getElementById('tallyRange').textContent = range.toUpperCase();
    }

    const meta = [];
    const inmates = new Set(CALLS.filter(c => c.inmate).map(c => c.inmate));
    if (inmates.size === 1) meta.push('Defendant: ' + Array.from(inmates)[0]);
    else if (inmates.size > 1) meta.push(inmates.size + ' defendants');
    const facilities = new Set(CALLS.filter(c => c.facility).map(c => c.facility));
    if (facilities.size === 1) meta.push(Array.from(facilities)[0]);
    document.getElementById('caseMeta').innerHTML =
      meta.map(esc).join('<span class="sep">·</span>') || '';
  })();

  // Measure the control bar so the sticky column header snaps to the right offset.
  function measureCtl() {
    const ctl = document.getElementById('ctl');
    document.documentElement.style.setProperty('--ctl-h', ctl.getBoundingClientRect().height + 'px');
  }
  window.addEventListener('resize', measureCtl);
  // The control bar's height can settle late while embedded fonts load.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(measureCtl);

  const state = {
    query: '', dateFrom: '', dateTo: '', phone: '', relevance: '',
    sortKey: 'call_sort', sortDir: 'asc',
    page: 1, pageSize: 50, openIdx: null,
  };

  // Per-open-panel transcript navigation (one panel open at a time)
  const nav = { targets: [], pos: -1 };

  // Phone dropdown
  (function populatePhones() {
    const phones = new Set();
    CALLS.forEach(c => { if (c.outside) phones.add(c.outside); });
    const sel = document.getElementById('phoneFilter');
    Array.from(phones).sort().forEach(p => {
      const o = document.createElement('option'); o.value = p; o.textContent = p; sel.appendChild(o);
    });
  })();

  function getFiltered() {
    const q = state.query.trim().toLowerCase();
    return CALLS.filter(call => {
      if (state.dateFrom || state.dateTo) {
        if (!call.call_date) return false;
        if (state.dateFrom && call.call_date < state.dateFrom) return false;
        if (state.dateTo && call.call_date > state.dateTo) return false;
      }
      if (state.phone && call.outside !== state.phone) return false;
      if (state.relevance && call.relevance !== state.relevance) return false;
      if (q && call.search_blob.indexOf(q) === -1) return false;
      return true;
    });
  }
  function getSorted(rows) {
    const key = state.sortKey;
    const dir = state.sortDir === 'asc' ? 1 : -1;
    return rows.slice().sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir;
      return String(av).localeCompare(String(bv)) * dir;
    });
  }
  function countOccurrences(blob, q) {
    if (!q) return 0;
    let n = 0, i = 0;
    while ((i = blob.indexOf(q, i)) !== -1) { n++; i += q.length; }
    return n;
  }

  function buildExcerpts(call, q) {
    if (!q) return [];
    const qLower = q.toLowerCase();
    const out = [];
    const turns = call.turns || [];
    for (let i = 0; i < turns.length && out.length < 3; i++) {
      const [speaker, start, text] = turns[i];
      const idx = text.toLowerCase().indexOf(qLower);
      if (idx === -1) continue;
      const lo = Math.max(0, idx - 55);
      const hi = Math.min(text.length, idx + q.length + 85);
      let snippet = text.slice(lo, hi);
      if (lo > 0) snippet = '…' + snippet;
      if (hi < text.length) snippet += '…';
      out.push({ start, speaker, snippet });
    }
    if (out.length < 3 && call.brief_summary && call.brief_summary.toLowerCase().indexOf(qLower) !== -1) {
      out.push({ start: null, speaker: 'SUMMARY', snippet: call.brief_summary });
    }
    return out;
  }

  function renderTable() {
    const rowsEl = document.getElementById('rows');
    const noRes = document.getElementById('noResults');
    const colsEl = document.getElementById('cols');
    const countEl = document.getElementById('searchCount');
    const filtered = getFiltered();
    const sorted = getSorted(filtered);
    const q = state.query.trim();

    const pag = document.getElementById('pagination');
    const totalPages = Math.max(1, Math.ceil(sorted.length / state.pageSize));
    if (state.page > totalPages) state.page = totalPages;
    if (sorted.length > state.pageSize) {
      pag.classList.remove('hidden');
      document.getElementById('pageInfo').textContent =
        'Page ' + state.page + ' of ' + totalPages + ' · Showing ' +
        ((state.page - 1) * state.pageSize + 1) + '–' +
        Math.min(state.page * state.pageSize, sorted.length) + ' of ' + sorted.length;
      document.getElementById('prevPage').disabled = state.page <= 1;
      document.getElementById('nextPage').disabled = state.page >= totalPages;
    } else {
      pag.classList.add('hidden');
    }
    const start = (state.page - 1) * state.pageSize;
    const pageRows = sorted.slice(start, start + state.pageSize);

    if (q) {
      const qLower = q.toLowerCase();
      const mentions = sorted.reduce((a, c) => a + countOccurrences(c.turns_blob, qLower), 0);
      countEl.innerHTML = '<b>' + sorted.length + '</b> call' + (sorted.length === 1 ? '' : 's')
        + (mentions ? ' · <b>' + mentions + '</b> mention' + (mentions === 1 ? '' : 's') : '');
    } else {
      countEl.textContent = '';
    }

    if (sorted.length === 0) {
      rowsEl.innerHTML = '';
      colsEl.classList.add('hidden');
      noRes.classList.remove('hidden');
      return;
    }
    colsEl.classList.remove('hidden');
    noRes.classList.add('hidden');

    document.querySelectorAll('#cols [data-sort]').forEach(el => {
      const isSorted = el.dataset.sort === state.sortKey;
      el.classList.toggle('sorted', isSorted);
      const arr = el.querySelector('.arr');
      if (arr) arr.textContent = isSorted ? (state.sortDir === 'asc' ? ' ↑' : ' ↓') : '';
    });

    const frag = document.createDocumentFragment();
    pageRows.forEach(call => {
      frag.appendChild(buildRow(call, q));
      if (state.openIdx === call.index) frag.appendChild(buildDetail(call, q));
    });
    rowsEl.innerHTML = '';
    rowsEl.appendChild(frag);

    if (state.openIdx != null) initTransNav(q);
  }

  function buildRow(call, q) {
    const div = document.createElement('div');
    const isOpen = state.openIdx === call.index;
    div.className = 'row' + (call.relevance ? ' rel-' + call.relevance : '') + (isOpen ? ' open' : '');
    div.dataset.idx = call.index;

    const excerpts = q ? buildExcerpts(call, q) : [];
    let summaryHtml;
    if (excerpts.length) {
      summaryHtml = '<div class="excerpts">' + excerpts.map(ex => {
        const t = ex.start != null ? secondsToLabel(ex.start) : esc(ex.speaker);
        return '<div class="excerpt" data-t="' + (ex.start != null ? ex.start : '') + '">'
          + '<span class="ex-t">' + esc(t) + '</span>'
          + '<span>' + highlight(ex.snippet, q) + '</span>'
          + '</div>';
      }).join('') + '</div>';
    } else if (call.brief_summary) {
      summaryHtml = '<div class="clamp">' + highlight(call.brief_summary, q) + '</div>';
    } else {
      summaryHtml = '<div class="empty">No summary</div>';
    }

    const dt = fmtDate(call.datetime || call.call_date);
    const idShort = call.identity || '';

    div.innerHTML =
      relMark(call.relevance) +
      '<span class="date"><b>' + esc(dt.d) + '</b><span>' + esc(dt.t) + '</span></span>' +
      '<span class="dur">' + esc(call.duration_str || '—') + '</span>' +
      '<span class="party"><b>' + highlight(call.outside || '—', q) + '</b>' +
        (idShort ? '<span title="' + esc(idShort) + '">' + highlight(idShort, q) + '</span>' : '') +
      '</span>' +
      '<span class="sum">' + summaryHtml + '</span>' +
      '<span class="acts">' +
        (call.audio_filename ? '<a class="btn btn--primary" data-action="viewer">Viewer</a>' : '') +
        (call.pdf_filename ? '<a class="btn" data-action="pdf">PDF</a>' : '') +
      '</span>';
    return div;
  }

  function buildDetail(call, q) {
    const div = document.createElement('div');
    div.className = 'detail' + (call.relevance ? ' rel-' + call.relevance : '');
    div.dataset.idx = call.index;

    const briefHtml = call.brief_summary
      ? '<h4>Summary</h4><p class="brief">' + highlight(call.brief_summary, q) + '</p>'
      : '<h4>Summary</h4><p class="brief" style="color:var(--ink-3)">No summary for this call.</p>';
    const idHtml = call.identity
      ? '<div class="id-block"><h4>Outside Party</h4><p>' + highlight(call.identity, q) + '</p></div>'
      : '';

    let cuesHtml = '';
    const cues = call.notes_cues || [];
    if (cues.length) {
      const rows = cues.map((cue, i) => {
        const noteBits = [];
        if (cue.note) noteBits.push('<b>' + highlight(cue.note, q) + '</b>');
        if (cue.quote) noteBits.push('<span class="q">&ldquo;' + highlight(cue.quote, q) + '&rdquo;</span>');
        const citeParts = [];
        if (cue.line_cite) citeParts.push('Tr. ' + esc(cue.line_cite));
        if (cue.page) citeParts.push('<a data-cue-pdf="' + i + '">PDF p.' + cue.page + '</a>');
        return '<tr data-cue-idx="' + i + '">'
          + '<td class="ts">' + esc((cue.timestamp || '').replace(/[\[\]]/g, '')) + '</td>'
          + '<td class="note">' + noteBits.join(': ') + '</td>'
          + '<td class="cite">' + citeParts.join(' · ') + '</td>'
          + '</tr>';
      }).join('');
      cuesHtml = '<div class="cues"><h4>Review Cues: ' + cues.length + '</h4><table>' + rows + '</table></div>';
    } else {
      cuesHtml = '<div class="cues"><h4>Review Cues</h4><p style="margin:0;font-size:13px;color:var(--ink-3)">None flagged for this call.</p></div>';
    }

    // Map cues to their nearest transcript turn for flag markers + jumping.
    const turns = call.turns || [];
    const cueTurnSet = new Set();
    cues.forEach(cue => {
      const ts = cue.timestamp_sec;
      if (ts == null) return;
      let best = -1;
      for (let i = 0; i < turns.length; i++) {
        if (turns[i][1] <= ts + 0.25) best = i; else break;
      }
      if (best >= 0) cueTurnSet.add(best);
    });

    let turnsHtml;
    if (turns.length) {
      const qLower = q ? q.toLowerCase() : '';
      turnsHtml = turns.map((t, i) => {
        const [speaker, start, text] = t;
        const isMatch = qLower && text.toLowerCase().indexOf(qLower) !== -1;
        const cls = 'turn' + (isMatch ? ' is-match' : '') + (cueTurnSet.has(i) ? ' has-cue' : '');
        return '<div class="' + cls + '" data-t="' + start + '" data-i="' + i + '">'
          + '<span class="who">' + esc(speaker) + '<span class="t">' + esc(secondsToLabel(start)) + '</span></span>'
          + '<span class="tx">' + highlight(text, q) + '</span>'
          + '</div>';
      }).join('');
    } else {
      turnsHtml = '<div style="color:var(--ink-3);padding:14px 0">No transcript available.</div>';
    }

    const transHtml =
      '<div class="trans">'
      + '<div class="trans-head"><h4>Transcript</h4><span class="hint">Select any line to open the viewer at that moment</span>'
      + '<div class="trans-nav" id="transNav">'
      + '<span class="nav-label" id="navLabel"></span>'
      + '<button data-nav="-1" title="Previous">‹</button>'
      + '<button data-nav="1" title="Next">›</button>'
      + '</div></div>'
      + '<div class="trans-body" id="transBody">' + turnsHtml + '</div>'
      + '</div>';

    const metaCells = [];
    const push = (label, value) => { if (value) metaCells.push({ label, value }); };
    push('Call', String(call.index + 1).padStart(3, '0') + ' / ' + CALLS.length);
    push('Recorded', call.datetime);
    push('Number', call.outside);
    push('Facility', call.facility);
    push('Outcome', call.outcome);
    push('File', call.filename);
    const metaHtml = metaCells.map(m =>
      '<div class="m"><span class="lbl">' + esc(m.label) + '</span><span class="v">' + esc(m.value) + '</span></div>'
    ).join('');

    const cueFirst = cues.length ? cues[0].timestamp_sec : null;
    const actsHtml =
      '<div class="detail-acts">'
      + (call.pdf_filename ? '<a class="btn" data-action="pdf">Transcript PDF</a>' : '')
      + (call.audio_filename
          ? '<a class="btn btn--primary" data-action="viewer-cue">Open in Viewer'
            + (cueFirst != null ? ' <span class="mono-bit">' + esc(secondsToLabel(cueFirst)) + ' →</span>' : '')
            + '</a>'
          : '')
      + '</div>';

    div.innerHTML =
      '<div class="detail-grid">'
      + '<div>' + briefHtml + idHtml + '</div>'
      + cuesHtml
      + '</div>'
      + transHtml
      + '<div class="detail-meta">' + metaHtml + actsHtml + '</div>';
    return div;
  }

  // ── Transcript match / flag navigation ─────────────────────────────────
  function initTransNav(q) {
    const body = document.getElementById('transBody');
    const navEl = document.getElementById('transNav');
    const label = document.getElementById('navLabel');
    if (!body || !navEl) return;

    const matches = Array.from(body.querySelectorAll('.turn.is-match'));
    const flags = Array.from(body.querySelectorAll('.turn.has-cue'));
    if (q && matches.length) {
      nav.targets = matches;
      label.className = 'nav-label';
      label.textContent = matches.length + ' MATCH' + (matches.length === 1 ? '' : 'ES');
    } else if (flags.length) {
      nav.targets = flags;
      label.className = 'nav-label';
      label.textContent = flags.length + ' FLAGGED';
    } else {
      nav.targets = [];
      navEl.classList.add('hidden');
      return;
    }
    navEl.classList.remove('hidden');
    nav.pos = -1;
    if (q && matches.length) jumpTrans(1);  // land on the first hit immediately
  }

  function jumpTrans(dir) {
    if (!nav.targets.length) return;
    const body = document.getElementById('transBody');
    if (!body) return;
    if (nav.pos >= 0 && nav.targets[nav.pos]) nav.targets[nav.pos].classList.remove('nav-current');
    nav.pos = ((nav.pos + dir) % nav.targets.length + nav.targets.length) % nav.targets.length;
    const el = nav.targets[nav.pos];
    el.classList.add('nav-current');
    body.scrollTop = el.offsetTop - body.clientHeight / 2 + el.clientHeight / 2;
    const label = document.getElementById('navLabel');
    if (label) {
      const base = label.textContent.replace(/^\d+ \/ \d+ · /, '');
      label.textContent = (nav.pos + 1) + ' / ' + nav.targets.length + ' · ' + base;
    }
  }

  // ── Events ─────────────────────────────────────────────────────────────
  const rowsEl = document.getElementById('rows');
  rowsEl.addEventListener('click', e => {
    const navBtn = e.target.closest('[data-nav]');
    if (navBtn) { e.stopPropagation(); jumpTrans(parseInt(navBtn.dataset.nav, 10)); return; }

    const actionEl = e.target.closest('[data-action]');
    if (actionEl) {
      e.stopPropagation();
      const host = actionEl.closest('.row, .detail'); if (!host) return;
      const call = CALLS[parseInt(host.dataset.idx, 10)];
      if (!call) return;
      const action = actionEl.dataset.action;
      if (action === 'viewer') openViewer(call);
      else if (action === 'pdf') openPdf(call);
      else if (action === 'viewer-cue') {
        const cues = call.notes_cues || [];
        openViewer(call, cues.length ? cues[0].timestamp_sec : null);
      }
      return;
    }
    const excerpt = e.target.closest('.excerpt');
    if (excerpt) {
      e.stopPropagation();
      const row = excerpt.closest('.row'); if (!row) return;
      const call = CALLS[parseInt(row.dataset.idx, 10)];
      const t = excerpt.dataset.t;
      openViewer(call, t ? parseFloat(t) : null);
      return;
    }
    const detail = e.target.closest('.detail');
    if (detail) {
      const call = CALLS[parseInt(detail.dataset.idx, 10)];
      if (!call) return;
      const cuePdfLink = e.target.closest('[data-cue-pdf]');
      if (cuePdfLink) {
        e.stopPropagation();
        const cue = call.notes_cues[parseInt(cuePdfLink.dataset.cuePdf, 10)];
        if (cue && cue.page) openPdf(call, cue.page);
        return;
      }
      const cueRow = e.target.closest('[data-cue-idx]');
      if (cueRow) {
        e.stopPropagation();
        const cue = call.notes_cues[parseInt(cueRow.dataset.cueIdx, 10)];
        if (cue) openViewer(call, cue.timestamp_sec);
        return;
      }
      const turn = e.target.closest('.turn');
      if (turn) {
        e.stopPropagation();
        openViewer(call, parseFloat(turn.dataset.t));
        return;
      }
      return;
    }
    const row = e.target.closest('.row');
    if (!row) return;
    const idx = parseInt(row.dataset.idx, 10);
    state.openIdx = state.openIdx === idx ? null : idx;
    renderTable();
  });

  let debounce = null;
  document.getElementById('searchInput').addEventListener('input', e => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { state.query = e.target.value; state.page = 1; renderTable(); }, 180);
  });

  function onFilterChange() { state.page = 1; renderTable(); }
  document.getElementById('dateFrom').addEventListener('change', e => { state.dateFrom = e.target.value; onFilterChange(); });
  document.getElementById('dateTo').addEventListener('change', e => { state.dateTo = e.target.value; onFilterChange(); });
  document.getElementById('phoneFilter').addEventListener('change', e => { state.phone = e.target.value; onFilterChange(); });

  function setRelevance(rel) {
    state.relevance = rel;
    document.querySelectorAll('.chip[data-rel]').forEach(b => b.classList.toggle('active', b.dataset.rel === rel));
    onFilterChange();
  }
  document.querySelectorAll('.chip[data-rel]').forEach(btn => {
    btn.addEventListener('click', () => setRelevance(btn.dataset.rel));
  });
  document.querySelectorAll('.tally [data-tally]').forEach(el => {
    el.addEventListener('click', () => setRelevance(state.relevance === el.dataset.tally ? '' : el.dataset.tally));
  });
  document.getElementById('relAll').classList.add('active');

  document.getElementById('clearFilters').addEventListener('click', () => {
    state.query = ''; state.dateFrom = ''; state.dateTo = '';
    state.phone = ''; state.relevance = '';
    state.page = 1;
    document.getElementById('searchInput').value = '';
    document.getElementById('dateFrom').value = '';
    document.getElementById('dateTo').value = '';
    document.getElementById('phoneFilter').value = '';
    document.querySelectorAll('.chip[data-rel]').forEach(b => b.classList.toggle('active', b.id === 'relAll'));
    renderTable();
  });

  document.querySelectorAll('#cols [data-sort]').forEach(el => {
    el.addEventListener('click', () => {
      const key = el.dataset.sort;
      if (state.sortKey === key) {
        state.sortDir = state.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        state.sortKey = key;
        state.sortDir = (key === 'rel_rank' || key === 'duration') ? 'desc' : 'asc';
      }
      renderTable();
    });
  });

  document.getElementById('prevPage').addEventListener('click', () => {
    if (state.page > 1) { state.page--; renderTable(); window.scrollTo({ top: 0, behavior: 'smooth' }); }
  });
  document.getElementById('nextPage').addEventListener('click', () => {
    state.page++; renderTable(); window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  measureCtl();
  renderTable();
</script>
</body>
</html>
"""
