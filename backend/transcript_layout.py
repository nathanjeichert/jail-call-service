"""Transcript line layout: the single source of truth for page:line geometry.

Every surface that cites a transcript location (the transcript PDF sheets,
index.html's printed pages and cites, the case report's call cards,
and the ``[Page:Line]`` references the summarization prompt asks the model to
cite) derives from :func:`compute_line_entries`. Changing the wrapping rules
here shifts every citation in the product, so the constants below are pinned.

Line entries are plain dicts::

    {
      "id": "<turn_index>-<line_index>",
      "turn_index": int, "line_index": int,
      "speaker": "INMATE", "text": "wrapped line text",
      "rendered_text": "          INMATE:   wrapped line text",
      "start": sec, "end": sec,          # from word timestamps when available
      "page": int, "line": int, "pgln": page*100+line,
      "is_continuation": bool,
      "words": [{"t": word, "s": sec, "e": sec}, ...]   # optional
    }
"""

import re
from typing import List, Optional

from .formatting import format_timestamp, timestamp_to_seconds
from .models import TranscriptTurn, WordTimestamp

#: Characters per transcript line. Derived from the PDF's ruled corridor
#: (see ``delivery/transcript_pdf.py``, which asserts its geometry still
#: yields exactly this value). Every page:line citation depends on it.
MAX_LINE_CHARS = 62
LINES_PER_PAGE = 25

SPEAKER_PREFIX_SPACES = 10
CONTINUATION_SPACES = 5
SPEAKER_COLON = ":   "


# ────────────────────────── Wrapping ──────────────────────────

def wrap_text(text: str, max_width: int) -> List[str]:
    """Greedy word wrap on character count (monospace transcript sheets)."""
    if not text:
        return [""]
    if max_width <= 0:
        return [text]
    words = text.split()
    lines, current, length = [], [], 0
    for word in words:
        space = len(word) + (1 if current else 0)
        if length + space <= max_width:
            current.append(word)
            length += space
        else:
            if current:
                lines.append(" ".join(current))
            current, length = [word], len(word)
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def _distribute_words_to_lines(
    words: Optional[List[WordTimestamp]],
    all_lines: List[str],
) -> List[List[dict]]:
    """Map word timestamps to wrapped lines by matching word text sequentially."""
    result: List[List[dict]] = [[] for _ in all_lines]
    if not words or not all_lines:
        return result

    word_idx = 0
    for line_idx, line_text in enumerate(all_lines):
        line_remaining = line_text.strip()
        while word_idx < len(words) and line_remaining:
            w = words[word_idx]
            wtext = w.text.strip()
            if not wtext:
                word_idx += 1
                continue
            if line_remaining.lower().startswith(wtext.lower()):
                result[line_idx].append({
                    "t": wtext,
                    "s": round(w.start / 1000.0, 3),
                    "e": round(w.end / 1000.0, 3),
                })
                line_remaining = line_remaining[len(wtext):].lstrip()
                word_idx += 1
            else:
                # Skip punctuation the ASR word list doesn't carry.
                stripped = line_remaining.lstrip(" ,;:!?.'\"—-–()")
                if stripped != line_remaining:
                    line_remaining = stripped
                else:
                    break
    return result


def compute_line_entries(
    turns: List[TranscriptTurn],
    audio_duration: float,
    lines_per_page: int = LINES_PER_PAGE,
) -> List[dict]:
    """Build page/line layout data from transcript turns."""
    line_entries: List[dict] = []
    page = 1
    line_in_page = 1

    for turn_idx, turn in enumerate(turns):
        start_sec = timestamp_to_seconds(turn.timestamp)
        if turn.words:
            word_starts = [w.start for w in turn.words if w.start is not None and w.start >= 0]
            word_ends = [w.end for w in turn.words if w.end is not None and w.end >= 0]
            if word_starts and word_ends:
                start_sec = min(word_starts) / 1000.0

        is_continuation = getattr(turn, 'is_continuation', False)
        speaker_name = turn.speaker.upper()
        text = turn.text.strip()

        speaker_prefix = " " * SPEAKER_PREFIX_SPACES + speaker_name + SPEAKER_COLON
        max_cont_width = MAX_LINE_CHARS - CONTINUATION_SPACES

        if is_continuation:
            max_first_line = max_cont_width
        else:
            max_first_line = MAX_LINE_CHARS - len(speaker_prefix)

        wrapped = wrap_text(text, max_first_line)
        if not wrapped:
            wrapped = [""]

        cont_text = " ".join(wrapped[1:])
        cont_lines = wrap_text(cont_text, max_cont_width) if cont_text else []
        all_lines = [wrapped[0]] + cont_lines

        words_per_line = _distribute_words_to_lines(turn.words, all_lines)

        for line_idx, line_text in enumerate(all_lines):
            is_cont_line = is_continuation or line_idx > 0
            if line_idx == 0 and not is_continuation:
                rendered = speaker_prefix + line_text
            else:
                rendered = " " * CONTINUATION_SPACES + line_text

            line_words = words_per_line[line_idx]
            if line_words:
                line_start = line_words[0]["s"]
                line_end = line_words[-1]["e"]
            else:
                line_start = start_sec
                line_end = start_sec

            entry = {
                "id": f"{turn_idx}-{line_idx}",
                "turn_index": turn_idx,
                "line_index": line_idx,
                "speaker": speaker_name,
                "text": line_text,
                "rendered_text": rendered,
                "start": line_start,
                "end": line_end,
                "page": page,
                "line": line_in_page,
                "pgln": page * 100 + line_in_page,
                "is_continuation": is_cont_line,
            }
            if line_words:
                entry["words"] = line_words
            line_entries.append(entry)

            line_in_page += 1
            if line_in_page > lines_per_page:
                page += 1
                line_in_page = 1

    return line_entries


# ────────────────────────── Line cites & quotes ──────────────────────────

_LINE_CITE_RANGE_RE = re.compile(r"^\s*(\d+):(\d+)(?:\s*[-–—]\s*(\d+):(\d+))?\s*$")
_INLINE_QUOTED_TEXT_RE = re.compile(r'"([^"\n]{1,200})"')
_SENTENCE_END_RE = re.compile(r"[.!?](?:['\")\]]+)?\s*$")


def line_cite_for_timestamp(timestamp: str, line_entries: Optional[List[dict]]) -> str:
    """Return a transcript page:line cite for the line nearest a cue timestamp."""
    if not timestamp or not line_entries:
        return ""

    target = timestamp_to_seconds(timestamp)
    best: Optional[dict] = None
    best_distance = float("inf")

    for entry in line_entries:
        try:
            start = float(entry.get("start", 0) or 0)
            end = float(entry.get("end", start) or start)
        except (TypeError, ValueError):
            continue

        if start <= target <= max(end, start):
            best = entry
            break

        distance = min(abs(target - start), abs(target - end))
        if distance < best_distance:
            best = entry
            best_distance = distance

    if not best:
        return ""

    page = best.get("page")
    line = best.get("line")
    if not page or not line:
        return ""
    return f"{int(page)}:{int(line)}"


def normalize_line_cite(value: str) -> str:
    """Canonical ``P:L`` / ``P:L-P:L`` form: ASCII hyphen, no whitespace."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", "", text)


def strip_inline_quotes(value: str) -> str:
    """Unquote ``"..."`` spans a model embedded in note prose."""
    text = str(value or "").strip()
    if '"' not in text:
        return text
    return _INLINE_QUOTED_TEXT_RE.sub(lambda m: m.group(1), text)


def parse_line_cite(value: str) -> Optional[tuple]:
    """Parse a cite into ``(start_page, start_line, end_page, end_line)``."""
    match = _LINE_CITE_RANGE_RE.match(normalize_line_cite(value))
    if not match:
        return None

    start_page = int(match.group(1))
    start_line = int(match.group(2))
    end_page = int(match.group(3) or start_page)
    end_line = int(match.group(4) or start_line)

    if (end_page, end_line) < (start_page, start_line):
        return None
    return start_page, start_line, end_page, end_line


def _entries_for_line_cite(line_cite: str, line_entries: Optional[List[dict]]) -> List[dict]:
    parsed = parse_line_cite(line_cite)
    if not parsed or not line_entries:
        return []

    start_page, start_line, end_page, end_line = parsed
    selected: List[dict] = []
    for entry in sorted(
        line_entries,
        key=lambda e: (int(e.get("page", 0) or 0), int(e.get("line", 0) or 0), str(e.get("id", ""))),
    ):
        page = int(entry.get("page", 0) or 0)
        line = int(entry.get("line", 0) or 0)
        key = (page, line)
        if (start_page, start_line) <= key <= (end_page, end_line):
            selected.append(entry)

    if not selected:
        return []
    if (int(selected[0].get("page", 0) or 0), int(selected[0].get("line", 0) or 0)) != (start_page, start_line):
        return []
    if (int(selected[-1].get("page", 0) or 0), int(selected[-1].get("line", 0) or 0)) != (end_page, end_line):
        return []
    return selected


def _line_entry_lookup(line_entries: Optional[List[dict]]) -> dict:
    lookup = {}
    for entry in line_entries or []:
        try:
            key = (int(entry.get("turn_index", -1)), int(entry.get("line_index", -1)))
        except (TypeError, ValueError):
            continue
        lookup[key] = entry
    return lookup


def _same_turn_neighbor(entry: dict, lookup: dict, *, direction: int) -> Optional[dict]:
    try:
        key = (int(entry.get("turn_index", -1)), int(entry.get("line_index", -1)) + direction)
    except (TypeError, ValueError):
        return None
    return lookup.get(key)


def _has_sentence_boundary_before(entry: dict, lookup: dict) -> bool:
    prev_entry = _same_turn_neighbor(entry, lookup, direction=-1)
    if not prev_entry:
        return True
    prev_text = str(prev_entry.get("text", "") or "").strip()
    if not prev_text:
        return True
    return bool(_SENTENCE_END_RE.search(prev_text))


def _has_sentence_boundary_after(entry: dict, lookup: dict) -> bool:
    next_entry = _same_turn_neighbor(entry, lookup, direction=1)
    if not next_entry:
        return True
    text = str(entry.get("text", "") or "").strip()
    if not text:
        return True
    return bool(_SENTENCE_END_RE.search(text))


def _apply_quote_ellipses(text: str, *, prefix: bool, suffix: bool) -> str:
    quote = str(text or "").strip()
    if not quote:
        return ""
    if prefix and not quote.startswith("..."):
        quote = f"...{quote}"
    if suffix and not quote.endswith("..."):
        quote = f"{quote}..."
    return quote


def quote_for_line_cite(
    line_cite: str,
    line_entries: Optional[List[dict]],
    *,
    max_lines: int = 3,
    max_chars: int = 220,
) -> str:
    """Bounded pull-quote text for a cited line range, with edge ellipses."""
    selected = _entries_for_line_cite(line_cite, line_entries)
    if not selected:
        return ""

    excerpt: List[dict] = []
    for entry in selected[:max_lines]:
        text = str(entry.get("text", "")).strip()
        if not text:
            continue
        candidate = excerpt + [entry]
        quote = " ".join(str(item.get("text", "")).strip() for item in candidate if str(item.get("text", "")).strip())
        quote = re.sub(r"\s+", " ", quote).strip()
        if quote and len(quote) <= max_chars:
            excerpt = candidate
            continue
        break

    if not excerpt:
        return ""

    quote = " ".join(str(entry.get("text", "")).strip() for entry in excerpt if str(entry.get("text", "")).strip())
    quote = re.sub(r"\s+", " ", quote).strip()
    if not quote or len(quote) > max_chars:
        return ""

    lookup = _line_entry_lookup(line_entries)
    prefix_ellipsis = not _has_sentence_boundary_before(excerpt[0], lookup)
    suffix_ellipsis = excerpt[-1].get("id") != selected[-1].get("id") or not _has_sentence_boundary_after(excerpt[-1], lookup)
    return _apply_quote_ellipses(quote, prefix=prefix_ellipsis, suffix=suffix_ellipsis)


def resolve_line_ref_context(line_cite: str, line_entries: Optional[List[dict]]) -> Optional[dict]:
    """Timestamp, speaker, canonical cite, quote, and start second for a cite.

    Returns None when the cite does not resolve to real transcript lines.
    """
    selected = _entries_for_line_cite(line_cite, line_entries)
    if not selected:
        return None

    first = selected[0]
    start_seconds = float(first.get("start", 0) or 0)
    return {
        "timestamp": format_timestamp(start_seconds),
        "speaker": str(first.get("speaker", "") or "").strip(),
        "line_cite": normalize_line_cite(line_cite),
        "quote": quote_for_line_cite(line_cite, line_entries),
        "start": start_seconds,
    }


def hydrate_review_cues(cues: Optional[List[dict]], line_entries: Optional[List[dict]]) -> List[dict]:
    """Enrich parsed review cues with deterministic line cites and quotes."""
    hydrated: List[dict] = []
    for cue in cues or []:
        item = dict(cue)
        item["note"] = strip_inline_quotes(item.get("note", ""))
        line_ref = normalize_line_cite(item.get("line_ref", ""))
        if line_ref and parse_line_cite(line_ref):
            item["line_ref"] = line_ref
            item["line_cite"] = line_ref
            quote = quote_for_line_cite(line_ref, line_entries)
            if quote:
                item["quote"] = quote
        else:
            item["line_ref"] = ""
            item["line_cite"] = line_cite_for_timestamp(item.get("timestamp", ""), line_entries)
        hydrated.append(item)
    return hydrated
