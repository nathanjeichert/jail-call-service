"""
Case Report PDF generation.

Aggregates per-call summaries into a standalone case-level report:
  - Top findings synthesized by the active summarization engine (Gemini or Gemma)
    from high/medium relevance call notes
  - Outside-party identity inference (in the same synthesis call)
  - High & medium relevance call cards with hotlinks to viewer + transcript PDFs
  - Frequent caller statistics (with AI-inferred identities)
  - At-a-glance metrics, daily call timeline, relevance distribution

Renders via WeasyPrint, sharing all design tokens with pdf_cover_template.html
and guide_template.html.
"""

import asyncio
import concurrent.futures
import logging
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from tenacity import retry, stop_after_attempt, wait_random_exponential

from . import config as cfg
from . import pdf_utils as U
from .models import CallResult, Job, call_stem
from .summarization.base import SummarizationEngine

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


CASE_REPORT_SYNTHESIS_PROMPT = """You are synthesizing analysis of multiple jail phone call transcripts for a legal team.

CASE CONTEXT:
{case_context}

You have TWO tasks. Complete BOTH in your single response.

═══════════════════════════════════════════════════════════════════
TASK 1 — TOP FINDINGS
═══════════════════════════════════════════════════════════════════

From the calls in INPUT_CALLS below, identify between {min_findings} and {max_findings} findings that are MOST consequential for case strategy — the things a reviewing attorney must know first when picking up this case. Quality over quantity. Each finding should tie to a specific moment in a specific call when possible.

VOICE FOR HEADLINE AND DETAIL:
- The audience is a legal professional reviewing the case — this may be DEFENSE counsel OR PROSECUTION. Write in a neutral, objective, reader-agnostic tone. Do not take sides.
- Narrate in third person, neutral past-tense factual prose. Refer to participants by role or name (e.g. "the defendant", "Lowe", "the outside party", "Seth").
- NEVER use the second person ("you", "your", "yours"). The defendant is NEVER "you" — even when the transcript has the defendant speaking in the first person. If the outside party told the defendant something, write "the outside party told the defendant…", NOT "the outside party informs you…".
- NEVER use imperatives directed at the reader ("review this", "note that", "see", "click here").
- Do not include recommendations, legal advice, strategic suggestions, or commentary about the report itself. Just state what happened and why it matters.

CRITERIA (what makes a finding important):
- Discussion of charges, alleged offense, or related criminal conduct
- Admissions, statements against interest, contradictions of prior statements
- Mentions of co-defendants, witnesses, victims, evidence, or alleged accomplices
- Discussion of legal strategy, plea offers, defense theories, or counsel
- Statements bearing on credibility, intent, motive, or state of mind
- Statements about confinement, custody status, bail, or release conditions
- Statements that contradict or corroborate the prosecution's theory of the case
- Coded, evasive, or guarded language that appears tied to the case
- DO NOT highlight routine personal conversation, family logistics, or banter unless it directly bears on the above

Output each finding using EXACTLY this structure, in priority order, separated by a single blank line:

FINDING_START
CALL_ID: <integer call id from INPUT_CALLS>
HEADLINE: <4-9 word title in title case>
TIMESTAMP: <[MM:SS] from that call's notes if a specific moment, otherwise NONE>
DETAIL: <one to three sentences explaining what was said and why it matters>
FINDING_END

The TIMESTAMP must be one that already appears in that call's NOTES, or NONE. Do not invent timestamps. If absolutely nothing in the input warrants attorney attention, return a single FINDING_START / FINDING_END block with HEADLINE: NONE.

═══════════════════════════════════════════════════════════════════
TASK 2 — OUTSIDE PARTY IDENTITY INFERENCE
═══════════════════════════════════════════════════════════════════

For each unique outside number listed in INPUT_NUMBERS below, the per-call analysis pass produced one or more candidate identity descriptions. Synthesize the SINGLE best inferred identity that balances accuracy and usefulness across all of that number's descriptions.

GUIDELINES:
- Where the descriptions clearly support BOTH a name AND a role/relationship, return both, e.g. "Kate, significant other" or "Sandra (mother)".
- Where only a role is supportable, return just the role, e.g. "Defense attorney" or "Mother of defendant".
- Where only a name is supportable, return just the name.
- When descriptions disagree, prefer the more conservative inference. Do not invent details.
- If nothing reliable can be said about a number, return INFERENCE: Unknown.
- CONFIDENCE: HIGH only when multiple descriptions converge on the same identity. MEDIUM when one or two descriptions support the inference but with some ambiguity. LOW when the inference is a guess.
- Keep INFERENCE strings short — under 60 characters.

Output each inferred identity using EXACTLY this structure:

IDENTITY_START
NUMBER: <the phone number string, copied verbatim from INPUT_NUMBERS>
INFERENCE: <name and/or role, or Unknown>
CONFIDENCE: <HIGH | MEDIUM | LOW>
IDENTITY_END

═══════════════════════════════════════════════════════════════════
OUTPUT ORDER
═══════════════════════════════════════════════════════════════════

First emit ALL FINDING blocks (Task 1), then emit ALL IDENTITY blocks (Task 2). Nothing else before, between, or after — your entire response is just blocks.

═══════════════════════════════════════════════════════════════════
INPUT_CALLS
═══════════════════════════════════════════════════════════════════
{calls_block}

═══════════════════════════════════════════════════════════════════
INPUT_NUMBERS
═══════════════════════════════════════════════════════════════════
{numbers_block}
"""


# ────────────────────────── helpers ──────────────────────────

def _stem(call: CallResult) -> str:
    return call_stem(call.index, call.filename)


def _viewer_link(call: CallResult, timestamp: Optional[str] = None) -> str:
    audio_filename = f"{_stem(call)}.mp3"
    base = f"viewer.html?call={quote(audio_filename)}"
    if timestamp:
        ts = timestamp.strip("[]").strip()
        if ts:
            base += f"&t={quote(ts)}"
    return base


def _transcript_pdf_link(call: CallResult) -> str:
    return f"transcripts/{quote(_stem(call) + '.pdf')}"


def _format_duration(seconds: Optional[float]) -> str:
    return U.format_duration(seconds, empty="—")


def _format_call_datetime_short(call: CallResult) -> str:
    return U.format_call_datetime_short(
        call.call_datetime_str, fallback_date=call.call_date,
    )


def _parse_call_date(call: CallResult) -> Optional[date]:
    if not call.call_date:
        return None
    try:
        return datetime.strptime(call.call_date.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ────────────────────────── relevance bucketing ──────────────────────────

def _split_by_relevance(
    done_calls: List[CallResult],
    parsed_by_index: Dict[int, dict],
) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "HIGH": [], "MEDIUM": [], "LOW": [], "UNKNOWN": [],
    }
    for call in done_calls:
        parsed = parsed_by_index.get(call.index, {})
        if not parsed:
            buckets["UNKNOWN"].append({"call": call, "parsed": {}})
            continue
        rel = (parsed.get("relevance") or "UNKNOWN").upper()
        if rel not in buckets:
            rel = "UNKNOWN"
        buckets[rel].append({"call": call, "parsed": parsed})
    for k in buckets:
        buckets[k].sort(key=lambda e: (e["call"].call_datetime_str or "", e["call"].index))
    return buckets


# ────────────────────────── synthesis input ──────────────────────────

def _select_synthesis_calls(buckets: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    high = list(buckets["HIGH"])
    medium = list(buckets["MEDIUM"])
    if len(high) >= TARGET_FINDINGS_INPUT_COUNT:
        return high
    needed = TARGET_FINDINGS_INPUT_COUNT - len(high)
    return high + medium[:needed]


def _format_calls_for_synthesis(call_entries: List[Dict[str, Any]]) -> str:
    blocks = []
    for entry in call_entries:
        call = entry["call"]
        parsed = entry["parsed"]
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


def _collect_identity_inputs(
    done_calls: List[CallResult],
    parsed_by_index: Dict[int, dict],
) -> Dict[str, List[str]]:
    """Group per-call 'Identity of Outside Party' descriptions by phone number.

    Key is the formatted display number; value is a list of unique-ish
    description strings drawn from each call's per-call summary.
    """
    by_number: Dict[str, List[str]] = defaultdict(list)
    for call in done_calls:
        number = (call.outside_number or "").strip()
        if not number:
            continue
        display = call.outside_number_fmt or number
        parsed = parsed_by_index.get(call.index, {})
        spk = (parsed.get("speakers") or "").strip()
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
                        summary_prompt: Optional[str]) -> str:
    lines = []
    if case_name:
        lines.append(f"Case Name: {case_name}")
    if defendant_name:
        lines.append(f"Defendant: {defendant_name}")
    if summary_prompt and summary_prompt.strip() and summary_prompt.strip() != cfg.DEFAULT_SUMMARY_PROMPT.strip():
        lines.append(
            "\nThe attorney provided this custom analysis prompt for per-call review.\n"
            "Use it to understand what the attorney considers relevant, but do not\n"
            "treat the prompt's instructions as facts about the case:\n"
            "---\n" + summary_prompt.strip() + "\n---"
        )
    return "\n".join(lines) or "(no case context provided)"


# ────────────────────────── synthesis call ──────────────────────────

def _call_synth(engine: Optional[SummarizationEngine], prompt_text: str) -> Optional[str]:
    """Run the case-report synthesis prompt through the active engine.

    Bounds the wall time per attempt via a thread-executor timeout and retries
    a few times with exponential backoff — same envelope we used when this
    always went through Gemini, now routed generically.
    """
    if engine is None:
        logger.warning("Case report synthesis skipped: no summarization engine configured")
        return None

    def _do_request():
        # engine.generate is async; drive it on a fresh loop inside the worker.
        return asyncio.run(engine.generate(prompt_text))

    @retry(
        wait=wait_random_exponential(min=2, max=30),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _call_with_timeout():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do_request)
            return future.result(timeout=SYNTHESIS_TIMEOUT_SEC)

    try:
        result = _call_with_timeout()
    except concurrent.futures.TimeoutError:
        logger.error(
            "Case report synthesis timed out after %ds (per attempt)",
            SYNTHESIS_TIMEOUT_SEC,
        )
        return None
    except Exception as e:
        logger.error("Case report synthesis call failed after retries: %s", e)
        return None

    logger.info(
        "Case report synthesis tokens — in:%d out:%d thinking:%d",
        result.get("input_tokens", 0),
        result.get("output_tokens", 0),
        result.get("thinking_tokens", 0),
    )

    text = (result.get("text") or "").strip()
    return text or None


# ────────────────────────── output parsers ──────────────────────────

FINDING_BLOCK_RE = re.compile(
    r"FINDING_START\s*(.*?)\s*FINDING_END",
    re.DOTALL | re.IGNORECASE,
)
SIMPLE_FINDING_FIELD_RE = re.compile(
    r"^(CALL_ID|HEADLINE|TIMESTAMP):\s*(.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

IDENTITY_BLOCK_RE = re.compile(
    r"IDENTITY_START\s*(.*?)\s*IDENTITY_END",
    re.DOTALL | re.IGNORECASE,
)
IDENTITY_FIELD_RE = re.compile(
    r"^(NUMBER|INFERENCE|CONFIDENCE):\s*(.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_findings(text: str) -> List[Dict[str, str]]:
    findings: List[Dict[str, str]] = []
    for match in FINDING_BLOCK_RE.finditer(text):
        block = match.group(1).strip()
        fields: Dict[str, str] = {}
        detail_split = re.split(
            r"^DETAIL:\s*", block, maxsplit=1, flags=re.MULTILINE | re.IGNORECASE
        )
        if len(detail_split) == 2:
            head, detail = detail_split
            fields["DETAIL"] = detail.strip()
        else:
            head = block
        for fmatch in SIMPLE_FINDING_FIELD_RE.finditer(head):
            fields[fmatch.group(1).upper()] = fmatch.group(2).strip()
        if fields.get("HEADLINE", "").upper() == "NONE":
            continue
        if fields:
            findings.append(fields)
    return findings


def _parse_identities(text: str) -> Dict[str, Dict[str, str]]:
    """Returns {display_number: {inference, confidence}}."""
    results: Dict[str, Dict[str, str]] = {}
    for match in IDENTITY_BLOCK_RE.finditer(text):
        block = match.group(1).strip()
        fields: Dict[str, str] = {}
        for fmatch in IDENTITY_FIELD_RE.finditer(block):
            fields[fmatch.group(1).upper()] = fmatch.group(2).strip()
        number = fields.get("NUMBER", "").strip()
        inference = fields.get("INFERENCE", "").strip()
        if not number or not inference:
            continue
        confidence = fields.get("CONFIDENCE", "").strip().upper()
        if confidence not in ("HIGH", "MEDIUM", "LOW"):
            confidence = ""
        results[number] = {
            "inference": inference,
            "confidence": confidence,
        }
    return results


# ────────────────────────── caller stats ──────────────────────────

def _build_caller_stats(
    done_calls: List[CallResult],
    parsed_by_index: Dict[int, dict],
    identity_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict[str, Any]]:
    identity_map = identity_map or {}
    by_number: Dict[str, List[CallResult]] = defaultdict(list)
    for call in done_calls:
        key = (call.outside_number or "").strip() or "(unknown)"
        by_number[key].append(call)

    stats = []
    for number, calls in by_number.items():
        total_dur = sum((c.duration_seconds or 0) for c in calls)
        rels: List[str] = []
        for c in calls:
            parsed = parsed_by_index.get(c.index, {})
            rel = (parsed.get("relevance") or "").upper()
            if rel in ("HIGH", "MEDIUM", "LOW"):
                rels.append(rel)
        rel_counter = Counter(rels)
        max_rel = "—"
        for r in ("HIGH", "MEDIUM", "LOW"):
            if rel_counter.get(r):
                max_rel = r
                break

        dates = sorted([c.call_date for c in calls if c.call_date])
        if dates:
            date_range = dates[0] if dates[0] == dates[-1] else f"{dates[0]} – {dates[-1]}"
        else:
            date_range = "—"

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

def _build_timeline(done_calls: List[CallResult]) -> Optional[Dict[str, Any]]:
    """Build a daily/weekly/monthly call volume timeline.

    Returns None if there are no dated calls. Otherwise returns a dict with
    `buckets` (list of {label, count, height_pct}), tick width pct, span
    metadata, and axis labels suitable for the template.
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

    # Pick granularity
    if span_days <= 60:
        granularity = "day"
        bin_days = 1
    elif span_days <= 365:
        granularity = "week"
        bin_days = 7
    else:
        granularity = "month"
        bin_days = 30

    # Build buckets — each bucket is a contiguous span of bin_days days,
    # closed on the left and open on the right (except the last one).
    buckets: List[Dict[str, Any]] = []
    cursor = start
    while cursor <= end:
        bucket_end = cursor + timedelta(days=bin_days - 1)
        if bucket_end > end:
            bucket_end = end
        count = sum(
            cnt for d, cnt in date_counts.items()
            if cursor <= d <= bucket_end
        )
        buckets.append({
            "start": cursor,
            "end": bucket_end,
            "count": count,
        })
        cursor = bucket_end + timedelta(days=1)

    max_count = max((b["count"] for b in buckets), default=1) or 1

    # Bar heights in inches, computed here so the template can use absolute
    # units (WeasyPrint does not reliably propagate percent heights into
    # nested elements inside table cells).
    BAR_AREA_IN = 0.85
    MIN_BAR_IN = 0.13
    for b in buckets:
        if b["count"] > 0:
            raw = b["count"] / max_count * BAR_AREA_IN
            b["height_in"] = max(MIN_BAR_IN, raw)
        else:
            b["height_in"] = 0.0
        if granularity == "day":
            b["label"] = b["start"].strftime("%b %-d")
        elif granularity == "week":
            b["label"] = "Wk of " + b["start"].strftime("%b %-d")
        else:
            b["label"] = b["start"].strftime("%b %Y")

    tick_width_pct = 100.0 / max(len(buckets), 1)
    # Pre-compute the left offset of each tick so the template can use
    # absolute positioning rather than relying on inline-block layout.
    for i, b in enumerate(buckets):
        b["left_pct"] = i * tick_width_pct

    # Mid-axis label: choose the bucket closest to the middle
    mid_bucket = buckets[len(buckets) // 2] if buckets else None
    mid_label = mid_bucket["label"] if mid_bucket else ""

    return {
        "buckets": buckets,
        "tick_width_pct": tick_width_pct,
        "max_count": max_count,
        "granularity": granularity,
        "granularity_plural": (
            "days" if granularity == "day"
            else "weeks" if granularity == "week"
            else "months"
        ),
        "tick_count": len(buckets),
        "span_days": span_days,
        "start_label": U.format_date_short(start),
        "end_label": U.format_date_short(end),
        "mid_label": mid_label,
        "show_mid_label": len(buckets) >= 5,
    }


# ────────────────────────── at-a-glance ──────────────────────────

def _build_at_a_glance(
    done_calls: List[CallResult],
    parsed_by_index: Dict[int, dict],
    buckets: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
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

    dates = sorted([c.call_date for c in done_calls if c.call_date])
    if dates:
        date_range = dates[0] if dates[0] == dates[-1] else f"{dates[0]} – {dates[-1]}"
    else:
        date_range = "—"

    unique_numbers = len({(c.outside_number or "").strip() for c in done_calls if c.outside_number})

    # Notes flagged across all done calls
    total_notes = 0
    calls_with_notes = 0
    for call in done_calls:
        parsed = parsed_by_index.get(call.index, {})
        cues = parsed.get("review_cue_items", []) or []
        if cues:
            total_notes += len(cues)
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
        "total_duration_display": U.format_duration_long(total_dur),
        "avg_duration_display": _format_duration(avg_dur),
        "date_range": date_range,
        "unique_callers": unique_numbers,
        "total_notes": total_notes,
        "calls_with_notes": calls_with_notes,
        "most_active_day_display": (
            U.format_date_short(most_active_day) if most_active_day else "—"
        ),
        "most_active_day_count": most_active_count,
        "longest_call_display": _format_duration(longest_dur) if longest_call else "—",
        "longest_call_party": (
            (longest_call.outside_number_fmt or longest_call.outside_number or "—")
            if longest_call else "—"
        ),
    }


# ────────────────────────── call cards ──────────────────────────

def _build_call_card(entry: Dict[str, Any]) -> Dict[str, Any]:
    call = entry["call"]
    parsed = entry["parsed"]

    cues = parsed.get("review_cue_items", []) or []
    cue_view = []
    for cue in cues[:8]:
        cue_view.append({
            "timestamp": cue.get("timestamp", ""),
            "speaker": cue.get("speaker", ""),
            "quote": cue.get("quote", ""),
            "note": cue.get("note", ""),
            "viewer_link": _viewer_link(call, cue.get("timestamp", "")),
        })

    speakers = (parsed.get("speakers") or "").replace("\n", " ").strip()
    brief = (parsed.get("call_summary") or "").replace("\n", " ").strip()

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


# ────────────────────────── main entry ──────────────────────────

def generate_case_report_pdf(
    job: Job,
    done_calls: List[CallResult],
    engine: SummarizationEngine,
    gen_date: Optional[str] = None,
) -> bytes:
    """Build the case report PDF for a completed job."""
    from weasyprint import HTML

    if not gen_date:
        gen_date = datetime.now().strftime("%B %d, %Y")

    case_name = (job.case_name or "Untitled Case").strip() or "Untitled Case"
    defendant_name = (job.defendant_name or "").strip()

    # Parse every summary exactly once and reuse the result everywhere downstream.
    parsed_by_index: Dict[int, dict] = {}
    for call in done_calls:
        if call.summary:
            parsed_by_index[call.index] = U.parse_summary_sections(call.summary)
        else:
            parsed_by_index[call.index] = {}

    rel_buckets = _split_by_relevance(done_calls, parsed_by_index)
    glance = _build_at_a_glance(done_calls, parsed_by_index, rel_buckets)
    timeline = _build_timeline(done_calls)

    # Combined Gemini synthesis: top findings AND identity inference in one call.
    synthesis_inputs = _select_synthesis_calls(rel_buckets)
    identity_inputs = _collect_identity_inputs(done_calls, parsed_by_index)

    findings: List[Dict[str, Any]] = []
    identity_map: Dict[str, Dict[str, str]] = {}
    synthesis_state = "no_input"

    if synthesis_inputs or identity_inputs:
        case_context = _build_case_context(case_name, defendant_name, job.summary_prompt)
        calls_block = _format_calls_for_synthesis(synthesis_inputs)
        numbers_block = _format_numbers_for_synthesis(identity_inputs)
        prompt_text = CASE_REPORT_SYNTHESIS_PROMPT.format(
            case_context=case_context,
            calls_block=calls_block,
            numbers_block=numbers_block,
            min_findings=MIN_TOP_FINDINGS,
            max_findings=MAX_TOP_FINDINGS,
        )

        synth_text = _call_synth(engine, prompt_text)
        if synth_text:
            calls_by_id = {entry["call"].index: entry for entry in synthesis_inputs}
            for f in _parse_findings(synth_text):
                try:
                    call_id = int((f.get("CALL_ID") or "").strip())
                except ValueError:
                    continue
                entry = calls_by_id.get(call_id)
                if not entry:
                    continue
                call = entry["call"]
                ts_raw = (f.get("TIMESTAMP") or "").strip()
                ts_clean = "" if ts_raw.upper() in ("NONE", "N/A", "") else ts_raw
                findings.append({
                    "headline": f.get("HEADLINE", "").strip(),
                    "detail": f.get("DETAIL", "").strip(),
                    "timestamp": ts_clean,
                    "call_filename": U.shorten(call.filename, 56),
                    "call_date": _format_call_datetime_short(call),
                    "viewer_link": _viewer_link(call, ts_clean or None),
                    "pdf_link": _transcript_pdf_link(call),
                })
            identity_map = _parse_identities(synth_text)
            if findings or identity_map:
                synthesis_state = "ok"
            else:
                synthesis_state = "parse_failed"
        else:
            synthesis_state = "synth_unavailable"

    callers = _build_caller_stats(done_calls, parsed_by_index, identity_map)
    top_callers = callers[:10]

    high_cards = [_build_call_card(entry) for entry in rel_buckets["HIGH"]]

    medium_rows = []
    for entry in rel_buckets["MEDIUM"]:
        call = entry["call"]
        parsed = entry["parsed"]
        brief = (parsed.get("call_summary") or "").strip() or "—"
        cue_count = len(parsed.get("review_cue_items", []) or [])
        medium_rows.append({
            "call_index": call.index + 1,
            "filename": U.shorten(call.filename, 48),
            "datetime": _format_call_datetime_short(call),
            "duration": _format_duration(call.duration_seconds),
            "party": call.outside_number_fmt or call.outside_number or "—",
            "brief": brief,
            "cue_count": cue_count,
            "viewer_link": _viewer_link(call),
            "pdf_link": _transcript_pdf_link(call),
        })

    ctx = {
        "case_name": case_name,
        "case_name_short": U.shorten(case_name, 38),
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

    template = U.get_jinja_env().get_template("case_report_template.html")
    html_str = template.render(**ctx)
    # base_url is intentionally omitted so that relative <a href> values
    # (e.g. "viewer.html?call=...", "transcripts/xxx.pdf") are written into
    # the PDF link annotations as-is. The delivery zip places case-report.pdf
    # at its root, next to viewer.html and the transcripts/ directory, so PDF
    # readers resolve those relative URIs against wherever the end user
    # extracts the zip.
    return HTML(string=html_str).write_pdf()
