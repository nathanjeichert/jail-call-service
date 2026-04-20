"""
Shared PDF utilities — fonts, formatting helpers, template caching.

Centralises functions previously duplicated across transcript_formatting,
case_report, guide_pdf, and pipeline modules.
"""

import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import jinja2
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logger = logging.getLogger(__name__)

# ────────────────────────── Relevance descriptors ──────────────────────────

RELEVANCE_DESC: Dict[str, str] = {
    "HIGH": "Potentially jury-relevant or case-substantive content",
    "MEDIUM": "Substantive legal or case context, not clearly central",
    "LOW": "Little to no apparent case relevance",
}


# ────────────────────────── Font registration ──────────────────────────

# Platform-aware Courier New (or compatible) font registration.  Probes
# common locations on macOS and Linux; falls back to ReportLab's built-in
# Courier if no TrueType file is found.

_COURIER_SEARCH_PATHS: List[List[str]] = [
    # macOS
    [
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    ],
    # Linux — msttcorefonts (installed via ttf-mscorefonts-installer)
    [
        "/usr/share/fonts/truetype/msttcorefonts/Courier_New.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Courier_New_Bold.ttf",
    ],
    # Linux — alternative msttcorefonts naming
    [
        "/usr/share/fonts/truetype/msttcorefonts/cour.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/courbd.ttf",
    ],
    # Linux — Liberation Mono (metric-compatible Courier New substitute)
    [
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    ],
    # Linux — alternative liberation path (e.g. Fedora)
    [
        "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
        "/usr/share/fonts/liberation-mono/LiberationMono-Bold.ttf",
    ],
]

_fonts_registered = False


def register_fonts() -> None:
    """Register CourierNew / CourierNew-Bold for ReportLab.

    Tries platform-specific TrueType paths first, then falls back to
    ReportLab's built-in Courier (always available but not TrueType).
    Safe to call multiple times — registration is idempotent.
    """
    global _fonts_registered
    if _fonts_registered:
        return

    for pair in _COURIER_SEARCH_PATHS:
        regular, bold = pair
        if os.path.isfile(regular) and os.path.isfile(bold):
            pdfmetrics.registerFont(TTFont("CourierNew", regular))
            pdfmetrics.registerFont(TTFont("CourierNew-Bold", bold))
            _fonts_registered = True
            logger.debug("Registered TrueType Courier from %s", regular)
            return

    # Fall back to built-in Courier — always available in ReportLab.
    # We register a font-family alias so that canvas code using the
    # "CourierNew" name transparently maps to the built-in Courier.
    logger.warning(
        "No TrueType Courier New / Liberation Mono found; "
        "falling back to ReportLab built-in Courier. "
        "Install fonts-liberation or ttf-mscorefonts-installer for better results."
    )
    from reportlab.lib.fonts import addMapping
    addMapping("CourierNew", 0, 0, "Courier")        # normal
    addMapping("CourierNew", 1, 0, "Courier-Bold")    # bold
    _fonts_registered = True


# ────────────────────────── Jinja2 template environment ──────────────────────────

_TEMPLATE_DIR = Path(__file__).parent

_jinja_env: Optional[jinja2.Environment] = None


def get_jinja_env() -> jinja2.Environment:
    """Return a cached Jinja2 Environment with FileSystemLoader.

    Templates are compiled once and cached automatically.  Using an
    Environment (rather than raw ``Template(text)``) enables ``{% include %}``,
    template inheritance, and avoids re-reading the file on every call.
    """
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=False,
            auto_reload=False,
        )
    return _jinja_env


# ────────────────────────── Text helpers ──────────────────────────

def safe_text(value: Optional[str]) -> str:
    """Strip and stringify, treating None as empty."""
    return str(value or "").strip()


def shorten(text: str, max_chars: int) -> str:
    """Tail-truncate *text* with an ellipsis if it exceeds *max_chars*."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "\u2026"


def shorten_middle(value: Optional[str], max_chars: int = 44) -> str:
    """Middle-truncate *value* with ``...`` for display in constrained areas."""
    text = safe_text(value)
    if len(text) <= max_chars:
        return text
    if max_chars <= 8:
        return text[:max_chars]
    keep = max_chars - 3
    head = (keep + 1) // 2
    tail = keep // 2
    return f"{text[:head]}...{text[-tail:]}"


# ────────────────────────── Duration formatting ──────────────────────────

def format_duration(seconds: Optional[float], empty: str = "") -> str:
    """Format seconds as ``H:MM:SS`` or ``M:SS``.  Returns *empty* for falsy input."""
    if not seconds:
        return empty
    secs = int(seconds)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_duration_long(seconds: float) -> str:
    """Human-readable long form, e.g. ``2 h 15 min``."""
    secs = int(seconds)
    if secs < 60:
        return f"{secs} sec"
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


# ────────────────────────── Datetime formatting ──────────────────────────

_DATETIME_FORMATS = [
    ("%Y-%m-%d %H:%M", 16),
    ("%Y-%m-%dT%H:%M:%S", 19),
    ("%Y-%m-%d %H:%M:%S", 19),
    ("%Y-%m-%d", 10),
]


def parse_call_datetime(raw: Optional[str]) -> Optional[datetime]:
    """Parse common call-datetime string formats.  Returns None on failure."""
    text = safe_text(raw)
    if not text:
        return None
    for fmt, size in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def format_display_datetime(value: Optional[str]) -> str:
    """Make XML-style call datetimes readable without inventing timezone data."""
    text = safe_text(value)
    if not text:
        return ""
    dt = parse_call_datetime(text)
    if dt is None:
        return text
    # Date-only format (no time component)
    if len(text.strip()) <= 10:
        return f"{dt.strftime('%b.')} {dt.day}, {dt.year}"
    time_text = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%b.')} {dt.day}, {dt.year} at {time_text}"


def format_call_datetime_short(raw: Optional[str], fallback_date: Optional[str] = None) -> str:
    """Short display form for case report call cards, e.g. ``Mar 15, 2024 · 2:30 PM``."""
    if not raw:
        return fallback_date or "—"
    text = raw.strip()
    dt = parse_call_datetime(text)
    if dt is None:
        return text
    if len(text) <= 10:
        return f"{dt.strftime('%b')} {dt.day}, {dt.year}"
    time_text = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%b')} {dt.day}, {dt.year} · {time_text}"


def format_date_short(d: date) -> str:
    """Portable short date: ``Mar 15, 2024`` (avoids non-portable ``%-d``)."""
    return f"{d.strftime('%b')} {d.day}, {d.year}"


# ────────────────────────── Summary parsing ──────────────────────────

TIMESTAMP_RE = re.compile(r"(\[(?:\d{1,2}:)?\d{2}:\d{2}\])")


def timestamp_to_seconds(timestamp: Optional[str]) -> float:
    """Convert ``[MM:SS]`` or ``[H:MM:SS]`` to seconds."""
    if not timestamp:
        return 0.0
    ts = timestamp.strip("[]").strip()
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            h, m, s = map(float, parts)
            return h * 3600 + m * 60 + s
        if len(parts) == 2:
            m, s = map(float, parts)
            return m * 60 + s
        return float(ts)
    except ValueError:
        return 0.0


def _parse_review_cue_line(line: str) -> Optional[dict]:
    """Parse a single cue bullet into timestamp/speaker/quote/note fields."""
    clean = line.strip()
    if not clean:
        return None
    clean = re.sub(r"^[-\u2022*]\s*", "", clean)
    clean = re.sub(r"^\d+[.)]\s*", "", clean)
    if re.fullmatch(
        r"(?i)(none|n/?a|no\s+notes?|no\s+relevant\s+(?:information|moments?)(?:\s+found)?\.?)",
        clean,
    ):
        return None

    ts_match = TIMESTAMP_RE.search(clean)
    if not ts_match:
        return None

    timestamp = ts_match.group(1)
    rest = clean[ts_match.end() :].strip(" :-\u2013\u2014")

    speaker = ""
    speaker_match = re.match(
        r"\[?([A-Z][A-Z0-9 /&.'-]{1,34})\]?(?=\s*:|\s+\[|\s+[-\u2013\u2014]|$)",
        rest,
    )
    if speaker_match:
        speaker = speaker_match.group(1).strip()
        rest = rest[speaker_match.end() :].strip()
        rest = re.sub(r"^:\s*", "", rest)

    line_ref = ""
    line_ref_match = re.match(r"\[?(\d+:\d+(?:\s*[-\u2013\u2014]\s*\d+:\d+)?)\]?\s*", rest)
    if line_ref_match:
        line_ref = re.sub(r"\s+", "", line_ref_match.group(1))
        line_ref = line_ref.replace("\u2013", "-").replace("\u2014", "-")
        rest = rest[line_ref_match.end() :].strip()

    quote = ""
    if not line_ref:
        quote_match = re.search(r'["\u201c]([^"\u201d]{1,220})["\u201d]', rest)
        if quote_match:
            quote = quote_match.group(1).strip()
            before = rest[: quote_match.start()].strip()
            after = rest[quote_match.end() :].strip()
            rest = " ".join(part for part in (before, after) if part)

    rest = re.sub(r"^\s*[-\u2013\u2014:]+\s*", "", rest).strip()
    if quote and rest:
        parts = re.split(r"\s+[-\u2013\u2014]\s+", rest, maxsplit=1)
        note = parts[-1].strip()
    else:
        note = rest

    return {
        "timestamp": timestamp,
        "speaker": speaker,
        "line_ref": line_ref,
        "quote": quote,
        "note": note,
    }


def _parse_review_cues(body: str) -> List[dict]:
    cues: List[dict] = []
    for line in body.splitlines():
        cue = _parse_review_cue_line(line)
        if cue:
            cues.append(cue)
    cues.sort(key=lambda c: timestamp_to_seconds(c.get("timestamp", "")))
    return cues


def parse_summary_sections(summary: str) -> dict:
    """Parse structured Gemini/Gemma output into renderable sections.

    Accepts both current NOTES-based format and older REVIEW CUES / KEY
    FINDINGS output so previously generated summaries still render.
    """
    sections: Dict[str, object] = {"raw": summary}
    text = summary.strip()

    rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", text, re.IGNORECASE)
    if rel_match:
        sections["relevance"] = rel_match.group(1).upper()

    header_patterns = [
        ("review_cues", r"(?m)^\s*NOTES:?"),
        ("review_cues", r"(?m)^\s*REVIEW\s+CUES:?"),
        ("review_cues", r"(?m)^\s*NOTABLE\s+MOMENTS:?"),
        ("review_cues", r"(?m)^\s*KEY\s+MOMENTS:?"),
        ("review_cues", r"(?m)^\s*PULL\s+QUOTES:?"),
        ("key_findings", r"(?m)^\s*KEY\s+FINDINGS:?"),
        ("speakers", r"(?m)^\s*IDENTITY\s+OF\s+OUTSIDE\s+PARTY:?"),
        ("speakers", r"(?m)^\s*SPEAKERS?\s*(?:&|AND)\s*RELATIONSHIP:?"),
        ("speakers", r"(?m)^\s*SPEAKER\s+NOTES:?"),
        ("call_summary", r"(?m)^\s*BRIEF\s+SUMMARY:?"),
        ("call_summary", r"(?m)^\s*CALL\s+SUMMARY:?"),
        ("call_summary", r"(?m)^\s*SUMMARY:?"),
    ]

    positions: List[tuple] = []
    for key, pattern in header_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if any(
                not (match.end() <= start or match.start() >= end)
                for start, end, _ in positions
            ):
                continue
            positions.append((match.start(), match.end(), key))
            break
    positions.sort()

    seen_keys: set = set()
    for i, (start, end, key) in enumerate(positions):
        if key in seen_keys and key not in {"review_cues"}:
            continue
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[end:next_start].strip()
        if key == "review_cues" and sections.get("review_cues"):
            sections["review_cues"] += "\n" + body
        else:
            sections[key] = body
        seen_keys.add(key)

    cue_body = sections.get("review_cues") or sections.get("key_findings") or ""
    if cue_body:
        sections["review_cue_items"] = _parse_review_cues(cue_body)

    sections["structured"] = bool(
        sections.get("relevance")
        and (
            sections.get("review_cue_items")
            or sections.get("call_summary")
            or sections.get("speakers")
        )
    )

    return sections
