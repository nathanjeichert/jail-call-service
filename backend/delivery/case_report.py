"""
Case Report PDF generation.

Aggregates per-call summaries into a standalone case-level report:
  - Top findings synthesized by the active summarization engine from
    high/medium relevance call notes
  - Outside-party identity inference (in the same synthesis call)
  - High & medium relevance call cards with hotlinks to the call view + transcript PDFs
  - Frequent caller statistics (with AI-inferred identities)
  - At-a-glance metrics, daily call timeline, relevance distribution

Renders via headless Chromium (backend.pdf_render) with Paged.js paged-media
support, sharing the "Record" design tokens with the other delivery templates.
"""

import asyncio
import concurrent.futures
import io
import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, unquote, urlsplit

from tenacity import retry, stop_after_attempt, wait_random_exponential

from ..case_documents import build_case_documents_block
from ..formatting import (
    format_call_datetime_short,
    format_date_short,
    format_duration,
    format_duration_long,
    format_generated_date,
    shorten,
    shorten_middle,
)
from ..job_settings import extract_case_context
from ..models import CallResult, CaseDocument, Job, call_stem
from ..summarization.base import CaseReportInputs, SummarizationEngine
from ..summarization.schemas import CaseReportResponse
from .call_view import CallView
from .pdf_render import render_pdf
from .templates import render_template
from .theme import theme_css

logger = logging.getLogger(__name__)

# Floor for the findings synthesis input set; if HIGH alone has at least this
# many calls we use only HIGH, otherwise we top up from MEDIUM.
TARGET_FINDINGS_INPUT_COUNT = 10

# Range the synthesis engine is asked to surface
MIN_TOP_FINDINGS = 5
MAX_TOP_FINDINGS = 8

# Cap on per-number identity descriptions sent to the engine (keeps prompt size sane)
MAX_IDENTITY_DESCS_PER_NUMBER = 6

# Hard cap on how long the case-report synthesis call is allowed to take per
# attempt. Tenacity then retries up to 3 times with exponential backoff, so
# the worst-case wall time is bounded.
SYNTHESIS_TIMEOUT_SEC = 120


# Trailing local-target portion of a link annotation URI. Chromium resolves
# relative hrefs against the temp-file URL the HTML was rendered from, so the
# annotations arrive as absolute file:///tmp/.../index.html#call=... URIs;
# a bare relative URI (e.g. from older renderer output) is also accepted.
# Group 1 captures the delivery-relative path: "index.html" with an optional
# #call=...&t=... fragment, or "transcripts/<file>.pdf" with an optional
# #fragment.
_LOCAL_TARGET_RE = re.compile(
    r"(?:^|/)((?:index\.html(?:#.*)?)|(?:transcripts/[^/?#]+\.pdf(?:#.*)?))$"
)


def _extract_local_target(uri: str) -> Optional[str]:
    """Return the delivery-relative target for a case-report local link.

    Links are built upstream with ``urllib.parse.quote``; the percent-encoded
    form is preserved exactly as it appears after the prefix strip (no
    unquote/requote round-trip). URIs that do not end in a local-target
    pattern return None and are left untouched.
    """
    if not uri or urlsplit(uri).scheme not in ("", "file"):
        return None
    match = _LOCAL_TARGET_RE.search(uri)
    return match.group(1) if match else None


def _rewrite_local_links(pdf_bytes: bytes) -> bytes:
    """Preserve the document catalog and make local links delivery-relative.

    GoToR addresses a page in another PDF; URI retains the HTML fragment.
    Launch treats a fragment as part of a filename in some native readers.
    """
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, BooleanObject, DictionaryObject, NameObject, NumberObject, TextStringObject

        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter(clone_from=reader)

        for page in writer.pages:
            annots = page.get("/Annots") or []
            for annot in annots:
                annotation_obj = annot.get_object()
                action = annotation_obj.get("/A")
                if not action:
                    continue
                action_obj = action.get_object()
                uri = action_obj.get("/URI")
                target = _extract_local_target(uri) if isinstance(uri, str) else None
                if not target:
                    continue
                if target.startswith("transcripts/"):
                    path, _, fragment = target.partition("#")
                    page_number = parse_qs(fragment).get("page", ["1"])[0]
                    page_index = max(int(page_number) - 1, 0) if page_number.isdecimal() else 0
                    annotation_obj[NameObject("/A")] = DictionaryObject({
                        NameObject("/S"): NameObject("/GoToR"),
                        NameObject("/F"): TextStringObject(unquote(path)),
                        NameObject("/D"): ArrayObject([NumberObject(page_index), NameObject("/Fit")]),
                        NameObject("/NewWindow"): BooleanObject(True),
                    })
                else:
                    action_obj[NameObject("/URI")] = TextStringObject(target)

        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    except Exception as e:
        logger.warning(
            "Could not make case-report PDF links relative: %s",
            e,
        )
        return pdf_bytes


# ────────────────────────── helpers ──────────────────────────

def _stem(call: CallResult) -> str:
    return call_stem(call.index, call.filename)


def _viewer_link(call: CallResult, timestamp: Optional[str] = None) -> str:
    """Deep link into index.html's call view: ``#call=<mp3>&t=MM:SS``."""
    audio_filename = f"{_stem(call)}.mp3"
    base = f"index.html#call={quote(audio_filename)}"
    if timestamp:
        ts = timestamp.strip("[]").strip()
        if ts:
            base += f"&t={quote(ts)}"
    return base


def _transcript_pdf_link(call: CallResult) -> str:
    return f"transcripts/{quote(_stem(call) + '.pdf')}"


def _format_duration(seconds: Optional[float]) -> str:
    return format_duration(seconds, empty="—")


def _format_call_datetime_short(call: CallResult) -> str:
    return format_call_datetime_short(
        call.call_datetime_str, fallback_date=call.call_date,
    )


def _parse_call_date(call: CallResult) -> Optional[date]:
    if not call.call_date:
        return None
    try:
        return datetime.strptime(call.call_date.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _format_date_range(parsed_dates: List[date]) -> str:
    """Display form of a date span: ``Mar 3 – May 28, 2026`` (or with both
    years when the span crosses a year boundary)."""
    if not parsed_dates:
        return "—"
    start, end = parsed_dates[0], parsed_dates[-1]
    if start == end:
        return format_date_short(start)
    if start.year == end.year:
        return (
            f"{start.strftime('%b')} {start.day} – "
            f"{end.strftime('%b')} {end.day}, {end.year}"
        )
    return f"{format_date_short(start)} – {format_date_short(end)}"


# ────────────────────────── relevance bucketing ──────────────────────────

def _split_by_relevance(views: List[CallView]) -> Dict[str, List[CallView]]:
    buckets: Dict[str, List[CallView]] = {
        "HIGH": [], "MEDIUM": [], "LOW": [], "UNKNOWN": [],
    }
    for view in views:
        rel = (view.relevance or "UNKNOWN").upper()
        if rel not in buckets:
            rel = "UNKNOWN"
        buckets[rel].append(view)
    for k in buckets:
        buckets[k].sort(key=lambda v: (v.call.call_datetime_str or "", v.index))
    return buckets


# ────────────────────────── synthesis input ──────────────────────────

def _select_synthesis_calls(buckets: Dict[str, List[CallView]]) -> List[CallView]:
    high = list(buckets["HIGH"])
    medium = list(buckets["MEDIUM"])
    if len(high) >= TARGET_FINDINGS_INPUT_COUNT:
        return high
    needed = TARGET_FINDINGS_INPUT_COUNT - len(high)
    return high + medium[:needed]


def _format_calls_for_synthesis(views: List[CallView]) -> str:
    blocks = []
    for view in views:
        call = view.call
        parsed = view.sections
        notes = (parsed.get("review_cues") or parsed.get("key_findings") or "(no notes)").strip()
        brief = (parsed.get("call_summary") or "(no brief summary)").strip()
        speakers = (parsed.get("speakers") or "").strip()
        rel = parsed.get("relevance", "?")
        date_str = call.call_datetime_str or "unknown date"
        party = call.outside_number_fmt or call.outside_number or "unknown number"
        duration = _format_duration(call.duration_seconds)

        block = (
            f"=== Call ID {call.index} ===\n"
            f"File: {call.filename}\n"
            f"Date: {date_str} | Outside Party: {party} | Duration: {duration} | Relevance: {rel}\n"
        )
        if speakers:
            block += f"Identity (per-call inference): {speakers}\n"
        block += f"\nNOTES:\n{notes}\n\nBRIEF SUMMARY:\n{brief}\n"
        blocks.append(block)
    return "\n".join(blocks) if blocks else "(no calls in scope for findings synthesis)"


def _collect_identity_inputs(views: List[CallView]) -> Dict[str, List[str]]:
    """Group per-call 'Identity of Outside Party' descriptions by phone number.

    Key is the formatted display number; value is a list of unique-ish
    description strings drawn from each call's per-call summary.
    """
    by_number: Dict[str, List[str]] = defaultdict(list)
    for view in views:
        call = view.call
        number = (call.outside_number or "").strip()
        if not number:
            continue
        display = call.outside_number_fmt or number
        spk = (view.sections.get("speakers") or "").strip()
        if not spk:
            continue
        # Deduplicate identical descriptions per number
        spk_clean = re.sub(r"\s+", " ", spk).strip()
        if spk_clean and spk_clean not in by_number[display]:
            by_number[display].append(spk_clean)
    return dict(by_number)


def _format_numbers_for_synthesis(identity_inputs: Dict[str, List[str]]) -> str:
    if not identity_inputs:
        return "(no per-call identity descriptions available)"
    blocks = []
    for display, descs in identity_inputs.items():
        block_lines = [f"=== Number: {display} ==="]
        block_lines.append("Per-call identity descriptions:")
        for d in descs[:MAX_IDENTITY_DESCS_PER_NUMBER]:
            block_lines.append(f"- {d}")
        blocks.append("\n".join(block_lines))
    return "\n\n".join(blocks)


def _build_case_context(case_name: str,
                        defendant_name: Optional[str],
                        case_context: Optional[str],
                        documents: Optional[List[CaseDocument]]) -> str:
    """The case block of the synthesis prompt.

    ``case_context`` is the operator's typed text only (``extract_case_context``),
    never the per-call summary prompt; the attached documents follow it as the
    same block the per-call prompt carries.
    """
    lines = []
    if case_name:
        lines.append(f"Case Name: {case_name}")
    if defendant_name:
        lines.append(f"Defendant: {defendant_name}")
    context = (case_context or "").strip()
    if context:
        lines.append("\nThe legal team provided this case context:\n" + context)
    block = build_case_documents_block(documents)
    if block:
        lines.append("\n" + block)
    return "\n".join(lines) or "(no case context provided)"


# ────────────────────────── synthesis call ──────────────────────────

def _run_synthesis(
    engine: Optional[SummarizationEngine],
    inputs: CaseReportInputs,
) -> Optional[CaseReportResponse]:
    """Run the case-report synthesis through the active engine.

    Bounds the wall time per attempt via a thread-executor timeout and
    retries a few times with exponential backoff. Returns None when the
    engine is missing or every attempt failed.
    """
    if engine is None:
        logger.warning("Case report synthesis skipped: no summarization engine configured")
        return None

    def _do_request():
        # The engine is async; drive it on a fresh loop inside the worker.
        return asyncio.run(engine.synthesize_case_report(inputs))

    @retry(wait=wait_random_exponential(min=2, max=30), stop=stop_after_attempt(3), reraise=True)
    def _call_with_timeout():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_do_request).result(timeout=SYNTHESIS_TIMEOUT_SEC)

    try:
        response, usage = _call_with_timeout()
    except concurrent.futures.TimeoutError:
        logger.error("Case report synthesis timed out after %ds (per attempt)", SYNTHESIS_TIMEOUT_SEC)
        return None
    except Exception as e:
        logger.error("Case report synthesis call failed after retries: %s", e)
        return None

    logger.info(
        "Case report synthesis tokens — in:%d out:%d thinking:%d",
        usage.input_tokens, usage.output_tokens, usage.thinking_tokens,
    )
    return response


def _format_caller_range(parsed_dates: List[date]) -> str:
    """Compact active-period display for the caller-stats table."""
    if not parsed_dates:
        return "—"
    start, end = parsed_dates[0], parsed_dates[-1]
    if start == end:
        return f"{start.strftime('%b')} {start.day}"
    if start.year == end.year:
        return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}"
    return f"{start.strftime('%b')} {start.year} – {end.strftime('%b')} {end.year}"


# ────────────────────────── caller stats ──────────────────────────

def _build_caller_stats(
    views: List[CallView],
    identity_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    identity_map = identity_map or {}
    by_number: Dict[str, List[CallView]] = defaultdict(list)
    for view in views:
        key = (view.call.outside_number or "").strip() or "(unknown)"
        by_number[key].append(view)

    stats = []
    for number, group in by_number.items():
        calls = [v.call for v in group]
        total_dur = sum((c.duration_seconds or 0) for c in calls)
        rels: List[str] = []
        for v in group:
            rel = v.relevance.upper()
            if rel in ("HIGH", "MEDIUM", "LOW"):
                rels.append(rel)
        rel_counter = Counter(rels)
        max_rel = "—"
        for r in ("HIGH", "MEDIUM", "LOW"):
            if rel_counter.get(r):
                max_rel = r
                break

        parsed_dates = sorted(
            d for d in (_parse_call_date(c) for c in calls) if d is not None
        )
        date_range = _format_caller_range(parsed_dates)

        sample = calls[0]
        display = sample.outside_number_fmt or number

        identity = identity_map.get(display) or {}
        inferred = identity.get("inference", "")
        confidence = identity.get("confidence", "")
        # Hide "Unknown" inferences entirely; better to show nothing than a useless line.
        if inferred and inferred.strip().lower() in ("unknown", "n/a", "none", "-"):
            inferred = ""
            confidence = ""

        stats.append({
            "number": number,
            "display": display,
            "count": len(calls),
            "total_duration_sec": total_dur,
            "total_duration_display": _format_duration(total_dur),
            "max_relevance": max_rel,
            "max_relevance_class": (max_rel.lower() if max_rel in ("HIGH", "MEDIUM", "LOW") else "none"),
            "date_range": date_range,
            "high_count": rel_counter.get("HIGH", 0),
            "medium_count": rel_counter.get("MEDIUM", 0),
            "low_count": rel_counter.get("LOW", 0),
            "inferred": inferred,
            "confidence": confidence,
            "confidence_class": confidence.lower() if confidence else "",
        })

    stats.sort(key=lambda s: (-s["count"], -s["total_duration_sec"]))
    return stats


# ────────────────────────── timeline ──────────────────────────

def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _build_timeline(done_calls: List[CallResult]) -> Optional[Dict[str, Any]]:
    """Build a call-volume timeline at a span-appropriate granularity.

    The coverage window of a delivery is unpredictable — weeks, months, or
    five-plus years — so the bucket unit adapts: days up to ~2 months, weeks
    up to ~13 months, calendar months up to ~5 years, calendar years beyond
    that. Returns None if there are no dated calls; otherwise a dict with
    bucket counts, axis labels, peak metadata, and precomputed SVG bar
    geometry for the template's inline chart.
    """
    date_counts: Counter = Counter()
    for call in done_calls:
        d = _parse_call_date(call)
        if d:
            date_counts[d] += 1

    if not date_counts:
        return None

    sorted_dates = sorted(date_counts.keys())
    start = sorted_dates[0]
    end = sorted_dates[-1]
    span_days = (end - start).days + 1

    if span_days <= 60:
        granularity = "day"
    elif span_days <= 392:
        granularity = "week"
    elif span_days <= 1860:
        granularity = "month"
    else:
        granularity = "year"

    # Build contiguous buckets. Weeks run in 7-day spans from the first call
    # date; months and years are calendar-aligned so their labels are exact.
    buckets: List[Dict[str, Any]] = []
    if granularity in ("day", "week"):
        bin_days = 1 if granularity == "day" else 7
        cursor = start
        while cursor <= end:
            bucket_end = min(cursor + timedelta(days=bin_days - 1), end)
            buckets.append({"start": cursor, "end": bucket_end})
            cursor = bucket_end + timedelta(days=1)
    elif granularity == "month":
        cursor = date(start.year, start.month, 1)
        while cursor <= end:
            nxt = _next_month(cursor)
            buckets.append({"start": cursor, "end": nxt - timedelta(days=1)})
            cursor = nxt
    else:
        for year in range(start.year, end.year + 1):
            buckets.append({"start": date(year, 1, 1), "end": date(year, 12, 31)})

    for b in buckets:
        b["count"] = sum(
            cnt for d, cnt in date_counts.items() if b["start"] <= d <= b["end"]
        )
        s = b["start"]
        if granularity in ("day", "week"):
            b["label"] = f"{s.month}/{s.day}"
        elif granularity == "month":
            b["label"] = f"{s.strftime('%b')} ’{s.strftime('%y')}"
        else:
            b["label"] = str(s.year)

    max_count = max((b["count"] for b in buckets), default=1) or 1
    peak_index = next(i for i, b in enumerate(buckets) if b["count"] == max_count)
    peak = buckets[peak_index]
    if granularity == "day":
        peak_label = format_date_short(peak["start"]).upper()
    elif granularity == "week":
        peak_label = "WEEK OF " + format_date_short(peak["start"]).upper()
    elif granularity == "month":
        peak_label = peak["start"].strftime("%B %Y").upper()
    else:
        peak_label = str(peak["start"].year)

    unit_names = {"day": "Day", "week": "Week", "month": "Month", "year": "Year"}

    return {
        "buckets": buckets,
        "max_count": max_count,
        "granularity": granularity,
        "unit_name": unit_names[granularity],
        "tick_count": len(buckets),
        "span_days": span_days,
        "start_label": format_date_short(start),
        "end_label": format_date_short(end),
        "peak_label": peak_label,
        "svg": _build_timeline_svg(buckets, max_count, peak_index),
    }


def _build_timeline_svg(
    buckets: List[Dict[str, Any]],
    max_count: int,
    peak_index: int,
) -> Dict[str, Any]:
    """Precompute inline-SVG bar-chart geometry for the timeline.

    Hand-rolled SVG (no chart library) prints crisply under Paged.js and
    needs nothing at view time. Coordinates are in a fixed 692×150 viewBox;
    the template stretches it to the content width.
    """
    width, height = 692.0, 150.0
    axis_y, top = 130.0, 14.0
    plot_h = axis_y - top

    n = len(buckets)
    slot = width / n
    bar_w = round(min(34.0, slot * 0.66), 2)

    bars = []
    for i, b in enumerate(buckets):
        if b["count"] <= 0:
            continue
        h = max(2.0, b["count"] / max_count * plot_h)
        x = i * slot + (slot - bar_w) / 2.0
        bars.append({
            "x": round(x, 2),
            "y": round(axis_y - h, 2),
            "w": bar_w,
            "h": round(h, 2),
            "peak": i == peak_index,
            "cx": round(x + bar_w / 2.0, 2),
        })

    # Sparse axis labels: at most ~12, always including the peak bucket.
    step = max(1, -(-n // 12))
    label_indexes = set(range(0, n, step))
    label_indexes.add(peak_index)
    peak_x = peak_index * slot + slot / 2.0
    labels = []
    for i in sorted(label_indexes):
        x = i * slot + slot / 2.0
        if i != peak_index and abs(x - peak_x) < 40.0:
            continue
        labels.append({
            # Clamp so centered text never clips at the viewBox edges.
            "x": round(min(max(x, 20.0), width - 20.0), 2),
            "text": buckets[i]["label"],
            "peak": i == peak_index,
        })

    # One dashed reference line at a round count value.
    grid = None
    if max_count >= 4:
        target = max_count * 0.55
        value = 1
        for candidate in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000):
            if candidate <= target:
                value = candidate
        grid = {
            "y": round(axis_y - (value / max_count * plot_h), 2),
            "label": value,
        }

    peak_bar = next(bar for bar in bars if bar["peak"])
    return {
        "width": width,
        "height": height,
        "axis_y": axis_y,
        "label_y": 144,
        "bars": bars,
        "labels": labels,
        "grid": grid,
        "peak": {"x": peak_bar["cx"], "y": round(peak_bar["y"] - 6.0, 2), "count": max_count},
    }


def _format_duration_stat(seconds: float) -> str:
    """Compact numeral form for stat strips: ``51h 24m`` / ``43m`` / ``50s``."""
    secs = int(seconds or 0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    if m:
        return f"{m}m"
    return f"{s}s"


# ────────────────────────── at-a-glance ──────────────────────────

def _build_at_a_glance(
    views: List[CallView],
    buckets: Dict[str, List[CallView]],
) -> Dict[str, Any]:
    done_calls = [v.call for v in views]
    rel_counts = {
        "HIGH": len(buckets["HIGH"]),
        "MEDIUM": len(buckets["MEDIUM"]),
        "LOW": len(buckets["LOW"]),
        "UNKNOWN": len(buckets["UNKNOWN"]),
    }
    total = sum(rel_counts.values())
    rel_percents = {k: ((v / total * 100) if total else 0) for k, v in rel_counts.items()}

    total_dur = sum((c.duration_seconds or 0) for c in done_calls)
    avg_dur = (total_dur / total) if total else 0

    parsed_dates = sorted(
        d for d in (_parse_call_date(c) for c in done_calls) if d is not None
    )
    date_range = _format_date_range(parsed_dates)

    unique_numbers = len({(c.outside_number or "").strip() for c in done_calls if c.outside_number})

    # Notes flagged across all done calls
    total_notes = 0
    calls_with_notes = 0
    for view in views:
        if view.cues:
            total_notes += len(view.cues)
            calls_with_notes += 1

    # Highlights (for the bottom row of the page)
    most_active_day = None
    most_active_count = 0
    if done_calls:
        per_day: Counter = Counter()
        for c in done_calls:
            d = _parse_call_date(c)
            if d:
                per_day[d] += 1
        if per_day:
            most_active_day, most_active_count = per_day.most_common(1)[0]

    longest_call = None
    longest_dur = 0.0
    for c in done_calls:
        if (c.duration_seconds or 0) > longest_dur:
            longest_dur = c.duration_seconds or 0
            longest_call = c

    return {
        "total_calls": total,
        "rel_counts": rel_counts,
        "rel_percents": rel_percents,
        "total_duration_sec": total_dur,
        "total_duration_display": format_duration_long(total_dur),
        "total_duration_stat": _format_duration_stat(total_dur),
        "avg_duration_display": _format_duration(avg_dur),
        "date_range": date_range,
        "unique_callers": unique_numbers,
        "total_notes": total_notes,
        "calls_with_notes": calls_with_notes,
        "most_active_day_display": (
            format_date_short(most_active_day) if most_active_day else "—"
        ),
        "most_active_day_count": most_active_count,
        "longest_call_display": _format_duration(longest_dur) if longest_call else "—",
        "longest_call_party": (
            (longest_call.outside_number_fmt or longest_call.outside_number or "—")
            if longest_call else "—"
        ),
    }


# ────────────────────────── call cards ──────────────────────────

def _build_call_card(view: CallView) -> Dict[str, Any]:
    call = view.call

    cue_view = []
    for cue in view.cues[:8]:
        cue_view.append({
            "timestamp": cue.get("timestamp", ""),
            "speaker": cue.get("speaker", ""),
            "quote": cue.get("quote", ""),
            "note": cue.get("note", ""),
            "line_cite": cue.get("line_cite", ""),
            "viewer_link": _viewer_link(call, cue.get("timestamp", "")),
        })

    speakers = view.identity
    brief = view.brief

    return {
        "call_index": call.index + 1,
        "filename": call.filename,
        "datetime": _format_call_datetime_short(call),
        "duration": _format_duration(call.duration_seconds),
        "party": call.outside_number_fmt or call.outside_number or "—",
        "speakers": speakers,
        "brief": brief,
        "cues": cue_view,
        "viewer_link": _viewer_link(call),
        "pdf_link": _transcript_pdf_link(call),
        "has_cues": bool(cue_view),
    }


_FINDING_TS_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def _finding_sources(item, views_by_id: Dict[int, CallView]) -> List[dict]:
    """One cite row per (call, moment) a finding names: the finding's own
    call first, then its ``sources``. A well-formed timestamp always deep
    links the viewer; the transcript PDF opens at the cited page only when
    the timestamp is one of that call's notes (the prompt asks for exactly
    those, but a model that drifts by a second still gets a viewer link)."""
    sources, seen = [], set()
    for source in [item, *item.sources]:
        view = views_by_id.get(source.call_id)
        if not view:
            continue
        ts = (source.timestamp or "").strip().strip("[]").strip()
        if not _FINDING_TS_RE.match(ts):
            ts = ""
        cue = next((c for c in view.cues if (c.get("timestamp") or "").strip("[]") == ts), None) if ts else None
        key = (view.index, ts)
        if key in seen:
            continue
        seen.add(key)
        pdf_link = _transcript_pdf_link(view.call)
        if cue and cue.get("page"):
            pdf_link += f"#page={view.pdf_front_matter_pages + cue['page']}"
        sources.append({
            "call_index": view.index + 1,
            "call_date": _format_call_datetime_short(view.call),
            "timestamp": ts,
            "viewer_link": _viewer_link(view.call, ts or None),
            "pdf_link": pdf_link,
        })
    return sources


# ────────────────────────── main entry ──────────────────────────

def generate_case_report_pdf(
    job: Job,
    views: List[CallView],
    engine: SummarizationEngine,
    gen_date: Optional[str] = None,
) -> bytes:
    """Build the case report PDF for a completed job.

    ``views`` are the completed calls' :class:`CallView` objects (summary
    parsed, cues hydrated, layout computed once by the delivery stage).
    """
    gen_date = gen_date or format_generated_date()

    case_name = (job.case_name or "Untitled Case").strip() or "Untitled Case"
    defendant_name = (job.defendant_name or "").strip()
    done_calls = [v.call for v in views]

    rel_buckets = _split_by_relevance(views)
    glance = _build_at_a_glance(views, rel_buckets)
    timeline = _build_timeline(done_calls)

    # One synthesis call: top findings AND identity inference together.
    synthesis_inputs = _select_synthesis_calls(rel_buckets)
    identity_inputs = _collect_identity_inputs(views)

    findings: List[Dict[str, Any]] = []
    identity_map: Dict[str, Dict[str, str]] = {}
    synthesis_state = "no_input"

    if synthesis_inputs or identity_inputs:
        calls_by_id = {v.index: v for v in synthesis_inputs}
        views_by_id = {v.index: v for v in views}
        response = _run_synthesis(engine, CaseReportInputs(
            case_context=_build_case_context(
                case_name, defendant_name, extract_case_context(job.summary_prompt), job.case_documents,
            ),
            calls_block=_format_calls_for_synthesis(synthesis_inputs),
            numbers_block=_format_numbers_for_synthesis(identity_inputs),
            min_findings=MIN_TOP_FINDINGS,
            max_findings=MAX_TOP_FINDINGS,
        ))
        if response is None:
            synthesis_state = "synth_unavailable"
        else:
            for item in response.findings:
                view = calls_by_id.get(item.call_id)
                if not view:
                    continue
                findings.append({
                    "headline": item.headline.strip(),
                    "detail": item.detail.strip(),
                    "sources": _finding_sources(item, views_by_id),
                })
            identity_map = {
                item.number: {"inference": item.inference, "confidence": item.confidence or ""}
                for item in response.identities
            }
            synthesis_state = "ok" if (findings or identity_map) else "parse_failed"

    callers = _build_caller_stats(views, identity_map)
    top_callers = callers[:10]

    high_cards = [_build_call_card(view) for view in rel_buckets["HIGH"]]

    medium_rows = []
    for view in rel_buckets["MEDIUM"]:
        call = view.call
        brief = view.brief or "—"
        cue_count = len(view.cues)
        medium_rows.append({
            "call_index": call.index + 1,
            # Middle-truncated to one line of the mono column (120pt minus
            # 12pt padding at 7.1pt IBM Plex Mono ≈ 25 chars); a tail spilling
            # one or two characters onto a second line reads badly.
            "filename": shorten_middle(call.filename, 24),
            "datetime": _format_call_datetime_short(call),
            "duration": _format_duration(call.duration_seconds),
            "party": call.outside_number_fmt or call.outside_number or "—",
            "brief": brief,
            "cue_count": cue_count,
            "viewer_link": _viewer_link(call),
            "pdf_link": _transcript_pdf_link(call),
        })

    # Court-caption style cover title when the case name parses as
    # "<party> v. <party>"; otherwise the case name renders on one line.
    caption_match = re.match(
        r"^(.{2,80}?)\s+vs?\.?\s+(.{2,80})$", case_name, re.IGNORECASE
    )
    case_caption = (
        {"left": caption_match.group(1).strip(), "right": caption_match.group(2).strip()}
        if caption_match
        else None
    )

    ctx = {
        "theme_css": theme_css("print"),
        "case_name": case_name,
        "case_name_short": shorten(case_name, 38),
        "case_caption": case_caption,
        "defendant_name": defendant_name,
        "gen_date": gen_date,
        "glance": glance,
        "timeline": timeline,
        "findings": findings,
        "synthesis_state": synthesis_state,
        "synthesis_input_count": len(synthesis_inputs),
        "synthesis_used_medium": len(synthesis_inputs) > len(rel_buckets["HIGH"]),
        "high_cards": high_cards,
        "high_count": len(rel_buckets["HIGH"]),
        "medium_rows": medium_rows,
        "medium_count": len(rel_buckets["MEDIUM"]),
        "low_count": len(rel_buckets["LOW"]),
        "unknown_count": len(rel_buckets["UNKNOWN"]),
        "top_callers": top_callers,
        "total_caller_count": len(callers),
        "identity_inferred_count": sum(1 for c in callers if c.get("inferred")),
    }

    html_str = render_template("case_report.html", **ctx)
    # Chromium resolves the relative <a href> values ("index.html#call=...",
    # "transcripts/xxx.pdf") against the temp file it renders from, baking
    # absolute file:///tmp/... URIs into the link annotations. The rewriter
    # below strips that machine-specific prefix while retaining destinations
    # and the action type appropriate to each target. Reader policies may
    # still require permission to open another local document.
    raw_pdf = render_pdf(html_str, paged=True)
    return _rewrite_local_links(raw_pdf)
