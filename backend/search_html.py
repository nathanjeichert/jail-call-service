"""
Self-contained search + browse HTML page generator.

This is the client-facing "home page" for a delivery package. It serves
as a sortable/filterable table of every call and as a
full-text search engine that surfaces relevant transcript excerpts. From
any row, clients can jump to the call in the viewer (with audio deep-link
to a specific timestamp) or open the formatted transcript PDF.

All call data is embedded in a <script> JSON blob; no external deps.
"""

import logging
import os
import re
from typing import List, Optional

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


def generate_search_html(calls, case_name: str = "") -> str:
    call_data = _build_call_data(calls)
    data_json = dump_script_safe_json(call_data)
    title = f"{case_name} — Searchable Call Index" if case_name else "Searchable Call Index"

    return _TEMPLATE.replace("__TITLE__", _escape(title)) \
                    .replace("__CASE_NAME__", _escape(case_name or "Jail Call Review")) \
                    .replace("__CALL_COUNT__", str(len(call_data))) \
                    .replace("__DATA_JSON__", data_json)


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


# ─────────────────────────────────────────────────────────────────────────────
# HTML template (single-file, vanilla JS)
# ─────────────────────────────────────────────────────────────────────────────
# Placeholders (string-replaced above) — not Jinja:
#   __TITLE__          page <title>
#   __CASE_NAME__      header case name
#   __CALL_COUNT__     header count
#   __DATA_JSON__      embedded JSON blob

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  :root {
    --ink:         #111315;
    --ink-soft:    #25282b;
    --body:        #3e4449;
    --muted:       #717980;
    --quiet:       #9ba3aa;
    --rule:        #d9dee2;
    --rule-strong: #b7c0c6;
    --paper:       #ffffff;
    --wash:        #f4f7f6;
    --wash-strong: #e6eeee;
    --teal:        #00746b;
    --teal-soft:   #dcefed;
    --teal-bright: #6ad2c5;
    --green:       #1f7a48;
    --amber:       #a97813;
    --red:         #a83242;
    --hi-mark:     #ffe680;
    --match-bg:    #fff7d9;
    --spine-w:     14px;
    --ctl-h:       0px;
  }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: "Avenir Next", Avenir, "Helvetica Neue", Helvetica, Arial, sans-serif;
    color: var(--ink);
    background: var(--wash);
    -webkit-font-smoothing: antialiased;
    font-size: 14.5px;
    line-height: 1.5;
    padding-left: var(--spine-w);
  }
  /* Persistent left "case binder" spine — a quiet motif that echoes the PDF cover */
  body::before {
    content: "";
    position: fixed;
    left: 0; top: 0; bottom: 0;
    width: var(--spine-w);
    background: var(--ink);
    z-index: 80;
  }

  .eyebrow, .label, .col-label {
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 800;
  }

  /* ── Header ────────────────────────────────────────────────────────────── */
  .hdr {
    background: var(--paper);
    padding: 40px 48px 30px;
    border-bottom: 1px solid var(--ink);
    position: relative;
  }
  .hdr-eyebrow {
    color: var(--teal);
    font-size: 10.5px;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-bottom: 14px;
  }
  .hdr-main {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 48px;
  }
  .hdr-left { min-width: 0; flex: 1; }
  .hdr-title {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 36px;
    line-height: 1.06;
    color: var(--ink);
    margin: 0;
    font-weight: normal;
    letter-spacing: -0.005em;
    overflow-wrap: break-word;
  }
  .hdr-rule {
    width: 98px;
    height: 3px;
    background: var(--teal);
    margin-top: 18px;
  }
  .hdr-meta {
    display: flex;
    align-items: stretch;
    gap: 0;
    flex-shrink: 0;
  }
  .hdr-meta-block {
    text-align: right;
    border-left: 1px solid var(--rule);
    padding: 4px 20px 4px 22px;
  }
  .hdr-meta-block:first-child { border-left: none; padding-left: 0; }
  .hdr-meta-label {
    color: var(--muted);
    font-size: 9px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    margin-bottom: 5px;
  }
  .hdr-meta-value {
    color: var(--ink);
    font-family: Georgia, "Times New Roman", serif;
    font-size: 17px;
    font-weight: normal;
    line-height: 1.15;
  }

  /* ── Controls ──────────────────────────────────────────────────────────── */
  .ctl {
    background: var(--paper);
    border-bottom: 1px solid var(--ink);
    padding: 18px 48px;
    position: sticky;
    top: 0;
    z-index: 40;
  }
  .ctl-inner {
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }
  .search-wrap {
    flex: 1 1 340px;
    min-width: 280px;
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--paper);
    border: 1px solid var(--rule-strong);
    padding: 0 16px;
    height: 44px;
    position: relative;
    transition: border-color 0.12s, box-shadow 0.12s;
  }
  .search-wrap:focus-within {
    border-color: var(--ink);
    box-shadow: inset 0 -2px 0 var(--teal);
  }
  .search-wrap svg { color: var(--muted); flex-shrink: 0; }
  .search-input {
    flex: 1;
    border: none;
    outline: none;
    background: transparent;
    font: inherit;
    font-size: 14.5px;
    color: var(--ink);
    height: 100%;
  }
  .search-input::placeholder { color: var(--quiet); }
  .search-count {
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 800;
    white-space: nowrap;
  }

  .filter-sep {
    width: 1px;
    height: 28px;
    background: var(--rule-strong);
    margin: 0 4px;
  }
  .filter-label {
    color: var(--muted);
    font-size: 9.5px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding-right: 2px;
  }
  .filter-input {
    font: inherit;
    font-size: 13px;
    color: var(--ink);
    background: var(--paper);
    border: 1px solid var(--rule-strong);
    padding: 0 10px;
    height: 34px;
    border-radius: 0;
    font-variant-numeric: tabular-nums;
  }
  .filter-select {
    font: inherit;
    font-size: 12.5px;
    color: var(--ink);
    background: var(--paper);
    border: 1px solid var(--rule-strong);
    padding: 0 28px 0 12px;
    height: 34px;
    border-radius: 0;
    min-width: 170px;
    appearance: none;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'><path fill='%23717980' d='M0 0l5 6 5-6z'/></svg>");
    background-repeat: no-repeat;
    background-position: right 10px center;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 600;
  }
  .filter-input:focus, .filter-select:focus {
    outline: none;
    border-color: var(--ink);
    box-shadow: inset 0 -2px 0 var(--teal);
  }

  .chip-group { display: inline-flex; gap: 0; }
  .chip {
    display: inline-flex;
    align-items: center;
    font: inherit;
    font-size: 10.5px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink);
    background: var(--paper);
    border: 1px solid var(--ink);
    padding: 0 14px;
    height: 32px;
    cursor: pointer;
    border-radius: 0;
    transition: background 0.12s, color 0.12s;
  }
  .chip + .chip { border-left: none; }
  .chip:hover { background: var(--wash-strong); }
  .chip.active { background: var(--ink); color: var(--paper); }
  .chip.active[data-rel="HIGH"]   { background: var(--red);   border-color: var(--red); }
  .chip.active[data-rel="MEDIUM"] { background: var(--amber); border-color: var(--amber); }
  .chip.active[data-rel="LOW"]    { background: var(--green); border-color: var(--green); }

  .chip-clear {
    background: transparent;
    border: none;
    color: var(--muted);
    font: inherit;
    font-size: 10.5px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    cursor: pointer;
    padding: 0 4px;
    text-decoration: underline;
    text-underline-offset: 4px;
    text-decoration-thickness: 1px;
  }
  .chip-clear:hover { color: var(--ink); }

  /* ── Main ──────────────────────────────────────────────────────────────── */
  .main { padding: 26px 48px 96px; }

  /* Dossier table */
  table.dossier {
    width: 100%;
    background: var(--paper);
    border: 1px solid var(--ink);
    border-collapse: collapse;
    table-layout: fixed;
  }
  table.dossier thead th {
    background: var(--ink);
    color: var(--paper);
    text-align: left;
    font-size: 10px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    padding: 13px 14px;
    border-right: 1px solid #2b2f33;
    cursor: pointer;
    user-select: none;
    position: sticky;
    top: var(--ctl-h);
    white-space: nowrap;
    z-index: 5;
  }
  table.dossier thead th:last-child { border-right: none; }
  table.dossier thead th:hover { background: #1d2124; }
  table.dossier thead th.sorted { background: #1d2124; }
  table.dossier thead th .sort-arrow {
    display: inline-block;
    margin-left: 6px;
    font-size: 9px;
    color: var(--teal-bright);
    opacity: 0;
    vertical-align: middle;
  }
  table.dossier thead th.sorted .sort-arrow { opacity: 1; }
  table.dossier thead th.no-sort { cursor: default; }
  table.dossier thead th.no-sort:hover { background: var(--ink); }

  table.dossier tbody tr.row {
    cursor: pointer;
    transition: background 0.1s;
    border-bottom: 1px solid var(--rule);
  }
  table.dossier tbody tr.row:hover { background: var(--wash); }
  table.dossier tbody tr.row.open  { background: var(--wash-strong); }
  table.dossier tbody tr.row td {
    padding: 14px 14px;
    vertical-align: middle;
    border-right: 1px solid var(--rule);
    font-size: 13.5px;
    color: var(--ink-soft);
  }
  table.dossier tbody tr.row td:last-child { border-right: none; }
  table.dossier tbody tr.row td:first-child {
    position: relative;
    padding-left: 22px;
  }
  table.dossier tbody tr.row td:first-child::before {
    content: "";
    position: absolute;
    left: 0; top: 0; bottom: 0;
    width: 5px;
    background: transparent;
  }
  table.dossier tbody tr.row.rel-HIGH   td:first-child::before { background: var(--red); }
  table.dossier tbody tr.row.rel-MEDIUM td:first-child::before { background: var(--amber); }
  table.dossier tbody tr.row.rel-LOW    td:first-child::before { background: var(--green); }

  .col-date    { width: 148px; }
  .col-rel     { width: 108px; }
  .col-dur     { width: 86px; text-align: right; }
  .col-outside { width: 146px; }
  .col-summary { width: auto; }
  .col-actions { width: 172px; text-align: right; white-space: nowrap; }

  td.col-date, td.col-dur, td.col-outside {
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  td.col-date { color: var(--ink); font-weight: 600; }

  .rel-pill {
    display: inline-block;
    font-size: 10px;
    font-weight: 800;
    padding: 3px 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--paper);
    border-radius: 0;
  }
  .rel-pill.rel-HIGH   { background: var(--red); }
  .rel-pill.rel-MEDIUM { background: var(--amber); }
  .rel-pill.rel-LOW    { background: var(--green); }
  .rel-none {
    color: var(--quiet);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 800;
  }

  .summary-cell {
    color: var(--ink-soft);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    line-height: 1.5;
    font-size: 13.5px;
  }
  .summary-cell.empty { color: var(--quiet); font-style: italic; }

  .action-btn {
    display: inline-block;
    font-size: 10px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 7px 13px;
    background: var(--paper);
    border: 1px solid var(--ink);
    color: var(--ink);
    text-decoration: none;
    cursor: pointer;
    margin-left: 6px;
    transition: background 0.12s, color 0.12s;
  }
  .action-btn:hover { background: var(--ink); color: var(--paper); }
  .action-btn.primary { background: var(--ink); color: var(--paper); }
  .action-btn.primary:hover { background: var(--teal); border-color: var(--teal); }

  /* ── Detail panel ──────────────────────────────────────────────────────── */
  tr.detail td {
    padding: 0;
    background: var(--wash);
    border-bottom: 1px solid var(--ink);
  }
  .detail-panel {
    padding: 30px 34px 36px;
    border-left: 5px solid var(--teal);
    background: var(--wash);
    animation: fadeIn 0.2s ease;
  }
  @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

  .meta-strip {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    border-top: 1px solid var(--ink);
    border-bottom: 1px solid var(--rule-strong);
    margin-bottom: 24px;
    background: var(--paper);
  }
  .meta-cell {
    padding: 14px 18px;
    border-right: 1px solid var(--rule);
    min-width: 0;
  }
  .meta-cell:last-child { border-right: none; }
  .meta-cell .meta-label {
    color: var(--muted);
    font-size: 9px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    margin-bottom: 6px;
  }
  .meta-cell .meta-value {
    color: var(--ink);
    font-size: 13px;
    font-weight: 700;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .section-heading {
    color: var(--teal);
    font-size: 10px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    margin: 0 0 12px 0;
    padding-bottom: 9px;
    border-bottom: 1px solid var(--rule);
  }

  .detail-grid {
    display: grid;
    grid-template-columns: 1.35fr 1fr;
    gap: 22px;
    margin-bottom: 26px;
  }
  .detail-block {
    background: var(--paper);
    border: 1px solid var(--rule-strong);
    padding: 20px 24px;
  }
  .detail-block p {
    margin: 0;
    font-size: 14px;
    line-height: 1.62;
    color: var(--ink-soft);
    font-family: Georgia, "Times New Roman", serif;
  }

  /* Review cues */
  .cues-block {
    background: var(--paper);
    border: 1px solid var(--rule-strong);
    padding: 20px 24px 8px;
    margin-bottom: 26px;
  }
  .cues-block .section-heading { margin-bottom: 4px; }
  .cues-legend {
    color: var(--muted);
    font-size: 9.5px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 10px;
  }
  .cue {
    display: grid;
    grid-template-columns: 78px 1fr 120px;
    gap: 18px;
    padding: 16px 12px 16px 0;
    border-top: 1px solid var(--rule);
    cursor: pointer;
    align-items: start;
    transition: background 0.12s, padding 0.15s;
    position: relative;
  }
  .cue::before {
    content: "";
    position: absolute;
    left: -12px; top: 12px; bottom: 12px;
    width: 3px;
    background: transparent;
    transition: background 0.12s;
  }
  .cue:hover { background: var(--wash); padding-left: 10px; }
  .cue:hover::before { background: var(--teal); left: -2px; }
  .cue:first-child { border-top: none; }
  .cue-ts {
    color: var(--teal);
    font-family: "SF Mono", "IBM Plex Mono", Menlo, Consolas, monospace;
    font-size: 12px;
    font-weight: 800;
    padding-top: 2px;
    white-space: nowrap;
    letter-spacing: 0.02em;
  }
  .cue-body {
    font-size: 13.5px;
    line-height: 1.6;
    color: var(--ink-soft);
    min-width: 0;
  }
  .cue-speaker {
    display: inline-block;
    color: var(--muted);
    font-size: 9.5px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-right: 8px;
    vertical-align: 1px;
  }
  .cue-quote {
    font-family: Georgia, "Times New Roman", serif;
    font-style: italic;
    color: var(--ink);
  }
  .cue-note {
    display: block;
    margin-top: 5px;
    color: var(--body);
    font-size: 12.5px;
    line-height: 1.55;
  }
  .cue-cite {
    text-align: right;
    font-size: 9.5px;
    color: var(--muted);
    font-family: "SF Mono", "IBM Plex Mono", Menlo, Consolas, monospace;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding-top: 3px;
    line-height: 1.7;
    min-width: 0;
  }
  .cue-cite .cite-line { display: block; }
  .cue-cite a {
    color: var(--teal);
    text-decoration: none;
    border-bottom: 1px solid var(--teal-soft);
    padding-bottom: 1px;
    transition: border-color 0.12s;
  }
  .cue-cite a:hover { border-bottom-color: var(--teal); }

  /* Transcript block */
  .transcript-block {
    background: var(--paper);
    border: 1px solid var(--rule-strong);
  }
  .transcript-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    padding: 16px 24px 12px;
    border-bottom: 1px solid var(--rule);
    gap: 18px;
  }
  .transcript-head .section-heading {
    margin: 0; border-bottom: none; padding-bottom: 0;
  }
  .transcript-head .hint {
    font-size: 9.5px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-weight: 800;
  }
  .transcript-body {
    max-height: 440px;
    overflow-y: auto;
    padding: 16px 24px 22px;
    font-family: "SF Mono", "IBM Plex Mono", Menlo, Consolas, monospace;
    font-size: 12.5px;
    line-height: 1.85;
    color: var(--ink-soft);
  }
  .transcript-body::-webkit-scrollbar { width: 10px; }
  .transcript-body::-webkit-scrollbar-thumb { background: var(--rule-strong); }
  .transcript-body::-webkit-scrollbar-track { background: var(--wash); }

  .ts-turn {
    display: grid;
    grid-template-columns: 52px 130px 1fr;
    gap: 12px;
    padding: 3px 8px;
    cursor: pointer;
    border-left: 2px solid transparent;
    transition: background 0.1s, border-color 0.1s;
  }
  .ts-turn:hover {
    background: var(--wash);
    border-left-color: var(--teal);
  }
  .ts-turn.is-match {
    background: var(--match-bg);
    border-left-color: var(--amber);
  }
  .ts-turn .t {
    color: var(--teal);
    font-size: 11px;
    font-weight: 700;
  }
  .ts-turn .sp {
    color: var(--muted);
    font-size: 10.5px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ts-turn .tx {
    color: var(--ink-soft);
    word-break: break-word;
  }

  /* Match excerpts in the Summary column (search mode) */
  .excerpts { display: flex; flex-direction: column; gap: 5px; }
  .excerpt {
    display: grid;
    grid-template-columns: 46px 1fr;
    gap: 12px;
    padding: 7px 10px;
    background: var(--match-bg);
    border-left: 3px solid var(--amber);
    cursor: pointer;
    font-size: 12.5px;
    line-height: 1.55;
    color: var(--ink-soft);
    transition: background 0.12s;
  }
  .excerpt:hover { background: #ffecb0; }
  .excerpt .ex-t {
    font-family: "SF Mono", "IBM Plex Mono", Menlo, monospace;
    color: var(--muted);
    font-size: 10px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding-top: 1px;
  }

  mark {
    background: var(--hi-mark);
    color: var(--ink);
    padding: 0 1px;
  }

  /* Empty state */
  .no-results {
    background: var(--paper);
    border: 1px solid var(--ink);
    padding: 92px 32px;
    text-align: center;
  }
  .no-results .big {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 34px;
    color: var(--ink);
    margin-bottom: 10px;
    line-height: 1;
  }
  .no-results .big::after {
    content: "";
    display: block;
    width: 72px;
    height: 3px;
    background: var(--teal);
    margin: 16px auto 16px;
  }
  .no-results .small {
    color: var(--muted);
    font-size: 10.5px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    font-weight: 800;
  }

  /* Pagination */
  .pagination {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 28px;
    padding: 32px 0 0;
  }
  .pagination button {
    background: var(--paper);
    border: 1px solid var(--ink);
    color: var(--ink);
    font: inherit;
    font-size: 10px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    padding: 11px 22px;
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
  }
  .pagination button:hover:not(:disabled) {
    background: var(--ink);
    color: var(--paper);
  }
  .pagination button:disabled { opacity: 0.3; cursor: default; }
  .pagination .page-info {
    color: var(--muted);
    font-size: 10px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .hidden { display: none !important; }

  @media (max-width: 1100px) {
    .hdr, .ctl, .main { padding-left: 28px; padding-right: 28px; }
    .col-outside { display: none; }
    .detail-grid { grid-template-columns: 1fr; }
    .hdr-main { flex-direction: column; align-items: flex-start; gap: 22px; }
    .hdr-meta { align-self: stretch; justify-content: flex-start; }
    .hdr-meta-block:first-child { padding-left: 0; }
    .hdr-meta-block { padding-left: 18px; }
  }
  @media (max-width: 760px) {
    body { padding-left: 0; font-size: 13.5px; }
    body::before { display: none; }
    .hdr-title { font-size: 25px; }
    .col-dur, .col-rel { display: none; }
    .cue { grid-template-columns: 60px 1fr; gap: 12px; }
    .cue-cite { grid-column: 1 / -1; text-align: left; padding-top: 0; }
  }

  @media print {
    body { background: var(--paper); padding-left: 0; }
    body::before, .ctl, .pagination, .action-btn { display: none !important; }
    tr.detail { display: none !important; }
    table.dossier thead th { position: static; }
  }
</style>
</head>
<body>
  <header class="hdr">
    <div class="hdr-eyebrow">Searchable Call Index &middot; Review Dossier</div>
    <div class="hdr-main">
      <div class="hdr-left">
        <h1 class="hdr-title">__CASE_NAME__</h1>
        <div class="hdr-rule"></div>
      </div>
      <div class="hdr-meta">
        <div class="hdr-meta-block" id="hdrInmate">
          <div class="hdr-meta-label">Defendant</div>
          <div class="hdr-meta-value" id="hdrInmateVal">&mdash;</div>
        </div>
        <div class="hdr-meta-block">
          <div class="hdr-meta-label">Calls</div>
          <div class="hdr-meta-value">__CALL_COUNT__</div>
        </div>
      </div>
    </div>
  </header>

  <div class="ctl" id="ctl">
    <div class="ctl-inner">
      <div class="search-wrap">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>
        </svg>
        <input type="text" class="search-input" id="searchInput"
               placeholder="Search transcripts, summaries, cues, names, numbers…" autofocus>
        <span class="search-count" id="searchCount"></span>
      </div>
      <div class="filter-sep"></div>
      <span class="filter-label">From</span>
      <input type="date" class="filter-input" id="dateFrom">
      <span class="filter-label">To</span>
      <input type="date" class="filter-input" id="dateTo">
      <select class="filter-select" id="phoneFilter">
        <option value="">All Numbers</option>
      </select>
      <div class="filter-sep"></div>
      <div class="chip-group">
        <button class="chip" data-rel="" id="relAll">All</button>
        <button class="chip" data-rel="HIGH">High</button>
        <button class="chip" data-rel="MEDIUM">Medium</button>
        <button class="chip" data-rel="LOW">Low</button>
      </div>
      <button class="chip-clear" id="clearFilters">Clear</button>
    </div>
  </div>

  <main class="main">
    <table class="dossier" id="callTable">
      <thead>
        <tr>
          <th class="col-date" data-sort="call_sort">Date / Time<span class="sort-arrow"></span></th>
          <th class="col-rel" data-sort="rel_rank">Relevance<span class="sort-arrow"></span></th>
          <th class="col-dur" data-sort="duration">Duration<span class="sort-arrow"></span></th>
          <th class="col-outside" data-sort="outside">Outside<span class="sort-arrow"></span></th>
          <th class="col-summary no-sort">Summary &middot; Matches</th>
          <th class="col-actions no-sort">Open</th>
        </tr>
      </thead>
      <tbody id="callTbody"></tbody>
    </table>
    <div class="no-results hidden" id="noResults">
      <div class="big">No matches</div>
      <div class="small">Try a different search term or clear your filters</div>
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

  // Precompute sort/search helpers
  CALLS.forEach(c => {
    c.rel_rank = REL_RANK[c.relevance] || 0;
    c.search_blob = [
      c.filename, c.inmate, c.outside, c.brief_summary,
      c.identity, c.facility, c.outcome, c.call_type,
      (c.notes_cues || []).map(n => (n.quote || '') + ' ' + (n.note || '')).join(' '),
      (c.turns || []).map(t => t[2]).join(' '),
    ].join(' ').toLowerCase();
  });

  // Populate header defendant cell from the unique inmate set.
  (function setHeaderInmate() {
    const inmates = new Set();
    CALLS.forEach(c => { if (c.inmate) inmates.add(c.inmate); });
    const el = document.getElementById('hdrInmateVal');
    const wrap = document.getElementById('hdrInmate');
    if (inmates.size === 0) {
      wrap.classList.add('hidden');
    } else if (inmates.size === 1) {
      el.textContent = Array.from(inmates)[0];
    } else {
      el.textContent = inmates.size + ' defendants';
    }
  })();

  // Measure the control bar so the sticky table header snaps to the right offset.
  function measureCtl() {
    const ctl = document.getElementById('ctl');
    const h = ctl.getBoundingClientRect().height;
    document.documentElement.style.setProperty('--ctl-h', h + 'px');
  }
  window.addEventListener('resize', measureCtl);

  const state = {
    query: '', dateFrom: '', dateTo: '', phone: '', relevance: '',
    sortKey: 'call_sort', sortDir: 'asc',
    page: 1, pageSize: 50, openIdx: null,
  };

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
  function relPill(rel) {
    if (!rel) return '<span class="rel-none">&mdash;</span>';
    return '<span class="rel-pill rel-' + rel + '">' + rel + '</span>';
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
    const tbody = document.getElementById('callTbody');
    const noRes = document.getElementById('noResults');
    const tbl = document.getElementById('callTable');
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
        'Page ' + state.page + ' of ' + totalPages + ' · ' +
        ((state.page - 1) * state.pageSize + 1) + '–' +
        Math.min(state.page * state.pageSize, sorted.length) + ' of ' + sorted.length;
      document.getElementById('prevPage').disabled = state.page <= 1;
      document.getElementById('nextPage').disabled = state.page >= totalPages;
    } else {
      pag.classList.add('hidden');
    }
    const start = (state.page - 1) * state.pageSize;
    const pageRows = sorted.slice(start, start + state.pageSize);

    countEl.textContent = sorted.length
      ? (sorted.length + (q ? ' match' : ' call') + (sorted.length === 1 ? '' : (q ? 'es' : 's')))
      : '';

    if (sorted.length === 0) {
      tbody.innerHTML = '';
      tbl.classList.add('hidden');
      noRes.classList.remove('hidden');
      return;
    }
    tbl.classList.remove('hidden');
    noRes.classList.add('hidden');

    document.querySelectorAll('table.dossier thead th[data-sort]').forEach(th => {
      const isSorted = th.dataset.sort === state.sortKey;
      th.classList.toggle('sorted', isSorted);
      const arr = th.querySelector('.sort-arrow');
      if (arr) arr.textContent = isSorted ? (state.sortDir === 'asc' ? '▲' : '▼') : '';
    });

    const frag = document.createDocumentFragment();
    pageRows.forEach(call => {
      frag.appendChild(buildRow(call, q));
      if (state.openIdx === call.index) frag.appendChild(buildDetailRow(call, q));
    });
    tbody.innerHTML = '';
    tbody.appendChild(frag);
  }

  function buildRow(call, q) {
    const tr = document.createElement('tr');
    tr.className = 'row' + (call.relevance ? ' rel-' + call.relevance : '') + (state.openIdx === call.index ? ' open' : '');
    tr.dataset.idx = call.index;

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
      summaryHtml = '<div class="summary-cell">' + highlight(call.brief_summary, q) + '</div>';
    } else {
      summaryHtml = '<div class="summary-cell empty">No summary</div>';
    }

    tr.innerHTML =
      '<td class="col-date">' + esc(call.datetime || '—') + '</td>' +
      '<td class="col-rel">' + relPill(call.relevance) + '</td>' +
      '<td class="col-dur">' + esc(call.duration_str || '—') + '</td>' +
      '<td class="col-outside">' + highlight(call.outside || '—', q) + '</td>' +
      '<td class="col-summary">' + summaryHtml + '</td>' +
      '<td class="col-actions">' +
        (call.audio_filename ? '<a class="action-btn primary" data-action="viewer">Viewer</a>' : '') +
        (call.pdf_filename ? '<a class="action-btn" data-action="pdf">PDF</a>' : '') +
      '</td>';
    return tr;
  }

  function buildDetailRow(call, q) {
    const tr = document.createElement('tr');
    tr.className = 'detail';
    const td = document.createElement('td');
    td.colSpan = 6;
    td.innerHTML = buildDetailHtml(call, q);
    tr.appendChild(td);
    return tr;
  }

  function buildDetailHtml(call, q) {
    const metaCells = [];
    const push = (label, value) => { if (value) metaCells.push({label, value}); };
    push('When', call.datetime);
    push('Duration', call.duration_str);
    push('Outside', call.outside);
    push('Outcome', call.outcome);
    push('Facility', call.facility);
    push('Type', call.call_type);
    push('File', call.filename);

    const metaHtml = metaCells.length
      ? '<div class="meta-strip">' + metaCells.map(m =>
          '<div class="meta-cell" title="' + esc(m.value) + '"><div class="meta-label">' + esc(m.label) + '</div>'
          + '<div class="meta-value">' + esc(m.value) + '</div></div>'
        ).join('') + '</div>'
      : '';

    const briefBlock = call.brief_summary
      ? '<div class="detail-block"><div class="section-heading">Brief Summary</div><p>' + highlight(call.brief_summary, q) + '</p></div>'
      : '';
    const idBlock = call.identity
      ? '<div class="detail-block"><div class="section-heading">Identity of Outside Party</div><p>' + highlight(call.identity, q) + '</p></div>'
      : '';
    const blocksHtml = (briefBlock || idBlock)
      ? '<div class="detail-grid">' + briefBlock + idBlock + '</div>'
      : '';

    let cuesHtml = '';
    if ((call.notes_cues || []).length) {
      const items = call.notes_cues.map((cue, i) => {
        const speakerHtml = cue.speaker ? '<span class="cue-speaker">' + esc(cue.speaker) + '</span>' : '';
        const quoteHtml = cue.quote ? '<span class="cue-quote">&ldquo;' + highlight(cue.quote, q) + '&rdquo;</span>' : '';
        const noteHtml = cue.note ? '<span class="cue-note">' + highlight(cue.note, q) + '</span>' : '';
        const citeParts = [];
        if (cue.line_cite) citeParts.push('<span class="cite-line">Tr. ' + esc(cue.line_cite) + '</span>');
        if (cue.page) citeParts.push('<span class="cite-line"><a data-cue-pdf="' + i + '">PDF p.' + cue.page + '</a></span>');
        const citeHtml = citeParts.length ? '<div class="cue-cite">' + citeParts.join('') + '</div>' : '<div></div>';
        return '<div class="cue" data-cue-idx="' + i + '">'
          + '<div class="cue-ts">' + esc(cue.timestamp || '') + '</div>'
          + '<div class="cue-body">' + speakerHtml + quoteHtml + noteHtml + '</div>'
          + citeHtml
          + '</div>';
      }).join('');
      cuesHtml = '<div class="cues-block">'
        + '<div class="section-heading">Review Cues</div>'
        + '<div class="cues-legend">Click any cue to jump to that moment in the viewer</div>'
        + items
        + '</div>';
    }

    const turns = call.turns || [];
    let turnsHtml;
    if (turns.length) {
      const qLower = q ? q.toLowerCase() : '';
      turnsHtml = turns.map(t => {
        const [speaker, start, text] = t;
        const isMatch = qLower && text.toLowerCase().indexOf(qLower) !== -1;
        return '<div class="ts-turn' + (isMatch ? ' is-match' : '') + '" data-t="' + start + '">'
          + '<span class="t">' + esc(secondsToLabel(start)) + '</span>'
          + '<span class="sp">' + esc(speaker) + '</span>'
          + '<span class="tx">' + highlight(text, q) + '</span>'
          + '</div>';
      }).join('');
    } else {
      turnsHtml = '<div style="color:var(--muted)">No transcript available.</div>';
    }
    const transcriptBlock =
      '<div class="transcript-block">'
      + '<div class="transcript-head"><div class="section-heading">Full Transcript</div>'
      + '<span class="hint">Click any line to jump to that moment</span></div>'
      + '<div class="transcript-body">' + turnsHtml + '</div>'
      + '</div>';

    return '<div class="detail-panel">' + metaHtml + blocksHtml + cuesHtml + transcriptBlock + '</div>';
  }

  // ── Events ─────────────────────────────────────────────────────────────
  const tbody = document.getElementById('callTbody');
  tbody.addEventListener('click', e => {
    const actionEl = e.target.closest('[data-action]');
    if (actionEl) {
      e.stopPropagation();
      const row = actionEl.closest('tr.row'); if (!row) return;
      const call = CALLS[parseInt(row.dataset.idx, 10)];
      if (!call) return;
      if (actionEl.dataset.action === 'viewer') openViewer(call);
      else if (actionEl.dataset.action === 'pdf') openPdf(call);
      return;
    }
    const excerpt = e.target.closest('.excerpt');
    if (excerpt) {
      e.stopPropagation();
      const row = excerpt.closest('tr.row'); if (!row) return;
      const call = CALLS[parseInt(row.dataset.idx, 10)];
      const t = excerpt.dataset.t;
      openViewer(call, t ? parseFloat(t) : null);
      return;
    }
    const detailRow = e.target.closest('tr.detail');
    if (detailRow) {
      const cueCard = e.target.closest('.cue');
      const cuePdfLink = e.target.closest('[data-cue-pdf]');
      const tsTurn = e.target.closest('.ts-turn');
      const prevRow = detailRow.previousElementSibling;
      const call = prevRow ? CALLS[parseInt(prevRow.dataset.idx, 10)] : null;
      if (!call) return;
      if (cuePdfLink) {
        e.stopPropagation();
        const cue = call.notes_cues[parseInt(cuePdfLink.dataset.cuePdf, 10)];
        if (cue && cue.page) openPdf(call, cue.page);
        return;
      }
      if (cueCard) {
        e.stopPropagation();
        const cue = call.notes_cues[parseInt(cueCard.dataset.cueIdx, 10)];
        if (cue) openViewer(call, cue.timestamp_sec);
        return;
      }
      if (tsTurn) {
        e.stopPropagation();
        openViewer(call, parseFloat(tsTurn.dataset.t));
        return;
      }
      return;
    }
    const row = e.target.closest('tr.row');
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

  document.querySelectorAll('.chip[data-rel]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.relevance = btn.dataset.rel;
      document.querySelectorAll('.chip[data-rel]').forEach(b => b.classList.toggle('active', b === btn));
      onFilterChange();
    });
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

  document.querySelectorAll('table.dossier thead th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
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
