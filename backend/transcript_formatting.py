"""
PDF generation for jail call transcripts.

Produces a single-render PDF document:
  Sheet 1: Title page (case info, file metadata)
  Sheet 2+: AI summary sheet(s) (if a summary is provided)
  Remaining sheets: Legal-formatted transcript (25 lines/page, Courier,
                    line numbers)

Every page is an explicit fixed-size 8.5in x 11in sheet in one Jinja
template (``transcript_pdf_template.html``) rendered by headless Chromium
via :func:`backend.pdf_render.render_pdf`. Pagination is decided entirely
in Python — Chromium never makes a page-break decision — and the rendered
page count is asserted against the emitted sheet count after every render.
"""

import html
import io
import re
from collections import defaultdict
from typing import Dict, List, Optional

from . import font_metrics as FM
from . import pdf_utils as U
from .design_fonts import pdf_font_css
from .models import TranscriptTurn, WordTimestamp

# Text layout constants
SPEAKER_PREFIX_SPACES = 10
CONTINUATION_SPACES = 5
SPEAKER_COLON = ":   "

inch = 72.0

# Transcript page geometry (legal deposition style)
PDF_PAGE_WIDTH, PDF_PAGE_HEIGHT = 612.0, 792.0  # US letter, points
PDF_MARGIN_TOP = 0.75 * inch
PDF_MARGIN_BOTTOM = 0.75 * inch
PDF_LINE_HEIGHT = 25.0
PDF_TEXT_SIZE = 12
PDF_LINE_NUMBER_SIZE = 10
PDF_PAGE_NUMBER_SIZE = 10


# Vertical rule positions
PDF_LINE_NUM_RIGHT = 0.78 * inch      # right edge of line numbers
PDF_RULE_LEFT_OUTER = 0.92 * inch     # left double-line (outer)
PDF_RULE_LEFT_INNER = 0.97 * inch     # left double-line (inner)
PDF_RULE_RIGHT = 7.4 * inch           # right single line

# Derive transcript text block from the ruled corridor, centered between the
# inner left rule and the right rule.
#
# Courier (and metric-compatible fonts such as the vendored Courier Prime)
# advances exactly 0.6 em per character, i.e. 7.2 pt at 12 pt. This constant
# previously came from ReportLab's stringWidth("M", "CourierNew", 12) and
# evaluated to 7.201171875 pt with the macOS Courier New TTF; the pure 0.6 em
# value yields the same MAX_LINE_CHARS, which is asserted below because every
# page:line citation in the product depends on it.
_COURIER_CHAR_W = 7.2
_TRANSCRIPT_RULE_WIDTH = PDF_RULE_RIGHT - PDF_RULE_LEFT_INNER
MAX_LINE_CHARS = int((_TRANSCRIPT_RULE_WIDTH - 12) / _COURIER_CHAR_W)
assert MAX_LINE_CHARS == 62, (
    f"MAX_LINE_CHARS changed ({MAX_LINE_CHARS} != 62); this would shift every "
    "page:line citation in the product. Fix the arithmetic, not the value."
)
PDF_TEXT_BLOCK_WIDTH = MAX_LINE_CHARS * _COURIER_CHAR_W
PDF_TEXT_X = PDF_RULE_LEFT_INNER + ((_TRANSCRIPT_RULE_WIDTH - PDF_TEXT_BLOCK_WIDTH) / 2.0)

# Chromium line-box baseline offsets for the vendored Courier Prime
# (unitsPerEm 2048, hhea ascent 1600, descent -700). For a line box of
# height L and font size S the baseline sits at
#     (L - S*(asc+desc)) / 2 + S*asc
# from the top of the box. These are used to convert the ReportLab-era
# baseline coordinates into CSS `top` offsets for the row boxes.
_COURIER_ASCENT_EM = 1600.0 / 2048.0
_COURIER_DESCENT_EM = 700.0 / 2048.0


def _courier_baseline_in_box(box_height: float, font_size: float) -> float:
    content = font_size * (_COURIER_ASCENT_EM + _COURIER_DESCENT_EM)
    return ((box_height - content) / 2.0) + (font_size * _COURIER_ASCENT_EM)


# Cover / summary sheet geometry ("Record" design language). The mockup
# sheets are 816px wide (96px/in); multiply mockup px by 0.75 for pt.
SUMMARY_LEFT = 46.5
SUMMARY_RIGHT = 40.5
SUMMARY_WIDTH = PDF_PAGE_WIDTH - SUMMARY_LEFT - SUMMARY_RIGHT
SUMMARY_CONTENT_TOP = 116.0
SUMMARY_CONTENT_BOTTOM = 56.0
SUMMARY_CONTENT_HEIGHT = PDF_PAGE_HEIGHT - SUMMARY_CONTENT_TOP - SUMMARY_CONTENT_BOTTOM
SUMMARY_ASSESSMENT_HEIGHT = 36.0
SUMMARY_SECTION_GAP = 18.0
SUMMARY_CARD_GAP = 14.0
SUMMARY_CONTEXT_TWO_COL_GAP = 16.5
SUMMARY_CARD_TITLE_BLOCK = 20.0   # context-card h4 + rule + gap below
SUMMARY_NOTES_HEADING_HEIGHT = 22.0
SUMMARY_NOTES_TABLE_BOTTOM = 6.0
SUMMARY_NO_NOTES_HEIGHT = 56.0
SUMMARY_CUE_TIME_WIDTH = 42.0
SUMMARY_CUE_CITE_WIDTH = 72.0
SUMMARY_CUE_GRID_GAP = 12.0
SUMMARY_CUE_TEXT_WIDTH = (
    SUMMARY_WIDTH
    - SUMMARY_CUE_TIME_WIDTH
    - SUMMARY_CUE_CITE_WIDTH
    - 2 * SUMMARY_CUE_GRID_GAP
)
SUMMARY_CUE_ROW_PADDING = 18.75
SUMMARY_WRAP_WIDTH_RESERVE = 0.03 * inch
SUMMARY_QUOTE_GLYPHS = "“”"

SUMMARY_CARD_BODY_FONT = "Fraunces"
SUMMARY_CARD_BODY_SIZE = 9.75
SUMMARY_CARD_BODY_LINE_HEIGHT = 15.6
SUMMARY_QUOTE_FONT = "Fraunces Italic"
SUMMARY_QUOTE_SIZE = 9.4
SUMMARY_QUOTE_LINE_HEIGHT = 14.1
SUMMARY_QUOTE_TOP_GAP = 3.0
SUMMARY_NOTE_FONT = "Public Sans SemiBold"
SUMMARY_NOTE_SIZE = 9.4
SUMMARY_NOTE_LINE_HEIGHT = 13.6
SUMMARY_MONO_LINE_HEIGHT = 10.8   # cue timestamp / cite, single line

# Re-export public symbols that other modules depend on
timestamp_to_seconds = U.timestamp_to_seconds
parse_summary_sections = U.parse_summary_sections


def _safe_wrap_width(max_width: float) -> float:
    return max(max_width - SUMMARY_WRAP_WIDTH_RESERVE, 1.0)


def wrap_text(text: str, max_width: int) -> List[str]:
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


def _estimated_width(text: str, font_name: str, font_size: float) -> float:
    """Estimated rendered width with the conservative safety factor applied.

    Chromium renders the summary pages with real system fonts (Avenir Next /
    Georgia) that run wider than the standard Helvetica / Times metrics in
    :mod:`backend.font_metrics`, so estimates are inflated to over-predict.
    """
    return FM.string_width(text, font_name, font_size) * FM.SAFETY_FACTOR


def _wrap_text_to_width(
    text: str,
    max_width: float,
    *,
    font_name: str,
    font_size: float,
) -> List[str]:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return []

    max_width = _safe_wrap_width(max_width)
    words = text.split()
    lines: List[str] = []
    current: List[str] = []
    for word in words:
        candidate = " ".join(current + [word]).strip()
        if current and _estimated_width(candidate, font_name, font_size) > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines or [text]


def _estimate_context_card_height(text: str, inner_width: float) -> float:
    if not text:
        return 0.0
    body_lines = max(
        len(
            _wrap_text_to_width(
                text,
                inner_width,
                font_name=SUMMARY_CARD_BODY_FONT,
                font_size=SUMMARY_CARD_BODY_SIZE,
            )
        ),
        1,
    )
    return SUMMARY_CARD_TITLE_BLOCK + (body_lines * SUMMARY_CARD_BODY_LINE_HEIGHT)


def _choose_context_layout(speakers: str, call_summary: str) -> str:
    has_speakers = bool(speakers)
    has_summary = bool(call_summary)
    if has_speakers and has_summary:
        two_col_inner_width = (SUMMARY_WIDTH - SUMMARY_CONTEXT_TWO_COL_GAP) / 2.0
        speaker_height = _estimate_context_card_height(speakers, two_col_inner_width)
        summary_height = _estimate_context_card_height(call_summary, two_col_inner_width)
        if max(speaker_height, summary_height) <= 2.2 * inch:
            return "side-by-side"
        return "stacked"
    if has_speakers or has_summary:
        return "single"
    return "none"


def _estimate_context_height(layout: str, speakers: str, call_summary: str) -> float:
    if layout == "none":
        return 0.0

    if layout == "single":
        text = speakers or call_summary
        return _estimate_context_card_height(text, SUMMARY_WIDTH)

    if layout == "stacked":
        total = 0.0
        if speakers:
            total += _estimate_context_card_height(speakers, SUMMARY_WIDTH)
        if call_summary:
            if total:
                total += SUMMARY_CARD_GAP
            total += _estimate_context_card_height(call_summary, SUMMARY_WIDTH)
        return total

    two_col_inner_width = (SUMMARY_WIDTH - SUMMARY_CONTEXT_TWO_COL_GAP) / 2.0
    return max(
        _estimate_context_card_height(speakers, two_col_inner_width),
        _estimate_context_card_height(call_summary, two_col_inner_width),
    )


def _estimate_cue_height(cue: dict) -> float:
    """Estimated rendered height of one note row (time | body | cite grid)."""
    text_height = 0.0
    if cue.get("note"):
        note_lines = len(
            _wrap_text_to_width(
                cue.get("note", ""),
                SUMMARY_CUE_TEXT_WIDTH,
                font_name=SUMMARY_NOTE_FONT,
                font_size=SUMMARY_NOTE_SIZE,
            )
        )
        text_height += note_lines * SUMMARY_NOTE_LINE_HEIGHT
    if cue.get("quote"):
        # The speaker prefix renders in small sans caps ahead of the serif
        # quote; measuring the whole line as serif italic over-predicts,
        # which is the safe direction.
        prefix = f"{cue.get('speaker', '')} — " if cue.get("speaker") else ""
        quote_text = f"{prefix}{SUMMARY_QUOTE_GLYPHS[0]}{cue.get('quote', '')}{SUMMARY_QUOTE_GLYPHS[1]}"
        quote_lines = len(
            _wrap_text_to_width(
                quote_text,
                SUMMARY_CUE_TEXT_WIDTH,
                font_name=SUMMARY_QUOTE_FONT,
                font_size=SUMMARY_QUOTE_SIZE,
            )
        )
        text_height += SUMMARY_QUOTE_TOP_GAP + (quote_lines * SUMMARY_QUOTE_LINE_HEIGHT)

    content_height = max(SUMMARY_MONO_LINE_HEIGHT, text_height, SUMMARY_NOTE_LINE_HEIGHT)
    return content_height + SUMMARY_CUE_ROW_PADDING


def paginate_structured_summary(
    review_cues: List[dict],
    *,
    speakers: str = "",
    call_summary: str = "",
) -> dict:
    """Pack summary notes by estimated rendered height instead of fixed counts."""
    context_layout = _choose_context_layout(speakers, call_summary)
    context_height = _estimate_context_height(context_layout, speakers, call_summary)

    page1_budget = SUMMARY_CONTENT_HEIGHT - SUMMARY_ASSESSMENT_HEIGHT - SUMMARY_SECTION_GAP
    if context_height:
        page1_budget -= context_height + SUMMARY_SECTION_GAP
    page1_budget -= SUMMARY_NOTES_HEADING_HEIGHT
    if review_cues:
        page1_budget -= SUMMARY_NOTES_TABLE_BOTTOM
    else:
        page1_budget -= SUMMARY_NO_NOTES_HEIGHT

    overflow_budget = (
        SUMMARY_CONTENT_HEIGHT - SUMMARY_NOTES_HEADING_HEIGHT - SUMMARY_NOTES_TABLE_BOTTOM
    )
    page_budgets = [max(page1_budget, 0.0)]
    pages: List[List[dict]] = [[]]

    current_page = 0
    remaining = page_budgets[0]
    for cue in review_cues:
        cue_height = _estimate_cue_height(cue)
        if pages[current_page] and cue_height > remaining:
            pages.append([])
            current_page += 1
            page_budgets.append(max(overflow_budget, 0.0))
            remaining = page_budgets[current_page]

        pages[current_page].append(cue)
        remaining -= cue_height

    return {
        "context_layout": context_layout,
        "page1_review_cues": pages[0] if pages else [],
        "overflow_review_cue_pages": [
            {"review_cues": page_cues}
            for page_cues in pages[1:]
            if page_cues
        ],
    }


def _line_cite_for_timestamp(timestamp: str, line_entries: Optional[List[dict]]) -> str:
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


_LINE_CITE_RANGE_RE = re.compile(r"^\s*(\d+):(\d+)(?:\s*[-–—]\s*(\d+):(\d+))?\s*$")
_INLINE_QUOTED_TEXT_RE = re.compile(r'"([^"\n]{1,200})"')
_SENTENCE_END_RE = re.compile(r"[.!?](?:['\")\]]+)?\s*$")


def _normalize_line_cite_range(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", "", text)
    return text


def _normalize_note_text(value: str) -> str:
    text = str(value or "").strip()
    if '"' not in text:
        return text
    return _INLINE_QUOTED_TEXT_RE.sub(lambda m: m.group(1), text)


def _parse_line_cite_range(value: str) -> Optional[tuple]:
    match = _LINE_CITE_RANGE_RE.match(_normalize_line_cite_range(value))
    if not match:
        return None

    start_page = int(match.group(1))
    start_line = int(match.group(2))
    end_page = int(match.group(3) or start_page)
    end_line = int(match.group(4) or start_line)

    if (end_page, end_line) < (start_page, start_line):
        return None
    return start_page, start_line, end_page, end_line


def _entries_for_line_cite(
    line_cite: str,
    line_entries: Optional[List[dict]],
) -> List[dict]:
    parsed = _parse_line_cite_range(line_cite)
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


def _same_turn_neighbor(
    entry: dict,
    lookup: dict,
    *,
    direction: int,
) -> Optional[dict]:
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


def resolve_line_ref_context(
    line_cite: str,
    line_entries: Optional[List[dict]],
) -> Optional[dict]:
    selected = _entries_for_line_cite(line_cite, line_entries)
    if not selected:
        return None

    first = selected[0]
    start_seconds = float(first.get("start", 0) or 0)
    total = max(int(start_seconds), 0)
    mins, secs = divmod(total, 60)
    timestamp = f"[{mins:02d}:{secs:02d}]"

    return {
        "timestamp": timestamp,
        "speaker": str(first.get("speaker", "") or "").strip(),
        "line_cite": _normalize_line_cite_range(line_cite),
        "quote": _quote_from_line_cite(line_cite, line_entries),
        "start": start_seconds,
    }


def _quote_from_line_cite(
    line_cite: str,
    line_entries: Optional[List[dict]],
    *,
    max_lines: int = 3,
    max_chars: int = 220,
) -> str:
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


def hydrate_review_cues(
    cues: Optional[List[dict]],
    line_entries: Optional[List[dict]],
) -> List[dict]:
    """Enrich parsed review cues with deterministic line cites and quotes."""
    hydrated: List[dict] = []
    for cue in cues or []:
        item = dict(cue)
        item["note"] = _normalize_note_text(item.get("note", ""))
        line_ref = _normalize_line_cite_range(item.get("line_ref", ""))
        if line_ref and _parse_line_cite_range(line_ref):
            item["line_ref"] = line_ref
            item["line_cite"] = line_ref
            quote = _quote_from_line_cite(line_ref, line_entries)
            if quote:
                item["quote"] = quote
        else:
            item["line_ref"] = ""
            item["line_cite"] = _line_cite_for_timestamp(item.get("timestamp", ""), line_entries)
        hydrated.append(item)
    return hydrated


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
        # Walk through words, assigning to this line while they match
        line_remaining = line_text.strip()
        while word_idx < len(words) and line_remaining:
            w = words[word_idx]
            wtext = w.text.strip()
            if not wtext:
                word_idx += 1
                continue
            # Check if this word appears at the start of remaining text
            if line_remaining.lower().startswith(wtext.lower()):
                result[line_idx].append({
                    "t": wtext,
                    "s": round(w.start / 1000.0, 3),
                    "e": round(w.end / 1000.0, 3),
                })
                line_remaining = line_remaining[len(wtext):].lstrip()
                word_idx += 1
            else:
                # Skip non-matching characters (punctuation differences, etc.)
                # Try advancing past one character in line_remaining
                stripped = line_remaining.lstrip(" ,;:!?.'\"—-–()")
                if stripped != line_remaining:
                    line_remaining = stripped
                else:
                    break
    return result


def compute_line_entries(
    turns: List[TranscriptTurn],
    audio_duration: float,
    lines_per_page: int = 25,
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

            pgln = page * 100 + line_in_page
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
                "pgln": pgln,
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


def _build_cover_context(
    title_data: dict,
    summary: Optional[str],
    line_entries: Optional[List[dict]] = None,
) -> dict:
    """Build the Jinja context for the title sheet and summary sheet(s)."""
    case_name = U.safe_text(title_data.get("CASE_NAME"))
    file_name = U.safe_text(title_data.get("FILE_NAME"))
    call_datetime = U.safe_text(title_data.get("CALL_DATETIME"))
    display_datetime = U.format_display_datetime(call_datetime)
    file_duration = U.safe_text(title_data.get("FILE_DURATION"))
    inmate_name = U.safe_text(title_data.get("INMATE_NAME"))
    outside_number = U.safe_text(title_data.get("OUTSIDE_NUMBER_FMT"))
    firm_name = U.safe_text(title_data.get("FIRM_OR_ORGANIZATION_NAME"))

    # Cover title: "Call of April 14, 2026" with the time as a separate fact.
    parsed_dt = U.parse_call_datetime(call_datetime)
    if parsed_dt is not None:
        cover_title = f"Call of {parsed_dt.strftime('%B')} {parsed_dt.day}, {parsed_dt.year}"
        has_time = len(call_datetime.strip()) > 10
        cover_time = parsed_dt.strftime("%I:%M %p").lstrip("0") if has_time else ""
    else:
        cover_title = display_datetime or call_datetime or "Recorded Call"
        cover_time = ""

    cover_facts = [
        {"label": "Case", "value": case_name},
        {"label": "Defendant", "value": inmate_name},
        {"label": "Outside Number", "value": outside_number, "mono": True},
        {"label": "Recorded", "value": cover_time},
        {"label": "Source File", "value": file_name, "mono": True},
        {"label": "Prepared For", "value": firm_name},
    ]

    ctx: dict = {
        "case_name": case_name,
        "file_name": file_name,
        "display_datetime": display_datetime or call_datetime,
        "file_duration": file_duration,
        "inmate_name": inmate_name,
        "outside_number": outside_number,
        "cover_title": cover_title,
        "cover_facts": [f for f in cover_facts if f["value"]],
        "firm_name": firm_name,
        "has_summary": bool(summary),
        "overflow_review_cue_pages": [],
    }

    # ── Summary page context ──
    if summary:
        # The sheet title already carries the date; the meta line adds the
        # source file, time of day, and duration.
        ctx["summary_meta_file"] = U.shorten_middle(file_name, 34)
        meta_details = [cover_time, file_duration]
        ctx["summary_meta_details"] = " · ".join(p for p in meta_details if p)

        sections = U.parse_summary_sections(summary)
        ctx["is_structured"] = sections.get("structured", False)

        if sections.get("structured"):
            rel = sections.get("relevance", "")
            ctx["relevance"] = rel
            ctx["relevance_desc"] = U.RELEVANCE_DESC.get(rel, "")
            ctx["review_cues"] = hydrate_review_cues(sections.get("review_cue_items", []), line_entries)
            ctx["cue_count"] = len(ctx["review_cues"])

            spk = sections.get("speakers", "")
            ctx["speakers"] = spk.replace("\n", " ").strip() if spk else ""

            cs = sections.get("call_summary", "")
            ctx["call_summary"] = cs.replace("\n", " ").strip() if cs else ""
            pagination = paginate_structured_summary(
                ctx["review_cues"],
                speakers=ctx["speakers"],
                call_summary=ctx["call_summary"],
            )
            ctx["page1_review_cues"] = pagination["page1_review_cues"]
            ctx["overflow_review_cue_pages"] = pagination["overflow_review_cue_pages"]
            ctx["context_layout"] = pagination["context_layout"]
        else:
            rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", summary, re.IGNORECASE)
            if rel_match:
                rel = rel_match.group(1).upper()
                ctx["relevance"] = rel
                ctx["relevance_desc"] = U.RELEVANCE_DESC.get(rel, "")
                body = summary[rel_match.end():].strip()
            else:
                body = summary.strip()

            ctx["raw_body"] = body
            raw_blocks: list = []
            for para in re.split(r'\n{2,}', body):
                para = para.strip()
                if not para:
                    continue
                bullets: list = []
                texts: list = []
                for line in (l.strip() for l in para.split("\n") if l.strip()):
                    if re.match(r'^[-•*]\s', line):
                        if texts:
                            raw_blocks.append({"type": "text", "text": " ".join(texts)})
                            texts = []
                        bullets.append(re.sub(r'^[-•*]\s*', '', line))
                    else:
                        if bullets:
                            raw_blocks.append({"type": "bullet", "bullets": bullets})
                            bullets = []
                        texts.append(line)
                if bullets:
                    raw_blocks.append({"type": "bullet", "bullets": bullets})
                if texts:
                    raw_blocks.append({"type": "text", "text": " ".join(texts)})
            ctx["raw_blocks"] = raw_blocks

    return ctx


def _build_transcript_sheets(
    line_entries: List[dict],
    lines_per_page: int,
) -> List[dict]:
    """Convert precomputed line entries into per-sheet row geometry.

    Ports the ReportLab ``_draw_transcript_page`` baseline math: 25 rows at
    exactly 25 pt pitch, vertically centered between the half-margin content
    bounds, with the page number centered below.
    """
    pages: Dict[int, List[dict]] = defaultdict(list)
    for entry in line_entries:
        pages[int(entry.get("page", 1) or 1)].append(entry)

    if not pages:
        pages[1] = []

    content_top = PDF_PAGE_HEIGHT - PDF_MARGIN_TOP / 2   # from bottom of sheet
    content_bottom = PDF_MARGIN_BOTTOM / 2
    available_height = content_top - content_bottom
    line_block_height = ((max(lines_per_page, 1) - 1) * PDF_LINE_HEIGHT) + PDF_TEXT_SIZE
    vertical_padding = max((available_height - line_block_height) / 2.0, 0)
    top_baseline = content_top - vertical_padding - PDF_TEXT_SIZE

    row_baseline_offset = _courier_baseline_in_box(PDF_LINE_HEIGHT, PDF_TEXT_SIZE)

    sheets: List[dict] = []
    for page_number in sorted(pages):
        rows: List[dict] = []
        sorted_entries = sorted(
            pages[page_number],
            key=lambda e: (int(e.get("line", 0) or 0), e.get("id", "")),
        )
        for entry in sorted_entries:
            try:
                line_number = int(entry.get("line", 0) or 0)
            except (TypeError, ValueError):
                continue
            if line_number <= 0 or line_number > lines_per_page:
                continue
            baseline = top_baseline - (line_number - 1) * PDF_LINE_HEIGHT
            if baseline < content_bottom:
                continue

            # Convert the baseline (pt from sheet bottom) into a CSS top
            # offset for a 25pt line box whose baseline must land there.
            row_top = (PDF_PAGE_HEIGHT - baseline) - row_baseline_offset
            rows.append({
                "line": line_number,
                "top": f"{row_top:.3f}",
                "text": html.escape(str(entry.get("rendered_text", "")), quote=False),
            })
        sheets.append({"number": page_number, "rows": rows})
    return sheets


def create_pdf(
    title_data: dict,
    turns: List[TranscriptTurn],
    summary: Optional[str] = None,
    audio_duration: float = 0.0,
    lines_per_page: int = 25,
) -> bytes:
    """
    Create a PDF with:
      Sheet 1: Title page
      Sheet 2+: AI summary sheet(s) (if provided)
      Remaining sheets: Transcript (precise monospace layout)

    All sheets are rendered in a single headless-Chromium pass. The rendered
    page count is asserted against the emitted sheet count; a mismatch raises
    ``RuntimeError`` because every page:line citation in the product depends
    on the transcript pagination being exactly what Python computed.
    """
    from pypdf import PdfReader

    from .pdf_render import render_pdf

    line_entries = compute_line_entries(turns, audio_duration, lines_per_page)

    ctx = _build_cover_context(title_data, summary, line_entries=line_entries)
    ctx["transcript_sheets"] = _build_transcript_sheets(line_entries, lines_per_page)

    ctx["fonts_css"] = pdf_font_css(
        ("Fraunces", "Public Sans", "IBM Plex Mono", "Courier Prime")
    )

    cover_stats = []
    if ctx["file_duration"]:
        cover_stats.append({"n": ctx["file_duration"], "lbl": "Duration"})
    sheet_count = len(ctx["transcript_sheets"])
    cover_stats.append({
        "n": str(sheet_count),
        "lbl": "Transcript Page" if sheet_count == 1 else "Transcript Pages",
    })
    if ctx["has_summary"] and ctx.get("is_structured"):
        cover_stats.append({"n": str(ctx.get("cue_count", 0)), "lbl": "Review Cues"})
    ctx["cover_stats"] = cover_stats

    # Transcript text geometry (pt) — Python stays the layout source of truth.
    ctx["t_line_num_width"] = f"{PDF_LINE_NUM_RIGHT:.3f}"
    ctx["t_text_indent"] = f"{PDF_TEXT_X - PDF_LINE_NUM_RIGHT:.3f}"
    pn_baseline = PDF_MARGIN_BOTTOM / 2 - 0.1 * inch  # from sheet bottom
    pn_box = _courier_baseline_in_box(PDF_LINE_HEIGHT, PDF_PAGE_NUMBER_SIZE)
    ctx["t_pageno_top"] = f"{(PDF_PAGE_HEIGHT - pn_baseline) - pn_box:.3f}"

    summary_sheets = (1 + len(ctx.get("overflow_review_cue_pages") or [])) if ctx["has_summary"] else 0
    expected_sheets = 1 + summary_sheets + len(ctx["transcript_sheets"])

    template = U.get_jinja_env().get_template("transcript_pdf_template.html")
    html_str = template.render(**ctx)
    pdf_bytes = render_pdf(html_str)

    actual_pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    if actual_pages != expected_sheets:
        raise RuntimeError(
            f"Transcript PDF page-count mismatch: emitted {expected_sheets} "
            f"sheet divs but Chromium rendered {actual_pages} pages "
            f"(file: {U.safe_text(title_data.get('FILE_NAME'))!r}). "
            "Line citations would be unreliable; refusing to deliver."
        )
    return pdf_bytes
