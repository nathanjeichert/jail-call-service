"""Small text, duration, timestamp, and date formatting helpers shared app-wide."""

import re
from datetime import date, datetime
from typing import Optional

# ────────────────────────── Text helpers ──────────────────────────

def safe_text(value: Optional[str]) -> str:
    """Strip and stringify, treating None as empty."""
    return str(value or "").strip()


def collapse_whitespace(value: Optional[str]) -> str:
    """Collapse all runs of whitespace (including newlines) to single spaces."""
    return " ".join(str(value or "").split()).strip()


def shorten(text: str, max_chars: int) -> str:
    """Tail-truncate *text* with an ellipsis if it exceeds *max_chars*."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


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


# ────────────────────────── Durations & timestamps ──────────────────────────

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


TIMESTAMP_RE = re.compile(r"(\[(?:\d{1,2}:)?\d{2}:\d{2}\])")


def format_timestamp(seconds: Optional[float], brackets: bool = True) -> str:
    """Render seconds as the app's ``[MM:SS]`` transcript timestamp label.

    Truncates to whole seconds and clamps negatives to zero; minutes are not
    wrapped at 60 (``[75:03]`` is valid and what every surface expects).
    """
    total = max(int(seconds or 0), 0)
    mins, secs = divmod(total, 60)
    label = f"{mins:02d}:{secs:02d}"
    return f"[{label}]" if brackets else label


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


# ────────────────────────── Dates ──────────────────────────

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
    if len(text) <= 10:
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


def format_generated_date(d: Optional[date] = None) -> str:
    """The "Generated" / "Prepared" date stamped on delivery artifacts: ``March 5, 2026``.

    Defaults to today. Every artifact uses this one format; tests pass a fixed
    date so the golden package is reproducible.
    """
    d = d or date.today()
    return f"{d.strftime('%B')} {d.day}, {d.year}"
