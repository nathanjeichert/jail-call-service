"""
PDF generation for jail call transcripts.

Produces a 3-part PDF:
  Page 1: Title page (case info, file metadata)
  Page 2: AI summary
  Pages 3+: Legal-formatted transcript (25 lines/page, Courier, line numbers)

Ported and trimmed from main/backend/transcript_formatting.py.
"""

import io
import logging
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from .models import TranscriptTurn, WordTimestamp

logger = logging.getLogger(__name__)

# Layout constants
SPEAKER_PREFIX_SPACES = 10
CONTINUATION_SPACES = 0
SPEAKER_COLON = ":   "
MAX_TOTAL_LINE_WIDTH = 64
MAX_CONTINUATION_WIDTH = 64
MIN_LINE_DURATION_SECONDS = 1.25

PDF_PAGE_WIDTH, PDF_PAGE_HEIGHT = letter
PDF_MARGIN_LEFT = 1.0 * inch
PDF_MARGIN_RIGHT = 1.0 * inch
PDF_MARGIN_TOP = 0.75 * inch
PDF_MARGIN_BOTTOM = 0.75 * inch
PDF_LINE_NUMBER_GUTTER = 0.7 * inch
PDF_LINE_HEIGHT = 25.0
PDF_TEXT_FONT = "Courier"
PDF_TEXT_FONT_BOLD = "Courier-Bold"
PDF_TEXT_SIZE = 12
PDF_LINE_NUMBER_SIZE = 10
PDF_PAGE_NUMBER_SIZE = 10
PDF_BORDER_INSET = 0.33 * inch
PDF_BORDER_GAP = 4.0

# Summary page layout (professional sans-serif)
SUMMARY_FONT = "Helvetica"
SUMMARY_FONT_BOLD = "Helvetica-Bold"
SUMMARY_FONT_OBLIQUE = "Helvetica-Oblique"
SUMMARY_TITLE_SIZE = 15
SUMMARY_HEADING_SIZE = 10.5
SUMMARY_BODY_SIZE = 9.5
SUMMARY_META_SIZE = 8.5
SUMMARY_LINE_HEIGHT = 13
SUMMARY_SECTION_GAP = 10

RELEVANCE_COLORS = {
    "HIGH": colors.Color(0.75, 0.10, 0.10),   # red
    "MEDIUM": colors.Color(0.80, 0.50, 0.02),  # amber
    "LOW": colors.Color(0.15, 0.55, 0.15),     # green
}
TIMESTAMP_RE = re.compile(r"(\[(?:\d{1,2}:)?\d{2}:\d{2}\])")


def _safe_text(value: Optional[str]) -> str:
    return str(value or "").strip()


def timestamp_to_seconds(timestamp: Optional[str]) -> float:
    if not timestamp:
        return 0.0
    ts = timestamp.strip('[]').strip()
    parts = ts.split(':')
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


def _draw_border(c: canvas.Canvas) -> None:
    ox, oy = PDF_BORDER_INSET, PDF_BORDER_INSET
    ow = PDF_PAGE_WIDTH - 2 * PDF_BORDER_INSET
    oh = PDF_PAGE_HEIGHT - 2 * PDF_BORDER_INSET
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.rect(ox, oy, ow, oh, stroke=1, fill=0)
    c.rect(ox + PDF_BORDER_GAP / 2, oy + PDF_BORDER_GAP / 2,
           ow - PDF_BORDER_GAP, oh - PDF_BORDER_GAP, stroke=1, fill=0)


def _draw_title_page(c: canvas.Canvas, title_data: dict) -> None:
    _draw_border(c)
    cx = PDF_PAGE_WIDTH / 2
    y = PDF_PAGE_HEIGHT - 1.7 * inch

    firm = _safe_text(title_data.get("FIRM_OR_ORGANIZATION_NAME"))
    if firm:
        c.setFont(PDF_TEXT_FONT_BOLD, 14)
        c.drawCentredString(cx, y, firm)
        y -= 0.6 * inch

    c.setFont(PDF_TEXT_FONT_BOLD, 18)
    c.drawCentredString(cx, y, "Generated Transcript")
    y -= 0.6 * inch

    # Build metadata lines — skip any that have no value
    metadata_pairs = [
        ("Case Name", title_data.get("CASE_NAME")),
        ("Inmate", title_data.get("INMATE_NAME")),
        ("Date/Time", title_data.get("CALL_DATETIME")),
        ("Housing Unit", title_data.get("FACILITY")),
        ("Outside Party", title_data.get("OUTSIDE_NUMBER_FMT")),
        ("Call Outcome", title_data.get("CALL_OUTCOME")),
        ("Duration", title_data.get("FILE_DURATION")),
        ("Original File", title_data.get("FILE_NAME")),
        ("Notes", title_data.get("NOTES")),
    ]

    c.setFont(PDF_TEXT_FONT, PDF_TEXT_SIZE)
    for label, value in metadata_pairs:
        v = _safe_text(value)
        if not v:
            continue
        c.drawCentredString(cx, y, f"{label}: {v}")
        y -= 0.35 * inch


def _parse_summary_sections(summary: str) -> dict:
    """Parse structured Gemini output into sections. Falls back to raw if unrecognized."""
    sections = {"raw": summary}
    text = summary.strip()

    # Try to extract RELEVANCE
    rel_match = re.search(r"RELEVANCE:\s*(HIGH|MEDIUM|LOW)", text, re.IGNORECASE)
    if rel_match:
        sections["relevance"] = rel_match.group(1).upper()

    # Section header patterns
    headers = [
        ("key_findings", r"KEY\s+FINDINGS:?"),
        ("speakers", r"SPEAKERS?\s*(?:&|AND)\s*RELATIONSHIP:?"),
        ("call_summary", r"CALL\s+SUMMARY:?"),
    ]

    # Find positions of each section header
    positions = []
    for key, pattern in headers:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            positions.append((m.start(), m.end(), key))
    positions.sort()

    # Extract body text between headers
    for i, (start, end, key) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[end:next_start].strip()
        sections[key] = body

    # Only consider it "structured" if we found at least relevance + one section
    if "relevance" in sections and len(sections) > 2:
        sections["structured"] = True
    else:
        sections["structured"] = False

    return sections


def _wrap_text_for_width(text: str, font: str, size: float, max_width: float) -> List[str]:
    """Word-wrap text to fit within max_width pixels using the given font metrics."""
    if not text:
        return [""]
    words = text.split()
    lines, current = [], []
    current_width = 0.0
    space_width = _approx_char_width(font, size)

    for word in words:
        from reportlab.pdfbase.pdfmetrics import stringWidth
        word_w = stringWidth(word, font, size)
        gap = space_width if current else 0
        if current_width + gap + word_w <= max_width:
            current.append(word)
            current_width += gap + word_w
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
            current_width = word_w
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def _approx_char_width(font: str, size: float) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth
    return stringWidth(" ", font, size)


def _draw_text_with_timestamps(
    c: canvas.Canvas, x: float, y: float,
    text: str, font: str, size: float,
    title_data: dict, max_width: float,
) -> float:
    """Draw text with blue timestamp hyperlinks. Returns new y position."""
    audio_fn = _safe_text(title_data.get("AUDIO_FILENAME")) or _safe_text(title_data.get("FILE_NAME"))
    lines = _wrap_text_for_width(text, font, size, max_width)
    y_floor = PDF_MARGIN_BOTTOM + 0.4 * inch

    for line in lines:
        if y < y_floor:
            break
        current_x = x
        last_idx = 0
        c.setFont(font, size)

        for match in TIMESTAMP_RE.finditer(line):
            ts_str = match.group(1)
            prefix = line[last_idx:match.start()]
            if prefix:
                c.setFillColor(colors.black)
                c.drawString(current_x, y, prefix)
                current_x += c.stringWidth(prefix, font, size)

            # Blue timestamp link
            c.setFillColor(colors.Color(0.02, 0.39, 0.76))
            c.drawString(current_x, y, ts_str)
            x1 = current_x
            current_x += c.stringWidth(ts_str, font, size)
            if audio_fn:
                url = f"../viewer/index.html?call={audio_fn}&t={ts_str[1:-1]}"
                c.linkURL(url, (x1, y - 2, current_x, y + size), relative=1)
            last_idx = match.end()

        remainder = line[last_idx:]
        if remainder:
            c.setFillColor(colors.black)
            c.drawString(current_x, y, remainder)

        y -= SUMMARY_LINE_HEIGHT

    return y


def _draw_summary_page(c: canvas.Canvas, summary: str, title_data: dict) -> None:
    """Draw a professionally formatted AI analysis page."""
    _draw_border(c)

    left = PDF_MARGIN_LEFT
    right = PDF_PAGE_WIDTH - PDF_MARGIN_RIGHT
    text_width = right - left
    y = PDF_PAGE_HEIGHT - PDF_MARGIN_TOP - 0.15 * inch
    y_floor = PDF_MARGIN_BOTTOM + 0.4 * inch

    # ── Header: "AI ANALYSIS" left, case name right ──
    c.setFont(SUMMARY_FONT_BOLD, SUMMARY_TITLE_SIZE)
    c.setFillColor(colors.Color(0.12, 0.16, 0.21))
    c.drawString(left, y, "AI Analysis")
    case_name = _safe_text(title_data.get("CASE_NAME"))
    if case_name:
        c.setFont(SUMMARY_FONT, SUMMARY_META_SIZE)
        c.setFillColor(colors.Color(0.40, 0.45, 0.50))
        c.drawRightString(right, y + 2, case_name)
    y -= 6

    # Horizontal rule
    c.setStrokeColor(colors.Color(0.80, 0.83, 0.86))
    c.setLineWidth(0.6)
    c.line(left, y, right, y)
    y -= 14

    # ── Metadata line ──
    meta_parts = []
    fn = _safe_text(title_data.get("FILE_NAME"))
    if fn:
        meta_parts.append(fn)
    dur = _safe_text(title_data.get("FILE_DURATION"))
    if dur:
        meta_parts.append(dur)
    dt = _safe_text(title_data.get("CALL_DATETIME"))
    if dt:
        meta_parts.append(dt)
    inmate = _safe_text(title_data.get("INMATE_NAME"))
    if inmate:
        meta_parts.append(inmate)
    if meta_parts:
        c.setFont(SUMMARY_FONT, SUMMARY_META_SIZE)
        c.setFillColor(colors.Color(0.40, 0.45, 0.50))
        c.drawString(left, y, " \u00b7 ".join(meta_parts))
        y -= 20
    else:
        y -= 8

    # Parse structured sections
    sections = _parse_summary_sections(summary)

    if sections.get("structured"):
        # ── Relevance badge ──
        relevance = sections.get("relevance", "")
        if relevance in RELEVANCE_COLORS:
            badge_color = RELEVANCE_COLORS[relevance]
            badge_h = 20
            badge_w = text_width
            c.setFillColor(badge_color)
            c.roundRect(left, y - badge_h + 4, badge_w, badge_h, 3, fill=1, stroke=0)
            c.setFillColor(colors.white)
            c.setFont(SUMMARY_FONT_BOLD, 10.5)
            c.drawString(left + 10, y - badge_h + 10, f"RELEVANCE: {relevance}")
            y -= badge_h + SUMMARY_SECTION_GAP

        # ── Key Findings ──
        findings = sections.get("key_findings", "")
        if findings:
            c.setFillColor(colors.Color(0.12, 0.16, 0.21))
            c.setFont(SUMMARY_FONT_BOLD, SUMMARY_HEADING_SIZE)
            c.drawString(left, y, "KEY FINDINGS")
            y -= SUMMARY_LINE_HEIGHT + 2
            c.setFont(SUMMARY_FONT, SUMMARY_BODY_SIZE)
            for bullet_line in findings.split("\n"):
                if y < y_floor:
                    break
                bullet_line = bullet_line.strip()
                if not bullet_line:
                    continue
                # Ensure bullet prefix
                if not bullet_line.startswith("-") and not bullet_line.startswith("\u2022"):
                    bullet_line = "- " + bullet_line
                y = _draw_text_with_timestamps(c, left, y, bullet_line, SUMMARY_FONT, SUMMARY_BODY_SIZE, title_data, text_width)
                y -= 2  # small gap between bullets
            y -= SUMMARY_SECTION_GAP

        # ── Speakers & Relationship ──
        speakers = sections.get("speakers", "")
        if speakers and y > y_floor:
            c.setFillColor(colors.Color(0.12, 0.16, 0.21))
            c.setFont(SUMMARY_FONT_BOLD, SUMMARY_HEADING_SIZE)
            c.drawString(left, y, "SPEAKERS & RELATIONSHIP")
            y -= SUMMARY_LINE_HEIGHT + 2
            y = _draw_text_with_timestamps(c, left, y, speakers.replace("\n", " "), SUMMARY_FONT, SUMMARY_BODY_SIZE, title_data, text_width)
            y -= SUMMARY_SECTION_GAP

        # ── Call Summary ──
        call_summary = sections.get("call_summary", "")
        if call_summary and y > y_floor:
            c.setFillColor(colors.Color(0.12, 0.16, 0.21))
            c.setFont(SUMMARY_FONT_BOLD, SUMMARY_HEADING_SIZE)
            c.drawString(left, y, "CALL SUMMARY")
            y -= SUMMARY_LINE_HEIGHT + 2
            y = _draw_text_with_timestamps(c, left, y, call_summary.replace("\n", " "), SUMMARY_FONT, SUMMARY_BODY_SIZE, title_data, text_width)

    else:
        # ── Fallback: render raw summary text ──
        c.setFont(SUMMARY_FONT, SUMMARY_BODY_SIZE)
        y = _draw_text_with_timestamps(c, left, y, summary, SUMMARY_FONT, SUMMARY_BODY_SIZE, title_data, text_width)

    # Page number
    c.setFillColor(colors.black)
    pn_y = PDF_BORDER_INSET + PDF_BORDER_GAP / 2 + 8
    c.setFont(SUMMARY_FONT, PDF_PAGE_NUMBER_SIZE)
    c.drawCentredString(PDF_PAGE_WIDTH / 2, pn_y, "2")


def _draw_transcript_page(
    c: canvas.Canvas,
    page_entries: List[dict],
    lines_per_page: int,
    page_number: int,
) -> None:
    _draw_border(c)

    content_top = PDF_PAGE_HEIGHT - PDF_MARGIN_TOP
    available_height = content_top - PDF_MARGIN_BOTTOM
    line_block_height = ((max(lines_per_page, 1) - 1) * PDF_LINE_HEIGHT) + PDF_TEXT_SIZE
    vertical_padding = max((available_height - line_block_height) / 2.0, 0)
    top_baseline = content_top - vertical_padding - PDF_TEXT_SIZE
    number_right_x = PDF_MARGIN_LEFT - 6
    text_x = PDF_MARGIN_LEFT

    sorted_entries = sorted(page_entries, key=lambda e: (int(e.get("line", 0) or 0), e.get("id", "")))
    for entry in sorted_entries:
        try:
            line_number = int(entry.get("line", 0) or 0)
        except (TypeError, ValueError):
            continue
        if line_number <= 0 or line_number > lines_per_page:
            continue
        y = top_baseline - (line_number - 1) * PDF_LINE_HEIGHT
        if y < PDF_MARGIN_BOTTOM:
            continue

        c.setFillColor(colors.Color(0.45, 0.45, 0.45))
        c.setFont(PDF_TEXT_FONT, PDF_LINE_NUMBER_SIZE)
        c.drawRightString(number_right_x, y, str(line_number))

        c.setFillColor(colors.black)
        c.setFont(PDF_TEXT_FONT, PDF_TEXT_SIZE)
        c.drawString(text_x, y, str(entry.get("rendered_text", "")))

    pn_y = PDF_BORDER_INSET + PDF_BORDER_GAP / 2 + 8
    c.setFillColor(colors.black)
    c.setFont(PDF_TEXT_FONT, PDF_PAGE_NUMBER_SIZE)
    c.drawCentredString(PDF_PAGE_WIDTH / 2, pn_y, str(page_number))


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

        if is_continuation:
            speaker_prefix = ""
            max_first_line = MAX_CONTINUATION_WIDTH
        else:
            speaker_prefix = " " * SPEAKER_PREFIX_SPACES + speaker_name + SPEAKER_COLON
            max_first_line = MAX_TOTAL_LINE_WIDTH - len(speaker_prefix)

        wrapped = wrap_text(text, max_first_line)
        if not wrapped:
            wrapped = [""]

        cont_text = " ".join(wrapped[1:])
        cont_lines = wrap_text(cont_text, MAX_CONTINUATION_WIDTH) if cont_text else []
        all_lines = [wrapped[0]] + cont_lines

        for line_idx, line_text in enumerate(all_lines):
            is_cont_line = is_continuation or line_idx > 0
            if line_idx == 0 and not is_continuation:
                rendered = speaker_prefix + line_text
            else:
                rendered = " " * CONTINUATION_SPACES + line_text

            pgln = page * 100 + line_in_page
            line_entries.append({
                "id": f"{turn_idx}-{line_idx}",
                "turn_index": turn_idx,
                "line_index": line_idx,
                "speaker": speaker_name,
                "text": line_text,
                "rendered_text": rendered,
                "start": start_sec,
                "end": start_sec,
                "page": page,
                "line": line_in_page,
                "pgln": pgln,
                "is_continuation": is_cont_line,
            })

            line_in_page += 1
            if line_in_page > lines_per_page:
                page += 1
                line_in_page = 1

    return line_entries


def create_pdf(
    title_data: dict,
    turns: List[TranscriptTurn],
    summary: Optional[str] = None,
    audio_duration: float = 0.0,
    lines_per_page: int = 25,
) -> bytes:
    """
    Create a PDF with:
      Page 1: Title page
      Page 2: AI summary (if provided)
      Pages 3+: Transcript
    """
    output = io.BytesIO()
    c = canvas.Canvas(output, pagesize=letter, pageCompression=1)

    # Page 1: Title
    _draw_title_page(c, title_data)
    c.showPage()

    # Page 2: Summary
    if summary:
        _draw_summary_page(c, summary, title_data)
        c.showPage()
        transcript_page_offset = 2
    else:
        transcript_page_offset = 1

    # Transcript pages
    line_entries = compute_line_entries(turns, audio_duration, lines_per_page)
    pages: Dict[int, List[dict]] = defaultdict(list)
    for entry in line_entries:
        pages[int(entry.get("page", 1) or 1)].append(entry)

    if not pages:
        pages[1] = []

    for page_number in sorted(pages):
        display_page = page_number + transcript_page_offset
        _draw_transcript_page(c, pages[page_number], lines_per_page, display_page)
        c.showPage()

    c.save()
    output.seek(0)
    return output.read()
