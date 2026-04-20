"""
PDF generation for jail call transcripts.

Produces a 3-part PDF:
  Page 1: Title page (case info, file metadata)
  Page 2: AI summary
  Pages 3+: Legal-formatted transcript (25 lines/page, Courier, line numbers)

Ported and trimmed from main/backend/transcript_formatting.py.
"""

import io
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from . import pdf_utils as U
from .models import TranscriptTurn, WordTimestamp

# Register monospace fonts (platform-aware; safe to call multiple times)
U.register_fonts()

# Text layout constants
SPEAKER_PREFIX_SPACES = 10
CONTINUATION_SPACES = 5
SPEAKER_COLON = ":   "

# Transcript page geometry (legal deposition style)
PDF_PAGE_WIDTH, PDF_PAGE_HEIGHT = letter
PDF_MARGIN_TOP = 0.75 * inch
PDF_MARGIN_BOTTOM = 0.75 * inch
PDF_LINE_HEIGHT = 25.0
PDF_TEXT_FONT = "CourierNew"
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
_COURIER_CHAR_W = stringWidth("M", PDF_TEXT_FONT, PDF_TEXT_SIZE)
_TRANSCRIPT_RULE_WIDTH = PDF_RULE_RIGHT - PDF_RULE_LEFT_INNER
MAX_LINE_CHARS = int((_TRANSCRIPT_RULE_WIDTH - 12) / _COURIER_CHAR_W)
PDF_TEXT_BLOCK_WIDTH = MAX_LINE_CHARS * _COURIER_CHAR_W
PDF_TEXT_X = PDF_RULE_LEFT_INNER + ((_TRANSCRIPT_RULE_WIDTH - PDF_TEXT_BLOCK_WIDTH) / 2.0)

SUMMARY_LEFT = 0.68 * inch
SUMMARY_RIGHT = 0.62 * inch
SUMMARY_WIDTH = PDF_PAGE_WIDTH - SUMMARY_LEFT - SUMMARY_RIGHT
SUMMARY_CONTENT_TOP = 1.55 * inch
SUMMARY_CONTENT_BOTTOM = 0.74 * inch
SUMMARY_CONTENT_HEIGHT = PDF_PAGE_HEIGHT - SUMMARY_CONTENT_TOP - SUMMARY_CONTENT_BOTTOM
SUMMARY_ASSESSMENT_HEIGHT = 0.74 * inch
SUMMARY_SECTION_GAP = 0.14 * inch
SUMMARY_CARD_GAP = 0.16 * inch
SUMMARY_CARD_PADDING_X = 0.18 * inch
SUMMARY_CARD_PADDING_Y = 0.16 * inch
SUMMARY_CONTEXT_TWO_COL_GAP = 0.18 * inch
SUMMARY_NOTES_HEADING_HEIGHT = 0.24 * inch
SUMMARY_NOTES_KEY_HEIGHT = 0.34 * inch
SUMMARY_NOTES_TABLE_BOTTOM = 0.08 * inch
SUMMARY_NO_NOTES_HEIGHT = 0.82 * inch
SUMMARY_CUE_TIME_WIDTH = 1.02 * inch
SUMMARY_CUE_TEXT_PAD_LEFT = 0.13 * inch
SUMMARY_CUE_TIME_TEXT_WIDTH = SUMMARY_CUE_TIME_WIDTH - 0.06 * inch
SUMMARY_CUE_TEXT_WIDTH = SUMMARY_WIDTH - SUMMARY_CUE_TIME_WIDTH - SUMMARY_CUE_TEXT_PAD_LEFT

SUMMARY_CARD_TITLE_FONT = "Helvetica-Bold"
SUMMARY_CARD_TITLE_SIZE = 7.8
SUMMARY_CARD_TITLE_LINE_HEIGHT = 10.0
SUMMARY_CARD_BODY_FONT = "Helvetica"
SUMMARY_CARD_BODY_SIZE = 9.1
SUMMARY_CARD_BODY_LINE_HEIGHT = 13.5
SUMMARY_SPEAKER_FONT = "Helvetica-Bold"
SUMMARY_SPEAKER_SIZE = 7.6
SUMMARY_SPEAKER_LINE_HEIGHT = 9.0
SUMMARY_QUOTE_FONT = "Times-Roman"
SUMMARY_QUOTE_SIZE = 10.4
SUMMARY_QUOTE_LINE_HEIGHT = 13.7
SUMMARY_NOTE_FONT = "Helvetica"
SUMMARY_NOTE_SIZE = 8.75
SUMMARY_NOTE_LINE_HEIGHT = 12.1
SUMMARY_TIMESTAMP_FONT = "Helvetica-Bold"
SUMMARY_TIMESTAMP_SIZE = 9.7
SUMMARY_TIMESTAMP_LINE_HEIGHT = 10.9
SUMMARY_LINE_CITE_FONT = "Helvetica-Bold"
SUMMARY_LINE_CITE_SIZE = 8.0
SUMMARY_LINE_CITE_LINE_HEIGHT = 9.0

# Re-export public symbols that other modules depend on
timestamp_to_seconds = U.timestamp_to_seconds
parse_summary_sections = U.parse_summary_sections


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

    words = text.split()
    lines: List[str] = []
    current: List[str] = []
    for word in words:
        candidate = " ".join(current + [word]).strip()
        if current and stringWidth(candidate, font_name, font_size) > max_width:
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
    return (
        (2 * SUMMARY_CARD_PADDING_Y)
        + SUMMARY_CARD_TITLE_LINE_HEIGHT
        + 0.08 * inch
        + (body_lines * SUMMARY_CARD_BODY_LINE_HEIGHT)
    )


def _choose_context_layout(speakers: str, call_summary: str) -> str:
    has_speakers = bool(speakers)
    has_summary = bool(call_summary)
    if has_speakers and has_summary:
        two_col_inner_width = (
            (SUMMARY_WIDTH - SUMMARY_CONTEXT_TWO_COL_GAP) / 2.0
        ) - (2 * SUMMARY_CARD_PADDING_X)
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
        inner_width = SUMMARY_WIDTH - (2 * SUMMARY_CARD_PADDING_X)
        text = speakers or call_summary
        return _estimate_context_card_height(text, inner_width)

    if layout == "stacked":
        inner_width = SUMMARY_WIDTH - (2 * SUMMARY_CARD_PADDING_X)
        total = 0.0
        if speakers:
            total += _estimate_context_card_height(speakers, inner_width)
        if call_summary:
            if total:
                total += SUMMARY_CARD_GAP
            total += _estimate_context_card_height(call_summary, inner_width)
        return total

    two_col_inner_width = (
        (SUMMARY_WIDTH - SUMMARY_CONTEXT_TWO_COL_GAP) / 2.0
    ) - (2 * SUMMARY_CARD_PADDING_X)
    return max(
        _estimate_context_card_height(speakers, two_col_inner_width),
        _estimate_context_card_height(call_summary, two_col_inner_width),
    )


def _estimate_cue_height(cue: dict) -> float:
    timestamp_lines = max(
        len(
            _wrap_text_to_width(
                cue.get("timestamp", ""),
                SUMMARY_CUE_TIME_TEXT_WIDTH,
                font_name=SUMMARY_TIMESTAMP_FONT,
                font_size=SUMMARY_TIMESTAMP_SIZE,
            )
        ),
        1,
    )
    time_height = timestamp_lines * SUMMARY_TIMESTAMP_LINE_HEIGHT
    if cue.get("line_cite"):
        line_cite_lines = len(
            _wrap_text_to_width(
                cue.get("line_cite", ""),
                SUMMARY_CUE_TIME_TEXT_WIDTH,
                font_name=SUMMARY_LINE_CITE_FONT,
                font_size=SUMMARY_LINE_CITE_SIZE,
            )
        )
        time_height += (0.055 * inch) + (line_cite_lines * SUMMARY_LINE_CITE_LINE_HEIGHT)

    text_height = 0.0
    if cue.get("speaker"):
        text_height += SUMMARY_SPEAKER_LINE_HEIGHT + 0.03 * inch
    if cue.get("quote"):
        quote_lines = len(
            _wrap_text_to_width(
                cue.get("quote", ""),
                SUMMARY_CUE_TEXT_WIDTH,
                font_name=SUMMARY_QUOTE_FONT,
                font_size=SUMMARY_QUOTE_SIZE,
            )
        )
        text_height += (quote_lines * SUMMARY_QUOTE_LINE_HEIGHT) + 0.035 * inch
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

    content_height = max(time_height, text_height, SUMMARY_NOTE_LINE_HEIGHT)
    return content_height + (0.21 * inch)


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
        page1_budget -= SUMMARY_NOTES_KEY_HEIGHT + SUMMARY_NOTES_TABLE_BOTTOM
    else:
        page1_budget -= SUMMARY_NO_NOTES_HEIGHT

    overflow_budget = SUMMARY_CONTENT_HEIGHT - SUMMARY_NOTES_HEADING_HEIGHT - SUMMARY_NOTES_TABLE_BOTTOM
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


def _draw_transcript_rules(c: canvas.Canvas) -> None:
    """Draw vertical rules: double line on left (gutter), single on right."""
    rule_top = PDF_PAGE_HEIGHT
    rule_bottom = 0
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    # Double line on left
    c.line(PDF_RULE_LEFT_OUTER, rule_bottom, PDF_RULE_LEFT_OUTER, rule_top)
    c.line(PDF_RULE_LEFT_INNER, rule_bottom, PDF_RULE_LEFT_INNER, rule_top)
    # Single line on right
    c.line(PDF_RULE_RIGHT, rule_bottom, PDF_RULE_RIGHT, rule_top)


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


_LINE_CITE_RANGE_RE = re.compile(r"^\s*(\d+):(\d+)(?:\s*[-\u2013\u2014]\s*(\d+):(\d+))?\s*$")
_INLINE_QUOTED_TEXT_RE = re.compile(r'"([^"\n]{1,200})"')


def _normalize_line_cite_range(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\u2013", "-").replace("\u2014", "-")
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
    return quote if quote and len(quote) <= max_chars else ""


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


def _draw_transcript_page(
    c: canvas.Canvas,
    page_entries: List[dict],
    lines_per_page: int,
    page_number: int,
) -> None:
    _draw_transcript_rules(c)

    content_top = PDF_PAGE_HEIGHT - PDF_MARGIN_TOP / 2
    content_bottom = PDF_MARGIN_BOTTOM / 2
    available_height = content_top - content_bottom
    line_block_height = ((max(lines_per_page, 1) - 1) * PDF_LINE_HEIGHT) + PDF_TEXT_SIZE
    vertical_padding = max((available_height - line_block_height) / 2.0, 0)
    top_baseline = content_top - vertical_padding - PDF_TEXT_SIZE

    # Page number at bottom center
    pn_y = content_bottom - 0.1 * inch
    c.setFillColor(colors.black)
    c.setFont(PDF_TEXT_FONT, PDF_PAGE_NUMBER_SIZE)
    c.drawCentredString(PDF_PAGE_WIDTH / 2, pn_y, str(page_number))

    sorted_entries = sorted(page_entries, key=lambda e: (int(e.get("line", 0) or 0), e.get("id", "")))
    for entry in sorted_entries:
        try:
            line_number = int(entry.get("line", 0) or 0)
        except (TypeError, ValueError):
            continue
        if line_number <= 0 or line_number > lines_per_page:
            continue
        y = top_baseline - (line_number - 1) * PDF_LINE_HEIGHT
        if y < content_bottom:
            continue

        c.setFillColor(colors.black)
        c.setFont(PDF_TEXT_FONT, PDF_LINE_NUMBER_SIZE)
        c.drawRightString(PDF_LINE_NUM_RIGHT, y, str(line_number))

        c.setFont(PDF_TEXT_FONT, PDF_TEXT_SIZE)
        c.drawString(PDF_TEXT_X, y, str(entry.get("rendered_text", "")))


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


def _render_cover_pages(
    title_data: dict,
    summary: Optional[str],
    line_entries: Optional[List[dict]] = None,
) -> bytes:
    """Render title page (+ optional AI analysis page) as PDF via WeasyPrint.

    Uses an HTML/CSS template for polished, design-quality output, then
    returns raw PDF bytes ready to be merged with the transcript pages.
    """
    from weasyprint import HTML

    template = U.get_jinja_env().get_template("pdf_cover_template.html")
    base_url = str(Path(__file__).parent)

    case_name = U.safe_text(title_data.get("CASE_NAME"))
    file_name = U.safe_text(title_data.get("FILE_NAME"))
    call_datetime = U.safe_text(title_data.get("CALL_DATETIME"))
    display_datetime = U.format_display_datetime(call_datetime)
    file_duration = U.safe_text(title_data.get("FILE_DURATION"))
    inmate_name = U.safe_text(title_data.get("INMATE_NAME"))
    outside_number = U.safe_text(title_data.get("OUTSIDE_NUMBER_FMT"))

    title_meta = [
        ("Defendant", inmate_name),
        ("Outside Number", outside_number),
        ("Case", case_name),
    ]
    title_meta = [{"label": label, "value": value} for label, value in title_meta if value]

    ctx: dict = {
        "case_name": case_name,
        "file_name": file_name,
        "display_datetime": display_datetime or call_datetime,
        "file_duration": file_duration,
        "inmate_name": inmate_name,
        "outside_number": outside_number,
        "title_meta": title_meta,
        "firm_name": U.safe_text(title_data.get("FIRM_OR_ORGANIZATION_NAME")),
        "has_summary": bool(summary),
    }

    # ── Summary page context ──
    if summary:
        ctx["summary_meta_file"] = U.shorten_middle(file_name)
        meta_details = [display_datetime or call_datetime]
        ctx["summary_meta_details"] = " \u00b7 ".join(p for p in meta_details if p)

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
            ctx["overflow_review_cue_pages"] = []
            raw_blocks: list = []
            for para in re.split(r'\n{2,}', body):
                para = para.strip()
                if not para:
                    continue
                bullets: list = []
                texts: list = []
                for line in (l.strip() for l in para.split("\n") if l.strip()):
                    if re.match(r'^[-\u2022*]\s', line):
                        if texts:
                            raw_blocks.append({"type": "text", "text": " ".join(texts)})
                            texts = []
                        bullets.append(re.sub(r'^[-\u2022*]\s*', '', line))
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

    html_str = template.render(**ctx)
    return HTML(string=html_str, base_url=base_url).write_pdf()


def create_pdf(
    title_data: dict,
    turns: List[TranscriptTurn],
    summary: Optional[str] = None,
    audio_duration: float = 0.0,
    lines_per_page: int = 25,
) -> bytes:
    """
    Create a PDF with:
      Page 1: Title page        (WeasyPrint — HTML/CSS)
      Page 2: AI summary        (WeasyPrint — HTML/CSS, if provided)
      Pages 3+: Transcript      (ReportLab  — precise monospace layout)
    """
    from pypdf import PdfReader, PdfWriter

    line_entries = compute_line_entries(turns, audio_duration, lines_per_page)

    # ── Cover pages via WeasyPrint ──
    cover_pdf = _render_cover_pages(title_data, summary, line_entries=line_entries)

    # ── Transcript pages via ReportLab ──
    transcript_buf = io.BytesIO()
    c = canvas.Canvas(transcript_buf, pagesize=letter, pageCompression=1)
    pages: Dict[int, List[dict]] = defaultdict(list)
    for entry in line_entries:
        pages[int(entry.get("page", 1) or 1)].append(entry)

    if not pages:
        pages[1] = []

    for page_number in sorted(pages):
        _draw_transcript_page(c, pages[page_number], lines_per_page, page_number)
        c.showPage()

    c.save()
    transcript_buf.seek(0)

    # ── Merge with pypdf ──
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(cover_pdf)).pages:
        writer.add_page(page)
    for page in PdfReader(transcript_buf).pages:
        writer.add_page(page)

    final = io.BytesIO()
    writer.write(final)
    final.seek(0)
    return final.read()
