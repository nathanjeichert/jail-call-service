"""Call dates inferred from recording filenames, for batches without an ICM report.

Every date-driven surface (the delivery page's calendar and timeline, the
case report's timeline, the index's date filter) reads ``call_date`` /
``call_time`` / ``call_datetime_str`` off the call. Those come from the
provider's ``ICM_report.xml`` when it is present and matches the file;
when it is missing, this module recovers the same three fields from the
filename, and only those three (no number, name, or outcome is guessed).

Recognized names:

* GTL/ViaPath exports: ``<unix epoch>_<pin>_<...>.wav`` (e.g.
  ``1647369886_5000_12_161_857.wav``). The epoch is the call's start; it is
  rendered in the operator machine's local time zone, which matches the
  facility's clock in practice.
* Stamped names: ``YYYYMMDD_HHMMSS_<anything>`` or ``YYYYMMDD`` alone, with
  ``_``, ``-`` or ``T`` between date and time.

Anything else yields no date, and the call stays undated (the page lists it
without a date and the charts leave it out with a note).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Optional, Tuple

_EPOCH_RE = re.compile(r"^(\d{10})(?:\D|$)")
_STAMP_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?:[_\-T](\d{2})(\d{2})(?:\d{2})?)?(?!\d)")

# Epoch values outside this window are some other number, not a call time.
_EPOCH_MIN = datetime(2000, 1, 1).timestamp()
_EPOCH_MAX = datetime(2100, 1, 1).timestamp()


def infer_call_datetime(filename: str) -> Optional[Tuple[str, str]]:
    """``('YYYY-MM-DD', 'HH:MM')`` read from the filename, or None.

    The time is ``''`` when the name carries a date but no time.
    """
    name = str(filename or "").rsplit("/", 1)[-1]

    m = _EPOCH_RE.match(name)
    if m:
        epoch = int(m.group(1))
        if _EPOCH_MIN <= epoch < _EPOCH_MAX:
            dt = datetime.fromtimestamp(epoch)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")

    m = _STAMP_RE.search(name)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            datetime(year, month, day)
        except ValueError:
            return None
        if not 2000 <= year < 2100:
            return None
        time = ""
        if m.group(4) is not None:
            hour, minute = int(m.group(4)), int(m.group(5))
            if hour < 24 and minute < 60:
                time = f"{hour:02d}:{minute:02d}"
        return f"{year:04d}-{month:02d}-{day:02d}", time

    return None


def filename_date_fields(filename: str) -> Dict[str, str]:
    """The ``CallResult`` date fields a filename supports; ``{}`` when none."""
    inferred = infer_call_datetime(filename)
    if not inferred:
        return {}
    call_date, call_time = inferred
    return {
        "call_date": call_date,
        "call_time": call_time,
        "call_datetime_str": f"{call_date} {call_time}".strip(),
    }
